from __future__ import annotations

import hashlib
import json
import unittest
import uuid

import _source_tree  # noqa: F401

from media_interlock.adapters.arr import ArrGrabObservation, ArrRelease
from media_interlock.adapters.lidarr import LidarrRelease
from media_interlock.fence.model import AdmissionDecision, PreAdmissionIntent
from media_interlock.reconciler.model import AttemptPolicy, ReconciliationState, SearchIntent
from media_interlock.reconciler.service import ReconcilerService, ReconcilerSource


RADARR_SOURCE = ReconcilerSource("media-interlock-radarr", 7)


class ReconcilerServiceTests(unittest.TestCase):
    def test_recovery_reclaims_a_durable_pre_post_intent_without_stranding_capacity(self) -> None:
        events: list[str] = []
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)
        operation_id = str(uuid.uuid4())
        state = ReconciliationState()
        state.record_intent(SearchIntent(operation_id, "radarr", "42", False, "checkpoint"))
        from media_interlock.reconciler.model import GrabIntent
        state.record_grab_intent(GrabIntent(operation_id, "radarr", "42", release.selector_fingerprint, 400, 7, resource))

        class Store:
            def save(self, _: ReconciliationState) -> None: events.append("save")
        class Arr:
            def stopped_qbittorrent_client(self, _: str, __: int) -> bool: events.append("client"); return True
            def grab_release(self, _: ArrRelease) -> bool: events.append("post"); return False
            def observe_grab(self, _: str, __: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                self_outer.assertEqual(7, watermark); events.append("observe"); return ArrGrabObservation("absent")
        class Fence:
            def pre_admit(self, _: PreAdmissionIntent) -> AdmissionDecision: events.append("preadmit"); return AdmissionDecision(True, "admitted")
            def bind_grab(self, _: str, __: str, ___: str) -> bool: self_outer.fail("absence cannot bind")

        self_outer = self
        result = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE}).recover(now=100)

        self.assertEqual(["pending"], result)
        self.assertTrue(state.grab_attempted(operation_id))
        self.assertEqual(["client", "preadmit", "save", "post", "observe"], events)

    def test_recovery_derives_a_missing_grab_intent_before_any_pre_admission(self) -> None:
        events: list[str] = []
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)
        operation_id = str(uuid.uuid4())
        state = ReconciliationState()
        state.record_intent(SearchIntent(operation_id, "radarr", "42", False, "checkpoint"))

        class Store:
            def save(self, _: ReconciliationState) -> None: events.append("save")
        class Arr:
            def stopped_qbittorrent_client(self, _: str, __: int) -> bool: events.append("client"); return True
            def history_watermark(self) -> int | None: events.append("watermark"); return 7
            def first_approved_release(self, _: str) -> ArrRelease | None: events.append("release"); return release
            def grab_release(self, _: ArrRelease) -> bool: events.append("post"); return False
            def observe_grab(self, _: str, __: ArrRelease, *, watermark: int) -> ArrGrabObservation: events.append("observe"); return ArrGrabObservation("absent")
        class Fence:
            def pre_admit(self, _: PreAdmissionIntent) -> AdmissionDecision: events.append("preadmit"); return AdmissionDecision(True, "admitted")
            def bind_grab(self, _: str, __: str, ___: str) -> bool: self_outer.fail("absence cannot bind")

        self_outer = self
        self.assertEqual(["pending"], ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE}).recover(now=100))
        self.assertEqual(["client", "watermark", "release", "save", "client", "preadmit", "save", "post", "observe"], events)

    def test_recovery_observes_durable_possible_grab_without_repeating_post(self) -> None:
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)
        operation_id = str(uuid.uuid4())
        state = ReconciliationState()
        state.record_intent(SearchIntent(operation_id, "radarr", "42", False, "checkpoint"))
        from media_interlock.reconciler.model import GrabIntent
        state.record_grab_intent(GrabIntent(operation_id, "radarr", "42", release.selector_fingerprint, 400, 7, resource))
        state.mark_grab_attempted(operation_id)
        calls: list[str] = []

        class Store:
            def save(self, _: ReconciliationState) -> None:
                calls.append("save")

        class Arr:
            def observe_grab(self, entity_id: str, selected: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                self_outer.assertEqual(("42", release, 7), (entity_id, selected, watermark))
                calls.append("observe")
                return ArrGrabObservation("observed", "A" * 40, "a" * 40)

        class Fence:
            def bind_grab(self, received_operation: str, download_id: str, torrent_hash: str) -> bool:
                self_outer.assertEqual((operation_id, "A" * 40, "a" * 40), (received_operation, download_id, torrent_hash))
                calls.append("bind")
                return True

        self_outer = self
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE})

        self.assertEqual(["bound"], service.recover(now=100))
        self.assertEqual(["observe", "bind", "save"], calls)
        self.assertFalse(state.eligible("radarr", "42", AttemptPolicy(0, 1), now=101))

    def test_lost_grab_response_is_observed_on_restart_without_a_second_post(self) -> None:
        events: list[str] = []
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)

        class Store:
            def save(self, _: ReconciliationState) -> None:
                events.append("save")

        class Arr:
            def stopped_qbittorrent_client(self, category: str, client_id: int) -> bool:
                events.append(f"client:{category}")
                return True

            def history_watermark(self) -> int | None:
                events.append("watermark")
                return 7

            def first_approved_release(self, entity_id: str) -> ArrRelease | None:
                events.append(f"release:{entity_id}")
                return release

            def grab_release(self, selected: ArrRelease) -> bool:
                self_outer.assertEqual(release, selected)
                events.append("post")
                return False

            def observe_grab(self, entity_id: str, selected: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                self_outer.assertEqual(("42", release, 7), (entity_id, selected, watermark))
                events.append("observe")
                return ArrGrabObservation("absent")

        class Fence:
            def pre_admit(self, intent: PreAdmissionIntent) -> AdmissionDecision:
                self_outer.assertEqual(("radarr", "42", release.selector_fingerprint, 400, "7"), (intent.source, intent.media_id, intent.selector_fingerprint, intent.expected_bytes, intent.watermark))
                events.append("preadmit")
                return AdmissionDecision(True, "admitted")

            def bind_grab(self, _: str, __: str, ___: str) -> bool:
                self_outer.fail("absent observation must not bind a grab")

        self_outer = self
        state = ReconciliationState()
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE})
        intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "checkpoint")

        self.assertEqual("pending", service.execute(intent, now=100))
        self.assertEqual(["save", "client:media-interlock-radarr", "watermark", "release:42", "save", "preadmit", "save", "post", "observe"], events)

        self.assertEqual("pending", service.execute(intent, now=101))
        self.assertEqual(1, events.count("post"))
        self.assertEqual(2, events.count("observe"))

    def test_selected_release_is_persisted_without_a_second_arr_search(self) -> None:
        events: list[str] = []
        resource = {"approved": True, "protocol": "torrent", "guid": "selected", "title": "fixture.selected", "size": 400, "downloadUrl": "https://indexer.invalid/selected"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)

        class Store:
            def save(self, _: ReconciliationState) -> None: events.append("save")
        class Arr:
            def stopped_qbittorrent_client(self, _: str, __: int) -> bool: events.append("client"); return True
            def history_watermark(self) -> int | None: events.append("watermark"); return 7
            def first_approved_release(self, _: str) -> ArrRelease | None: raise AssertionError("must not search again")
            def grab_release(self, selected: ArrRelease) -> bool: self_outer.assertEqual(release, selected); events.append("post"); return True
            def observe_grab(self, _: str, selected: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                self_outer.assertEqual((release, 7), (selected, watermark)); events.append("observe"); return ArrGrabObservation("absent")
        class Fence:
            def pre_admit(self, _: PreAdmissionIntent) -> AdmissionDecision: events.append("preadmit"); return AdmissionDecision(True, "admitted")
            def bind_grab(self, *_: object) -> bool: raise AssertionError

        self_outer = self
        state = ReconciliationState()
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE})
        intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "auto-v1:file-1:0")

        self.assertEqual("pending", service.execute_selected(intent, release, now=100))
        self.assertEqual(["client", "watermark", "save", "client", "preadmit", "save", "post", "observe"], events)

    def test_failed_selected_release_precondition_does_not_strand_an_intent(self) -> None:
        resource = {"approved": True, "protocol": "torrent", "guid": "selected", "title": "fixture.selected", "size": 400, "downloadUrl": "https://indexer.invalid/selected"}
        release = ArrRelease(resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)

        class Store:
            def save(self, _: ReconciliationState) -> None: raise AssertionError
        class Arr:
            def stopped_qbittorrent_client(self, _: str, __: int) -> bool: return False
        class Fence: pass

        state = ReconciliationState()
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": RADARR_SOURCE})
        intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "auto-v1:file-1:0")

        self.assertEqual("inhibited", service.execute_selected(intent, release, now=100))
        self.assertEqual((), state.intents())

    def test_selected_lidarr_release_is_recovered_from_its_sealed_resource_without_reselection(self) -> None:
        resource = {
            "approved": True, "downloadAllowed": True, "protocol": "torrent", "guid": "music-42",
            "title": "fixture.album", "size": 400, "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40,
            "albumId": 42, "seeders": 2, "indexer": "reliable", "infoHash": "a" * 40,
        }
        release = LidarrRelease(
            resource, hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            400, "42", 2, "reliable", "a" * 40, 0, (),
        )
        events: list[str] = []

        class Store:
            def save(self, _: ReconciliationState) -> None: events.append("save")
        class Lidarr:
            def stopped_qbittorrent_client(self, _: str, __: int) -> bool: events.append("client"); return True
            def history_watermark(self) -> int | None: events.append("watermark"); return 7
            def first_approved_release(self, _: str) -> ArrRelease | None: raise AssertionError("music must not reselect")
            def release_from_record(self, raw: dict[str, object], album_id: str) -> LidarrRelease | None:
                self_outer.assertEqual((resource, "42"), (raw, album_id)); return release
            def grab_release(self, selected: LidarrRelease) -> bool: self_outer.assertEqual(release, selected); events.append("post"); return True
            def observe_grab(self, _: str, __: LidarrRelease, *, watermark: int) -> ArrGrabObservation:
                self_outer.assertEqual(7, watermark); events.append("observe"); return ArrGrabObservation("absent")
        class Fence:
            def pre_admit(self, _: PreAdmissionIntent) -> AdmissionDecision: events.append("preadmit"); return AdmissionDecision(True, "admitted")
            def bind_grab(self, *_: object) -> bool: raise AssertionError("absence cannot bind")

        self_outer = self
        service = ReconcilerService(
            ReconciliationState(), Store(), {"lidarr": Lidarr()}, Fence(),
            {"lidarr": ReconcilerSource("media-interlock-lidarr", 9)},
        )
        intent = SearchIntent(str(uuid.uuid4()), "lidarr", "42", False, "music-v1:selected")

        self.assertEqual("pending", service.execute_selected(intent, release, now=100))
        self.assertEqual(["client", "watermark", "save", "client", "preadmit", "save", "post", "observe"], events)
