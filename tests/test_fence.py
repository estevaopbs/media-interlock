from __future__ import annotations

import hashlib
import uuid
import unittest
import tempfile
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import ContractError, custody_receipt
from media_interlock.fence.model import AcquisitionIntent, FencePolicy, FenceState, PreAdmissionIntent, ReservationState
from media_interlock.fence.store import FenceStore
from media_interlock.fence.service import FenceService


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))
FINGERPRINT = hashlib.sha256(b"magnet:?xt=urn:btih:fixture").hexdigest()


def active(state: FenceState, operation_id: str = OPERATION_ID) -> None:
    state.mark_qbittorrent_stopped(operation_id, "a" * 40)
    state.request_resume(operation_id)
    state.mark_qbittorrent_active(operation_id)


class FenceStateTests(unittest.TestCase):
    def test_pre_admission_binds_one_later_exact_arr_grab_without_locator(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = PreAdmissionIntent(OPERATION_ID, "radarr", "42", "a" * 64, 400, "2026-08-08T12:00:00Z")

        admitted = state.pre_admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        self.assertTrue(admitted.admitted)
        reservation = state.reservation(OPERATION_ID)
        self.assertEqual(ReservationState.PRE_ADMITTED, reservation.state)
        self.assertEqual("a" * 64, reservation.selector_fingerprint)
        self.assertIsNone(reservation.download_id)
        state.bind_observed_grab(OPERATION_ID, download_id="0123456789abcdef0123456789abcdef01234567", torrent_hash="b" * 40)
        self.assertEqual("0123456789abcdef0123456789abcdef01234567", state.reservation(OPERATION_ID).download_id)
        with self.assertRaises(ContractError):
            state.bind_observed_grab(OPERATION_ID, download_id="0123456789abcdef0123456789abcdef01234568", torrent_hash="c" * 40)

    def test_pre_admission_and_bound_grab_survive_durable_record_round_trip(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.pre_admit(PreAdmissionIntent(OPERATION_ID, "sonarr", "42", "a" * 64, 400, "watermark"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.bind_observed_grab(OPERATION_ID, download_id="download-42", torrent_hash="b" * 40)

        restored = FenceState.from_records(FencePolicy(capacity_bytes=1_000, max_inflight=1), state.records())
        reservation = restored.reservation(OPERATION_ID)
        self.assertEqual(ReservationState.GRAB_BOUND, reservation.state)
        self.assertEqual("a" * 64, reservation.selector_fingerprint)
        self.assertEqual("watermark", reservation.watermark)
        self.assertEqual("download-42", reservation.download_id)

    def test_bound_grab_requires_durable_tag_intent_and_tag_observation_before_resume(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.pre_admit(PreAdmissionIntent(OPERATION_ID, "radarr", "42", "a" * 64, 400, "watermark"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.bind_observed_grab(OPERATION_ID, download_id="download-42", torrent_hash="b" * 40)

        state.request_tag(OPERATION_ID)
        self.assertEqual(ReservationState.TAG_INTENT_RECORDED, state.reservation(OPERATION_ID).state)
        with self.assertRaises(ContractError):
            state.request_resume(OPERATION_ID)
        state.mark_qbittorrent_tagged(OPERATION_ID, observed_bytes=400)

        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        state.request_resume(OPERATION_ID)
    def test_admission_records_reservation_before_a_qbittorrent_effect(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)

        decision = state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)

        self.assertTrue(decision.admitted)
        self.assertEqual(ReservationState.INTENT_RECORDED, state.reservation(OPERATION_ID).state)
        self.assertEqual(400, state.reserved_bytes)

    def test_capacity_unknown_dependency_or_publisher_backpressure_inhibits_without_effect(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=500, max_inflight=1))
        first = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        second = AcquisitionIntent(str(uuid.uuid4()), "sonarr", "grab-43", "episode-43", 400, FINGERPRINT)

        self.assertFalse(state.admit(first, qbittorrent_ready=False, prowlarr_ready=True, publisher_ready=True).admitted)
        self.assertTrue(state.admit(first, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted)
        self.assertFalse(state.admit(second, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted)
        self.assertFalse(state.admit(second, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=False).admitted)
        self.assertEqual(400, state.reserved_bytes)

    def test_reused_operation_id_with_different_immutable_intent_conflicts(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)

        decision = state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-other", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)

        self.assertFalse(decision.admitted)
        self.assertEqual("conflict", decision.reason)

    def test_completed_payload_retains_fence_reservation_until_exact_custody_receipt(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        active(state)

        terminal = state.complete(OPERATION_ID)
        self.assertEqual("terminal_acquisition", terminal.kind)
        self.assertEqual(400, state.reserved_bytes)
        self.assertFalse(state.accept_custody(custody_receipt(OPERATION_ID, "wrong", "publisher-r-1")))
        self.assertEqual(400, state.reserved_bytes)
        self.assertTrue(state.accept_custody(custody_receipt(OPERATION_ID, state.reservation(OPERATION_ID).reservation_id, "publisher-r-1")))
        self.assertTrue(state.accept_custody(custody_receipt(OPERATION_ID, state.reservation(OPERATION_ID).reservation_id, "publisher-r-1")))
        self.assertEqual(0, state.reserved_bytes)


class FenceStoreTests(unittest.TestCase):
    def test_restart_restores_intent_reservation_before_any_qbittorrent_effect(self) -> None:
        policy = FencePolicy(capacity_bytes=1_000, max_inflight=1)
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "fence"
            store = FenceStore.open(state_dir)
            state = store.load(policy)
            self.assertTrue(state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted)
            store.save(state)
            store.close()

            restarted = FenceStore.open(state_dir)
            self.addCleanup(restarted.close)
            recovered = restarted.load(policy)
            self.assertEqual(ReservationState.INTENT_RECORDED, recovered.reservation(OPERATION_ID).state)
            self.assertEqual(400, recovered.reserved_bytes)


class FenceServiceTests(unittest.TestCase):
    def test_observed_arr_hash_is_tagged_and_confirmed_before_resume(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("save")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def observe_existing_stopped(self, torrent_hash: str, category: str) -> int | None:
                events.append(f"observe:{torrent_hash}:{category}")
                return 400

            def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool:
                events.append(f"tag:{torrent_hash}:{reservation_id}")
                return True

            def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str) -> int | None:
                events.append(f"tagged:{torrent_hash}:{category}:{reservation_id}")
                return 400

            def resume(self, torrent_hash: str) -> bool:
                events.append(f"resume:{torrent_hash}")
                return True

            def observe_active(self, torrent_hash: str, reservation_id: str) -> bool:
                events.append(f"active:{torrent_hash}:{reservation_id}")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, categories={"radarr": "media-interlock-radarr"})
        service.pre_admit(PreAdmissionIntent(OPERATION_ID, "radarr", "42", "a" * 64, 400, "7"), publisher_ready=True)

        self.assertTrue(service.bind_grab(OPERATION_ID, "a" * 40))

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual(["save", "observe:" + "a" * 40 + ":media-interlock-radarr", "save", "save", "tag:" + "a" * 40 + ":fence:" + OPERATION_ID, "tagged:" + "a" * 40 + ":media-interlock-radarr:fence:" + OPERATION_ID, "save", "save", "resume:" + "a" * 40, "active:" + "a" * 40 + ":fence:" + OPERATION_ID, "save"], events)

    def test_intent_is_saved_before_stopped_qbittorrent_effect_and_observation(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("saved")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def add_stopped(self, _: str, __: str) -> tuple[str, int]:
                events.append("qbittorrent")
                return "a" * 40, 400

            def resume(self, _: str) -> bool:
                events.append("resume")
                return True

            def observe_active(self, _: str, __: str) -> bool:
                events.append("observe_active")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)

        decision = service.admit(intent, source="magnet:?xt=urn:btih:fixture", publisher_ready=True)

        self.assertTrue(decision.admitted)
        self.assertEqual(["saved", "qbittorrent", "saved", "saved", "resume", "observe_active", "saved"], events)
        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)

    def test_recovery_reobserves_without_repeating_an_uncertain_effect(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("saved")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def add_stopped(self, _: str, __: str) -> bool:
                events.append("add")
                return True

            def observe_stopped(self, _: str) -> tuple[str, int] | None:
                events.append("observe")
                return None

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)

        service.recover()

        self.assertEqual(["observe"], events)
        self.assertEqual(ReservationState.INTENT_RECORDED, state.reservation(OPERATION_ID).state)

    def test_recovery_adopts_a_bound_arr_grab_without_repeating_arr_effect(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def observe_existing_stopped(self, _: str, __: str) -> int | None:
                return 400

            def apply_reservation_tag(self, _: str, __: str) -> bool:
                return True

            def observe_tagged_stopped(self, _: str, __: str, ___: str) -> int | None:
                return 400

            def resume(self, _: str) -> bool:
                return True

            def observe_active(self, _: str, __: str) -> bool:
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.pre_admit(PreAdmissionIntent(OPERATION_ID, "radarr", "42", "a" * 64, 400, "7"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.bind_observed_grab(OPERATION_ID, download_id="a" * 40, torrent_hash="a" * 40)

        FenceService(state, Store(), Qbittorrent(), prowlarr=None, categories={"radarr": "media-interlock-radarr"}).recover()

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)

    def test_recovery_resumes_only_a_durably_recorded_exact_stopped_hash(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("saved")

        class Qbittorrent:
            def observe_active(self, _: str, __: str) -> bool:
                events.append("observe")
                return False if events.count("observe") == 1 else True

            def resume(self, _: str) -> bool:
                events.append("start")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.mark_qbittorrent_stopped(OPERATION_ID, "a" * 40)

        FenceService(state, Store(), Qbittorrent(), prowlarr=None).recover()

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual(["saved", "observe", "start", "observe", "saved"], events)

    def test_terminal_observation_is_replayable_until_custody(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def terminal_observed(self, _: str, __: str) -> bool:
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        active(state)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)

        first = service.observe(OPERATION_ID)
        second = service.observe(OPERATION_ID)

        self.assertEqual(first, second)

    def test_failed_initial_save_does_not_change_live_admission_state(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                raise OSError("full")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)

        with self.assertRaises(OSError):
            service.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), source="magnet:?xt=urn:btih:fixture", publisher_ready=True)
        self.assertEqual(0, state.reserved_bytes)

    def test_observed_torrent_size_expands_hold_and_inhibits_resume(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def add_stopped(self, _: str, __: str) -> tuple[str, int]:
                return "a" * 40, 1_001

            def resume(self, _: str) -> bool:
                self.fail("oversized torrent must not resume")

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        decision = FenceService(state, Store(), Qbittorrent(), prowlarr=None).admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 1, FINGERPRINT), source="magnet:?xt=urn:btih:fixture", publisher_ready=True)

        self.assertFalse(decision.admitted)
        self.assertEqual("capacity_exhausted_after_observation", decision.reason)
        self.assertEqual(1_001, state.reserved_bytes)

    def test_unresolved_effect_inhibits_other_operation_ids(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=2))
        first = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        second = AcquisitionIntent(str(uuid.uuid4()), "sonarr", "grab-43", "episode-43", 400, FINGERPRINT)
        self.assertTrue(state.admit(first, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted)

        decision = state.admit(second, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)

        self.assertFalse(decision.admitted)
        self.assertEqual("recovering", decision.reason)

    def test_stopped_idempotent_retry_runs_recovery_to_active(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            observations = 0

            def ready(self) -> bool:
                return True

            def observe_active(self, _: str, __: str) -> bool:
                self.observations += 1
                return self.observations > 1

            def resume(self, _: str) -> bool:
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.mark_qbittorrent_stopped(OPERATION_ID, "a" * 40)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)

        decision = service.admit(intent, source="magnet:?xt=urn:btih:fixture", publisher_ready=True)

        self.assertTrue(decision.admitted)
        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)

    def test_oversized_observed_hold_restarts_and_original_intent_replays(self) -> None:
        policy = FencePolicy(capacity_bytes=1_000, max_inflight=1)
        state = FenceState(policy)
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 1, FINGERPRINT)
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.mark_qbittorrent_stopped(OPERATION_ID, "a" * 40)
        self.assertFalse(state.account_observed_bytes(OPERATION_ID, 1_001))

        restored = FenceState.from_records(policy, state.records())
        replay = restored.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)

        self.assertEqual(1_001, restored.reserved_bytes)
        self.assertEqual("recovering", replay.reason)

    def test_terminal_and_custody_transitions_are_durable_before_handoff(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("saved")

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        active(state)
        service = FenceService(state, Store(), object(), prowlarr=None)

        terminal = service.complete(OPERATION_ID)
        accepted = service.accept_custody(custody_receipt(OPERATION_ID, state.reservation(OPERATION_ID).reservation_id, "publisher-r-1"))

        self.assertEqual("terminal_acquisition", terminal.kind)
        self.assertTrue(accepted)
        self.assertEqual(["saved", "saved"], events)
        self.assertEqual(ReservationState.RELEASED, state.reservation(OPERATION_ID).state)

    def test_adapter_exception_inhibits_admission_without_creating_reservation(self) -> None:
        class Qbittorrent:
            def ready(self) -> bool:
                raise OSError("unavailable")

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, object(), Qbittorrent(), prowlarr=None)
        intent = AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT)

        self.assertFalse(service.admit(intent, source="magnet:?fixture", publisher_ready=True).admitted)
        self.assertEqual(0, state.reserved_bytes)

    def test_failed_custody_save_retains_fence_reservation(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                raise OSError("full")

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        active(state)
        state.complete(OPERATION_ID)
        service = FenceService(state, Store(), object(), prowlarr=None)

        with self.assertRaises(OSError):
            service.accept_custody(custody_receipt(OPERATION_ID, state.reservation(OPERATION_ID).reservation_id, "publisher-r-1"))
        self.assertEqual(ReservationState.TERMINAL, state.reservation(OPERATION_ID).state)
        self.assertEqual(400, state.reserved_bytes)

    def test_exact_terminal_observation_is_persisted_before_delivery(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("saved")

        class Qbittorrent:
            def terminal_observed(self, _: str, __: str) -> bool:
                events.append("observed")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        state.admit(AcquisitionIntent(OPERATION_ID, "radarr", "grab-42", "movie-42", 400, FINGERPRINT), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        active(state)
        terminal = FenceService(state, Store(), Qbittorrent(), prowlarr=None).observe(OPERATION_ID)

        self.assertEqual(["observed", "saved"], events)
        assert terminal is not None
        self.assertEqual("terminal_acquisition", terminal.kind)
        self.assertEqual(ReservationState.TERMINAL, state.reservation(OPERATION_ID).state)
