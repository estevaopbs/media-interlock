from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.arr import ArrGrabObservation, ArrRelease
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, FenceState, QbittorrentActivityObservation, QbittorrentObservation
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService
from media_interlock.reconciler.fence_client import UnixFenceClient
from media_interlock.reconciler.model import ReconciliationState, SearchIntent
from media_interlock.reconciler.service import ReconcilerService


class ReconcilerFenceVerticalTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_arr_release_is_bound_over_unix_before_the_fence_resumes_it(self) -> None:
        operation_id = str(uuid.uuid4())
        release_resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(release_resource, hashlib.sha256(json.dumps(release_resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), 400)
        events: list[str] = []

        class Store:
            def save(self, _: object) -> None:
                events.append("fence-save")

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

        class Arr:
            def stopped_qbittorrent_client(self, category: str) -> bool:
                return category == "media-interlock-radarr"

            def history_watermark(self) -> int | None:
                return 7

            def first_approved_release(self, entity_id: str) -> ArrRelease | None:
                return release if entity_id == "42" else None

            def grab_release(self, observed: ArrRelease) -> bool:
                events.append(f"arr-post:{observed.selector_fingerprint}")
                return observed == release

            def observe_grab(self, entity_id: str, observed: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                events.append(f"arr-observe:{entity_id}:{watermark}")
                return ArrGrabObservation("observed", "A" * 40, "a" * 40) if observed == release else ArrGrabObservation("unknown")

        fence_state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        daemon = FenceDaemon(FenceService(fence_state, Store(), Qbittorrent(), None, categories={"radarr": "media-interlock-radarr"}), FenceObservability(fence_state), readiness=lambda: (True, True, True))
        reconciliation = ReconciliationState()

        class ReconciliationStore:
            def save(self, _: ReconciliationState) -> None:
                events.append("reconciler-save")

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "fence.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                service = ReconcilerService(reconciliation, ReconciliationStore(), {"radarr": Arr()}, UnixFenceClient(socket_path), {"radarr": "media-interlock-radarr"})
                result = await asyncio.to_thread(service.execute, SearchIntent(operation_id, "radarr", "42", False, "fixture"), now=9)
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual("bound", result)
        self.assertTrue(reconciliation.observed(operation_id))
        self.assertEqual("qbittorrent_active", fence_state.reservation(operation_id).state)
        self.assertEqual("A" * 40, fence_state.reservation(operation_id).download_id)
        self.assertEqual(1, len([item for item in events if item.startswith("arr-post:")]))
        self.assertLess(events.index("arr-post:" + release.selector_fingerprint), events.index("arr-observe:42:7"))
        self.assertLess(events.index("arr-observe:42:7"), events.index("resume:" + "a" * 40))
