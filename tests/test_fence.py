from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import ContractError, custody_receipt
from media_interlock.adapters.arr import ArrExternalGrab, ArrExternalObservation
from media_interlock.config import VideoCandidateHealthConfig
from media_interlock.fence.model import ExternalAdoptionIntent, FencePolicy, FenceState, PostPnrAdoptionIntent, PostPnrHistoricalActivationIntent, PostPnrHistoricalAdoptionIntent, PreAdmissionIntent, QbittorrentActivityObservation, QbittorrentObservation, ReservationState
from media_interlock.fence.headroom import HeadroomPool, PhysicalHeadroom
from media_interlock.fence.service import FenceService, FenceSource
from media_interlock.fence.store import FenceStore


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))
HASH = "a" * 40
CATEGORY = "media-interlock-radarr"
SOURCE = FenceSource(CATEGORY, Path("/downloads/radarr"))


def intent(*, size: int = 400) -> PreAdmissionIntent:
    return PreAdmissionIntent(OPERATION_ID, "radarr", "movie-42", "b" * 64, size, "7")


def bound(state: FenceState, *, size: int = 400) -> None:
    assert state.pre_admit(intent(size=size), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
    state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
    state.request_tag(OPERATION_ID)
    assert state.mark_qbittorrent_tagged(OPERATION_ID, observed_bytes=size)
    state.request_resume(OPERATION_ID)
    state.mark_qbittorrent_active(OPERATION_ID)


class FenceStateTests(unittest.TestCase):
    def test_video_candidate_records_metadata_deadline_and_durable_invalidation(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)

        state.ensure_video_candidate(OPERATION_ID, now=100)
        state.record_video_candidate_failure(OPERATION_ID, now=200)
        state.record_video_candidate_failure(OPERATION_ID, now=300)
        self.assertTrue(state.invalidate_video_candidate(OPERATION_ID, reason="metadata_timeout", now=400))

        candidate = state.video_candidate(OPERATION_ID)
        self.assertEqual("invalidated", candidate["status"])
        self.assertEqual("metadata_timeout", candidate["reason"])
        self.assertEqual(400, candidate["invalidated_at"])

        restored = FenceState.from_snapshot(
            FencePolicy(capacity_bytes=1_000, max_inflight=1),
            state.records(),
            {},
            video_candidates=state.video_candidate_records(),
        )
        self.assertEqual(candidate, restored.video_candidate(OPERATION_ID))

    def test_completed_candidate_is_discarded_and_legacy_terminal_snapshot_recovers(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        state.ensure_video_candidate(OPERATION_ID, now=100)
        state.record_video_candidate_metadata(OPERATION_ID, downloaded_bytes=400, now=200)
        legacy_candidates = state.video_candidate_records()

        state.complete(OPERATION_ID)

        self.assertEqual((), state.video_candidate_records())
        restored = FenceState.from_snapshot(
            FencePolicy(capacity_bytes=1_000, max_inflight=1),
            state.records(),
            {},
            video_candidates=legacy_candidates,
        )
        self.assertEqual((), restored.video_candidate_records())

    def test_managed_historical_reservations_keep_bytes_but_free_all_inflight_slots(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=100_000, max_inflight=13))
        operation_ids = [str(uuid.uuid4()) for _ in range(13)]
        for index, operation_id in enumerate(operation_ids):
            historical = PostPnrHistoricalAdoptionIntent(operation_id, "sonarr", 7, (str(index + 1),), f"{index:040x}", "media-interlock-sonarr", "/downloads/shows", 400, (index + 1,))
            self.assertTrue(state.adopt_post_pnr_historical(historical, qbittorrent_ready=True).admitted)
            state.request_tag(operation_id); state.mark_qbittorrent_tagged(operation_id, observed_bytes=400)
            state.request_historical_activation(PostPnrHistoricalActivationIntent(operation_id)); state.mark_historical_managed(operation_id)
        restored = FenceState.from_snapshot(FencePolicy(capacity_bytes=100_000, max_inflight=4), state.records(), {}, post_pnr_historical_adoptions=state.post_pnr_historical_records(), post_pnr_historical_activations=state.post_pnr_historical_activation_records())
        self.assertEqual(5_200, restored.reserved_bytes)
        for index in range(4):
            decision = restored.pre_admit(PreAdmissionIntent(str(uuid.uuid4()), "radarr", f"movie-{index}", "c" * 64, 100, str(index)), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
            self.assertTrue(decision.admitted)
        self.assertEqual("concurrency_exhausted", restored.pre_admit(PreAdmissionIntent(str(uuid.uuid4()), "radarr", "movie-final", "d" * 64, 100, "last"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).reason)

    def test_terminal_reservation_requires_an_exact_freeze_before_hardlink_custody_release(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        terminal = state.complete(OPERATION_ID)

        state.request_freeze(OPERATION_ID)
        state.mark_qbittorrent_frozen(OPERATION_ID)

        self.assertEqual(ReservationState.QBITTORRENT_FROZEN, state.reservation(OPERATION_ID).state)
        self.assertTrue(state.accept_custody(custody_receipt(OPERATION_ID, terminal.body["fence_reservation_id"], "publisher-r-1")))

    def test_pre_admission_reserves_before_arr_has_a_download_identity(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))

        self.assertTrue(state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted)

        reservation = state.reservation(OPERATION_ID)
        self.assertEqual(ReservationState.PRE_ADMITTED, reservation.state)
        self.assertIsNone(reservation.download_id)
        self.assertEqual(400, state.reserved_bytes)

    def test_observed_grab_is_bound_once_and_survives_restart(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)

        restored = FenceState.from_records(FencePolicy(capacity_bytes=1_000, max_inflight=1), state.records())

        self.assertEqual(HASH, restored.reservation(OPERATION_ID).download_id)
        with self.assertRaises(ContractError):
            restored.bind_observed_grab(OPERATION_ID, download_id="b" * 40, torrent_hash="b" * 40)

    def test_observed_arr_download_id_must_canonicalize_to_the_bound_torrent_hash(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted

        with self.assertRaises(ContractError):
            state.bind_observed_grab(OPERATION_ID, download_id="B" * 40, torrent_hash="a" * 40)

        state.bind_observed_grab(OPERATION_ID, download_id=HASH.upper(), torrent_hash=HASH)
        records = [dict(record) for record in state.records()]
        records[0]["download_id"] = "B" * 40
        with self.assertRaises(ContractError):
            FenceState.from_records(FencePolicy(capacity_bytes=1_000, max_inflight=1), records)

    def test_tag_observation_precedes_resume_and_capacity_overrun_keeps_stopped(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=400, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID)

        self.assertFalse(state.mark_qbittorrent_tagged(OPERATION_ID, observed_bytes=401))
        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        with self.assertRaises(ContractError):
            state.mark_qbittorrent_active(OPERATION_ID)

    def test_terminal_contains_the_real_arr_download_id_until_exact_custody(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)

        terminal = state.complete(OPERATION_ID)

        self.assertEqual(HASH, terminal.body["download_id"])
        self.assertEqual(400, state.reserved_bytes)
        self.assertTrue(state.accept_custody(custody_receipt(OPERATION_ID, terminal.body["fence_reservation_id"], "publisher-r-1")))
        self.assertEqual(0, state.reserved_bytes)

    def test_released_external_observation_remains_idempotent_until_its_watermark_is_saved(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        external = ExternalAdoptionIntent(OPERATION_ID, "radarr", "42", HASH, HASH, 400, 8, "c" * 64)
        self.assertTrue(state.adopt_external(external, qbittorrent_ready=True, publisher_ready=True).admitted)
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID)
        state.mark_qbittorrent_tagged(OPERATION_ID, observed_bytes=400)
        state.request_resume(OPERATION_ID)
        state.mark_qbittorrent_active(OPERATION_ID)
        terminal = state.complete(OPERATION_ID)
        self.assertTrue(state.accept_custody(custody_receipt(OPERATION_ID, terminal.body["fence_reservation_id"], "publisher-r-1")))

        replay = state.adopt_external(external, qbittorrent_ready=True, publisher_ready=True)

        self.assertTrue(replay.admitted)
        self.assertEqual("idempotent", replay.reason)

    def test_quiescence_rejects_new_admission_and_marks_only_active_owned_work_for_pause(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)

        state.begin_quiescence()
        state.request_pause(OPERATION_ID)

        self.assertTrue(state.quiescing)
        self.assertEqual(ReservationState.PAUSE_INTENT_RECORDED, state.reservation(OPERATION_ID).state)
        self.assertEqual("quiescing", state.pre_admit(PreAdmissionIntent("22345678-1234-4678-9234-567812345678", "radarr", "43", "d" * 64, 200, "8"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).reason)


class FenceServiceTests(unittest.TestCase):
    def test_owned_video_candidate_without_metadata_is_invalidated_once_after_deadline(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            def observe_candidate_health(self, *_: object, **__: object):
                from media_interlock.fence.model import QbittorrentHealthObservation
                return QbittorrentHealthObservation("observed", metadata_known=False, downloaded_bytes=0, availability=0.0, peers=0)
            def pause(self, _: str) -> bool: events.append("pause"); return True
            def delete_owned_incomplete(self, *_: object, **__: object) -> bool: events.append("delete"); return True

        class Arr:
            def mark_history_failed(self, history_id: int) -> bool:
                self_outer.assertEqual(12, history_id)
                events.append("blocklist")
                return True

        self_outer = self
        state = FenceState(FencePolicy(1_000, 1))
        bound(state)
        state.reservation(OPERATION_ID).external_history_id = 12
        policy = VideoCandidateHealthConfig(60, 3_600, 43_200, 3, 1_800, 2.0, 21_600)
        service = FenceService(
            state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE},
            observers={"radarr": Arr()}, video_candidate_health=policy,
        )

        self.assertEqual((), service.poll_video_candidate_health(now=100))
        self.assertEqual((), service.poll_video_candidate_health(now=200))
        self.assertEqual((("radarr", "movie-42", 1_800),), service.poll_video_candidate_health(now=3_700))
        self.assertEqual("invalidated", state.video_candidate(OPERATION_ID)["status"])
        self.assertEqual(["pause", "blocklist", "delete"], [event for event in events if event != "save"])
        self.assertEqual((), service.poll_video_candidate_health(now=4_000))

    def test_post_pnr_recovery_from_durable_intent_claims_without_resuming(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            def observe_existing_stopped(self, *_: object, **__: object) -> QbittorrentObservation:
                events.append("observe")
                return QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, *_: object) -> bool:
                events.append("tag")
                return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation:
                events.append("read-back")
                return QbittorrentObservation("observed", 400)
            def resume(self, *_: object) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(1_000, 1))
        adoption = PostPnrAdoptionIntent(OPERATION_ID, "radarr", 7, "42", HASH, CATEGORY, "/downloads/radarr", 400, 8)
        self.assertTrue(state.adopt_post_pnr(adoption, qbittorrent_ready=True).admitted)

        FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}).recover()

        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        self.assertEqual(["observe", "tag", "read-back"], [event for event in events if event != "save"])

    def test_post_pnr_recovery_reads_back_a_crashed_tag_intent_without_resuming(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation:
                events.append("read-back")
                return QbittorrentObservation("observed", 400)
            def resume(self, *_: object) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(1_000, 1))
        adoption = PostPnrAdoptionIntent(OPERATION_ID, "radarr", 7, "42", HASH, CATEGORY, "/downloads/radarr", 400, 8)
        self.assertTrue(state.adopt_post_pnr(adoption, qbittorrent_ready=True).admitted)
        state.request_tag(OPERATION_ID)  # persisted intent survived a crash after the tag effect

        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})
        service.recover()

        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        self.assertEqual([], [event for event in events if event == "resume"])
        receipt = service.post_pnr_receipt(OPERATION_ID)
        assert receipt is not None
        self.assertEqual("adopted", receipt.body["state"])

    def test_freeze_pauses_only_the_exact_terminal_owned_hash_under_the_lease(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Lease:
            def acquire(self):
                class Held:
                    def __enter__(self): events.append("lease-enter")
                    def __exit__(self, *_: object): events.append("lease-exit")
                return Held()

        class Qbittorrent:
            active = True
            def observe_active(self, torrent_hash: str, *_: object, **__: object) -> QbittorrentActivityObservation:
                events.append(f"observe:{torrent_hash}")
                return QbittorrentActivityObservation("observed", self.active)
            def pause(self, torrent_hash: str) -> bool:
                events.append(f"pause:{torrent_hash}")
                self.active = False
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        state.complete(OPERATION_ID)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}, lease=Lease())

        self.assertTrue(service.freeze(OPERATION_ID))
        self.assertEqual(ReservationState.QBITTORRENT_FROZEN, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"pause:{HASH}"], [event for event in events if event.startswith("pause:")])
        self.assertLess(events.index("lease-enter"), events.index(f"observe:{HASH}"))

    def test_completed_terminal_is_reoffered_after_freeze_until_custody(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            active = True
            def terminal_observed(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", True)
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", self.active)
            def pause(self, *_: object) -> bool: self.active = False; return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})

        terminal, = service.pending_terminals()
        self.assertEqual("terminal_acquisition", terminal.kind)
        self.assertTrue(service.freeze(OPERATION_ID))
        self.assertEqual(terminal, service.pending_terminals()[0])
        self.assertTrue(service.accept_custody(custody_receipt(OPERATION_ID, terminal.body["fence_reservation_id"], "publisher-r-1")))
        self.assertEqual((), service.pending_terminals())

    def test_recovery_reestablishes_a_crashed_freeze_intent_without_touching_a_foreign_hash(self) -> None:
        calls: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            active = True
            def observe_active(self, torrent_hash: str, *_: object, **__: object) -> QbittorrentActivityObservation:
                calls.append(f"observe:{torrent_hash}")
                return QbittorrentActivityObservation("observed", self.active)
            def pause(self, torrent_hash: str) -> bool:
                calls.append(f"pause:{torrent_hash}")
                self.active = False
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        state.complete(OPERATION_ID)
        state.request_freeze(OPERATION_ID)  # durable intent survived a crash before the pause effect

        FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}).recover()

        self.assertEqual(ReservationState.QBITTORRENT_FROZEN, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"pause:{HASH}"], [call for call in calls if call.startswith("pause:")])

    def test_quiescence_pauses_only_the_exact_active_owned_hash_then_rechecks_before_resume(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            active = True
            def ready(self) -> bool: return True
            def observe_active(self, torrent_hash: str, reservation_id: str, *_: object, **__: object) -> QbittorrentActivityObservation:
                events.append(f"observe:{torrent_hash}:{reservation_id}")
                return QbittorrentActivityObservation("observed", self.active)
            def pause(self, torrent_hash: str) -> bool: events.append(f"pause:{torrent_hash}"); self.active = False; return True
            def resume(self, torrent_hash: str) -> bool: events.append(f"resume:{torrent_hash}"); self.active = True; return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})

        self.assertTrue(service.quiesce(enabled=True))
        self.assertTrue(state.quiescing)
        self.assertEqual(ReservationState.QBITTORRENT_PAUSED, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"pause:{HASH}"], [event for event in events if event.startswith("pause:")])

        self.assertTrue(service.quiesce(enabled=False))
        self.assertFalse(state.quiescing)
        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"resume:{HASH}"], [event for event in events if event.startswith("resume:")])

    def test_quiescence_never_recovers_a_preexisting_resume_intent(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            def resume(self, _: str) -> bool: events.append("resume"); return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", False)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID); state.mark_qbittorrent_tagged(OPERATION_ID, observed_bytes=400)
        state.request_resume(OPERATION_ID); state.begin_quiescence()
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})

        service.recover()

        self.assertEqual([], events)
        self.assertEqual(ReservationState.RESUME_INTENT_RECORDED, state.reservation(OPERATION_ID).state)

    def test_quiescence_exit_keeps_owned_work_paused_when_source_readiness_is_lost(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            active = True
            def ready(self) -> bool: return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation:
                return QbittorrentActivityObservation("observed", self.active)
            def pause(self, _: str) -> bool: self.active = False; return True
            def resume(self, _: str) -> bool: events.append("resume"); self.active = True; return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        bound(state)
        service = FenceService(
            state,
            Store(),
            Qbittorrent(),
            prowlarr=None,
            sources={"radarr": SOURCE},
            resume_ready=lambda: False,
        )

        self.assertTrue(service.quiesce(enabled=True))
        self.assertFalse(service.quiesce(enabled=False))
        self.assertEqual(ReservationState.QBITTORRENT_PAUSED, state.reservation(OPERATION_ID).state)
        self.assertEqual([], events)

    def test_physical_headroom_inhibits_admission_before_any_reservation(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None: self.fail("headroom rejection must not persist")

        class Qbittorrent:
            def ready(self) -> bool: return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        source = FenceSource(CATEGORY, Path("/downloads/radarr"), 7, "media", "media", "media")
        headroom = PhysicalHeadroom({"media": HeadroomPool("media", 100, 10)}, free_bytes=lambda _: 1_309)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": source}, headroom=headroom)

        decision = service.pre_admit(intent(), publisher_ready=True)

        self.assertFalse(decision.admitted)
        self.assertEqual("physical_headroom", decision.reason)
        with self.assertRaises(KeyError):
            state.reservation(OPERATION_ID)

    def test_bind_tags_then_resumes_and_confirms_the_source_category(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("save")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def observe_existing_stopped(self, torrent_hash: str, category: str, *, save_path: Path) -> QbittorrentObservation:
                events.append(f"stopped:{torrent_hash}:{category}")
                return QbittorrentObservation("observed", 400)

            def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool:
                events.append(f"tag:{torrent_hash}:{reservation_id}")
                return True

            def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str, *, save_path: Path) -> QbittorrentObservation:
                events.append(f"tagged:{torrent_hash}:{category}:{reservation_id}")
                return QbittorrentObservation("observed", 400)

            def resume(self, torrent_hash: str) -> bool:
                events.append(f"resume:{torrent_hash}")
                return True

            def observe_active(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation:
                events.append(f"active:{torrent_hash}:{reservation_id}:{category}")
                return QbittorrentActivityObservation("observed", True)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertTrue(service.bind_grab(OPERATION_ID, HASH.upper(), HASH))

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual(HASH.upper(), state.reservation(OPERATION_ID).download_id)
        self.assertEqual(f"active:{HASH}:fence:{OPERATION_ID}:{CATEGORY}", events[-2])
        before_retry = list(events)
        self.assertTrue(service.bind_grab(OPERATION_ID, HASH.upper(), HASH))
        self.assertEqual(before_retry, events)

    def test_capacity_overrun_after_tag_never_resumes_the_stopped_torrent(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def observe_existing_stopped(self, _: str, __: str, *, save_path: Path) -> QbittorrentObservation:
                return QbittorrentObservation("observed", 400)

            def apply_reservation_tag(self, _: str, __: str) -> bool:
                return True

            def observe_tagged_stopped(self, _: str, __: str, ___: str, *, save_path: Path) -> QbittorrentObservation:
                return QbittorrentObservation("observed", 401)

            def resume(self, _: str) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(capacity_bytes=400, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertFalse(service.bind_grab(OPERATION_ID, HASH, HASH))

        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        self.assertEqual([], events)

    def test_busy_shared_mutation_lease_prevents_any_qbittorrent_effect(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            active = False
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, *_: object) -> bool: events.append("tag"); return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def resume(self, *_: object) -> bool: events.append("resume"); return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", True)

        class BusyLease:
            def acquire(self) -> object:
                raise RuntimeError("busy")

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}, lease=BusyLease())
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertFalse(service.bind_grab(OPERATION_ID, HASH, HASH))
        self.assertEqual([], events)

    def test_tag_observation_is_persisted_before_releasing_the_shared_lease(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, state: FenceState) -> None:
                events.append(f"save:{state.reservation(OPERATION_ID).state.value}")

        class Lease:
            def acquire(self):
                class Held:
                    def __enter__(self):
                        events.append("lease-enter")

                    def __exit__(self, *_: object) -> None:
                        events.append("lease-exit")

                return Held()

        class Qbittorrent:
            active = False
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, *_: object) -> bool: events.append("tag"); return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def resume(self, *_: object) -> bool: return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", True)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}, lease=Lease())
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertTrue(service.bind_grab(OPERATION_ID, HASH, HASH))

        self.assertLess(events.index("save:qbittorrent_stopped"), events.index("lease-exit"))

    def test_pre_admitted_magnet_can_fetch_metadata_under_its_reserved_size(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            tagged = False
            active = False
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("metadata_pending")
            def apply_reservation_tag(self, *_: object) -> bool: self.tagged = True; return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("metadata_pending")
            def resume(self, *_: object) -> bool: self.active = True; return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", self.active)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertTrue(service.bind_grab(OPERATION_ID, HASH, HASH))
        self.assertEqual(400, state.reservation(OPERATION_ID).bytes_reserved)
        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)

    def test_external_observer_baselines_then_persists_and_adopts_one_stopped_grab(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            active = False
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, torrent_hash: str, *_: object, **__: object) -> QbittorrentObservation:
                events.append(f"stopped:{torrent_hash}")
                return QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, torrent_hash: str, _: str) -> bool: events.append(f"tag:{torrent_hash}"); return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def resume(self, torrent_hash: str) -> bool: events.append(f"resume:{torrent_hash}"); self.active = True; return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", self.active)

        grab = ArrExternalGrab("42", HASH.upper(), HASH, 400, 8)

        class Observer:
            calls: list[int] = []
            def history_watermark(self) -> int: return 7
            def external_grabs_after(self, watermark: int, **_: object) -> ArrExternalObservation:
                self.calls.append(watermark)
                return ArrExternalObservation(8, (grab,))

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        observer = Observer()
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": FenceSource(CATEGORY, Path("/downloads/radarr"), 7)}, observers={"radarr": observer})

        self.assertTrue(service.poll_external(publisher_ready=True))
        self.assertEqual((), tuple(observer.calls))
        self.assertEqual([], [event for event in events if event.startswith(("tag:", "resume:"))])
        self.assertEqual(7, state.watermark("radarr"))

        self.assertTrue(service.poll_external(publisher_ready=True))
        self.assertEqual((7,), tuple(observer.calls))
        reservation = next(iter(state.records()))
        self.assertEqual(HASH, reservation["torrent_hash"])
        self.assertIsNotNone(reservation["observation_fingerprint"])
        self.assertEqual(4, uuid.UUID(str(reservation["operation_id"])).version)
        self.assertEqual(8, state.watermark("radarr"))
        self.assertEqual([f"tag:{HASH}", f"resume:{HASH}"], [event for event in events if event.startswith(("tag:", "resume:"))])

    def test_external_observer_resumes_metadata_pending_magnet_under_arr_history_size(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None: pass

        class Qbittorrent:
            active = False
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("metadata_pending")
            def apply_reservation_tag(self, *_: object) -> bool: return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("metadata_pending")
            def resume(self, *_: object) -> bool: self.active = True; return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", self.active)

        grab = ArrExternalGrab("42", HASH.upper(), HASH, 400, 8)

        class Observer:
            def history_watermark(self) -> int: return 7
            def external_grabs_after(self, watermark: int, **_: object) -> ArrExternalObservation:
                return ArrExternalObservation(8, (grab,))

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(
            state,
            Store(),
            Qbittorrent(),
            prowlarr=None,
            sources={"sonarr": FenceSource(CATEGORY, Path("/downloads/sonarr"), 7)},
            observers={"sonarr": Observer()},
        )

        self.assertTrue(service.poll_external(publisher_ready=True))
        self.assertTrue(service.poll_external(publisher_ready=True))
        reservation = state.records()[0]
        self.assertEqual(400, reservation["bytes_reserved"])
        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE.value, reservation["state"])

    def test_external_adoption_failure_keeps_watermark_and_reobserves_the_durable_intent(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None: events.append("save")

        class Qbittorrent:
            active = False
            fail = True
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, torrent_hash: str, *_: object, **__: object) -> QbittorrentObservation:
                events.append(f"stopped:{torrent_hash}")
                return QbittorrentObservation("unknown") if self.fail else QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, torrent_hash: str, _: str) -> bool: events.append(f"tag:{torrent_hash}"); return True
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation: return QbittorrentObservation("observed", 400)
            def resume(self, torrent_hash: str) -> bool: events.append(f"resume:{torrent_hash}"); self.active = True; return True
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation: return QbittorrentActivityObservation("observed", self.active)

        grab = ArrExternalGrab("42", HASH, HASH, 400, 8)
        class Observer:
            def history_watermark(self) -> int: return 7
            def external_grabs_after(self, watermark: int, **_: object) -> ArrExternalObservation:
                return ArrExternalObservation(8, (grab,))

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        qbittorrent = Qbittorrent()
        service = FenceService(state, Store(), qbittorrent, prowlarr=None, sources={"radarr": FenceSource(CATEGORY, Path("/downloads/radarr"), 7)}, observers={"radarr": Observer()})

        self.assertTrue(service.poll_external(publisher_ready=True))
        self.assertFalse(service.poll_external(publisher_ready=True))
        self.assertEqual(7, state.watermark("radarr"))
        self.assertEqual(ReservationState.PRE_ADMITTED.value, state.records()[0]["state"])
        self.assertEqual([], [event for event in events if event.startswith(("tag:", "resume:"))])

        qbittorrent.fail = False
        self.assertTrue(service.poll_external(publisher_ready=True))
        self.assertEqual(8, state.watermark("radarr"))
        self.assertEqual([f"tag:{HASH}", f"resume:{HASH}"], [event for event in events if event.startswith(("tag:", "resume:"))])

    def test_restart_from_tag_intent_observes_before_any_resume(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("save")

        class Qbittorrent:
            def observe_tagged_stopped(self, _: str, category: str, __: str, *, save_path: Path) -> QbittorrentObservation:
                events.append(f"observe-tag:{category}")
                return QbittorrentObservation("observed", 400)

            def observe_active(self, _: str, __: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation:
                events.append(f"observe-active:{category}")
                return QbittorrentActivityObservation("observed", True)

            def resume(self, _: str) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE})

        service.recover()

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"observe-tag:{CATEGORY}", "save", "save", f"observe-active:{CATEGORY}", "save"], events)

    def test_restart_from_tag_intent_holds_the_lease_through_observation_and_persistence(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, state: FenceState) -> None:
                events.append(f"save:{state.reservation(OPERATION_ID).state.value}")

        class Lease:
            def acquire(self):
                class Held:
                    def __enter__(self): events.append("lease-enter")
                    def __exit__(self, *_: object) -> None: events.append("lease-exit")
                return Held()

        class Qbittorrent:
            def observe_tagged_stopped(self, *_: object, **__: object) -> QbittorrentObservation:
                events.append("observe-tag")
                return QbittorrentObservation("observed", 400)
            def observe_active(self, *_: object, **__: object) -> QbittorrentActivityObservation:
                return QbittorrentActivityObservation("observed", True)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, sources={"radarr": SOURCE}, lease=Lease())

        service.recover()

        self.assertLess(events.index("lease-enter"), events.index("observe-tag"))
        self.assertLess(events.index("save:qbittorrent_stopped"), events.index("lease-exit"))


class FenceStoreTests(unittest.TestCase):
    def test_restart_restores_pre_admission_without_a_download_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FenceStore.open(Path(directory) / "fence")
            state = store.load(FencePolicy(capacity_bytes=1_000, max_inflight=1))
            assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
            store.save(state)
            store.close()
            reopened = FenceStore.open(Path(directory) / "fence")
            self.addCleanup(reopened.close)

            recovered = reopened.load(FencePolicy(capacity_bytes=1_000, max_inflight=1))

        self.assertEqual(ReservationState.PRE_ADMITTED, recovered.reservation(OPERATION_ID).state)
