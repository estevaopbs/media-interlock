from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import ContractError, custody_receipt
from media_interlock.fence.model import FencePolicy, FenceState, PreAdmissionIntent, QbittorrentActivityObservation, QbittorrentObservation, ReservationState
from media_interlock.fence.service import FenceService
from media_interlock.fence.store import FenceStore


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))
HASH = "a" * 40
CATEGORY = "media-interlock-radarr"


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


class FenceServiceTests(unittest.TestCase):
    def test_bind_tags_then_resumes_and_confirms_the_source_category(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("save")

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def observe_existing_stopped(self, torrent_hash: str, category: str) -> QbittorrentObservation:
                events.append(f"stopped:{torrent_hash}:{category}")
                return QbittorrentObservation("observed", 400)

            def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool:
                events.append(f"tag:{torrent_hash}:{reservation_id}")
                return True

            def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str) -> QbittorrentObservation:
                events.append(f"tagged:{torrent_hash}:{category}:{reservation_id}")
                return QbittorrentObservation("observed", 400)

            def resume(self, torrent_hash: str) -> bool:
                events.append(f"resume:{torrent_hash}")
                return True

            def observe_active(self, torrent_hash: str, reservation_id: str, category: str) -> QbittorrentActivityObservation:
                events.append(f"active:{torrent_hash}:{reservation_id}:{category}")
                return QbittorrentActivityObservation("observed", True)

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, categories={"radarr": CATEGORY})
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

            def observe_existing_stopped(self, _: str, __: str) -> QbittorrentObservation:
                return QbittorrentObservation("observed", 400)

            def apply_reservation_tag(self, _: str, __: str) -> bool:
                return True

            def observe_tagged_stopped(self, _: str, __: str, ___: str) -> QbittorrentObservation:
                return QbittorrentObservation("observed", 401)

            def resume(self, _: str) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(capacity_bytes=400, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, categories={"radarr": CATEGORY})
        self.assertTrue(service.pre_admit(intent(), publisher_ready=True).admitted)

        self.assertFalse(service.bind_grab(OPERATION_ID, HASH, HASH))

        self.assertEqual(ReservationState.QBITTORRENT_STOPPED, state.reservation(OPERATION_ID).state)
        self.assertEqual([], events)

    def test_restart_from_tag_intent_observes_before_any_resume(self) -> None:
        events: list[str] = []

        class Store:
            def save(self, _: FenceState) -> None:
                events.append("save")

        class Qbittorrent:
            def observe_tagged_stopped(self, _: str, category: str, __: str) -> QbittorrentObservation:
                events.append(f"observe-tag:{category}")
                return QbittorrentObservation("observed", 400)

            def observe_active(self, _: str, __: str, category: str) -> QbittorrentActivityObservation:
                events.append(f"observe-active:{category}")
                return QbittorrentActivityObservation("observed", True)

            def resume(self, _: str) -> bool:
                events.append("resume")
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        assert state.pre_admit(intent(), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True).admitted
        state.bind_observed_grab(OPERATION_ID, download_id=HASH, torrent_hash=HASH)
        state.request_tag(OPERATION_ID)
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None, categories={"radarr": CATEGORY})

        service.recover()

        self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(OPERATION_ID).state)
        self.assertEqual([f"observe-tag:{CATEGORY}", "save", "save", f"observe-active:{CATEGORY}", "save"], events)


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
