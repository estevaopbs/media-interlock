from __future__ import annotations

import hashlib
import json
import unittest
import uuid

import _source_tree  # noqa: F401

from media_interlock.adapters.arr import ArrGrabObservation, ArrRelease
from media_interlock.fence.model import AdmissionDecision, PreAdmissionIntent
from media_interlock.reconciler.model import AttemptPolicy, ReconciliationState, SearchIntent
from media_interlock.reconciler.service import ReconcilerService


class ReconcilerServiceTests(unittest.TestCase):
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
                return ArrGrabObservation("observed", "a" * 40)

        class Fence:
            def bind_grab(self, received_operation: str, download_id: str) -> bool:
                self_outer.assertEqual((operation_id, "a" * 40), (received_operation, download_id))
                calls.append("bind")
                return True

        self_outer = self
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": "media-interlock-radarr"})

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
            def stopped_qbittorrent_client(self, category: str) -> bool:
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

            def bind_grab(self, _: str, __: str) -> bool:
                self_outer.fail("absent observation must not bind a grab")

        self_outer = self
        state = ReconciliationState()
        service = ReconcilerService(state, Store(), {"radarr": Arr()}, Fence(), {"radarr": "media-interlock-radarr"})
        intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "checkpoint")

        self.assertEqual("pending", service.execute(intent, now=100))
        self.assertEqual(["save", "client:media-interlock-radarr", "watermark", "release:42", "save", "preadmit", "save", "post", "observe"], events)

        self.assertEqual("pending", service.execute(intent, now=101))
        self.assertEqual(1, events.count("post"))
        self.assertEqual(2, events.count("observe"))
