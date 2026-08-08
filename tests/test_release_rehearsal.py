from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.arr import ArrCandidate, ArrGrabObservation, ArrRelease
from media_interlock.adapters.bazarr import BazarrAdapter
from media_interlock.adapters.jellyfin import CatalogExpectation, CatalogObservation, CatalogSubmission
from media_interlock.adapters.prowlarr import ProwlarrAdapter
from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.adapters.seerr import SeerrAdapter
from media_interlock.adapters.sonarr import SonarrAdapter
from media_interlock.config import SecretReference
from media_interlock.contracts import Envelope
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, FenceState, QbittorrentActivityObservation, QbittorrentObservation
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService
from media_interlock.fence.store import FenceStore
from media_interlock.publisher.daemon import PublisherDaemon
from media_interlock.publisher.filesystem import CandidateVerifier
from media_interlock.publisher.generation import AssetGenerationPublisher
from media_interlock.publisher.model import PublicationState
from media_interlock.publisher.observability import PublisherObservability
from media_interlock.publisher.service import AssetPublisherWorkProcessor, PathTranslation, PublisherService
from media_interlock.publisher.store import PublisherStore
from media_interlock.reconciler.fence_client import UnixFenceClient
from media_interlock.reconciler.model import ReconciliationState, SearchIntent
from media_interlock.reconciler.service import ReconcilerService


class ReleaseRehearsalTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_declared_adapters_use_their_public_http_readiness_boundaries(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass
            def do_POST(self) -> None:
                if self.path == "/api/v2/auth/login":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"Ok."); return
                self.send_error(404)
            def do_GET(self) -> None:
                payload: object
                if self.path == "/System/Info": payload = {"Version": "10.11.11"}
                elif self.path == "/api/system/status": payload = {"data": {"bazarr_version": "1.6.0"}}
                elif self.path == "/api/v1/settings/main": payload = {"applicationTitle": "fixture"}
                elif self.path == "/api/v1/health": payload = []
                elif self.path == "/api/v1/indexer": payload = [{"enable": True}]
                elif self.path == "/api/v3/downloadclient": payload = [{"enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}, {"name": "tvCategory", "value": "media-interlock-sonarr"}]}]
                elif self.path == "/api/v2/app/preferences": payload = {"start_paused_enabled": True}
                elif self.path == "/api/v2/app/webapiVersion":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"2.11.3"); return
                elif self.path == "/api/v2/app/version":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"v5.2.3"); return
                else: self.send_error(404); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(payload).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); thread = threading.Thread(target=server.serve_forever); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join); self.addCleanup(server.shutdown)
        host, port = server.server_address; base = f"http://{host}:{port}"; key = SecretReference("env", "FIXTURE")
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            self.assertTrue(RadarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-radarr"))
            self.assertTrue(SonarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-sonarr"))
            self.assertTrue(QbittorrentAdapter(base, key, key, staging_root=staging, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(ProwlarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(BazarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(SeerrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        from media_interlock.adapters.jellyfin import JellyfinAdapter
        self.assertTrue(JellyfinAdapter(base, key, secret_resolver=lambda _: "fixture").ready())

    async def test_reconciler_fence_and_publisher_complete_one_exact_unix_runtime_handoff(self) -> None:
        operation_id = str(uuid.uuid4())
        media = b"synthetic-release-media"
        release_resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie", "size": len(media), "downloadUrl": "https://indexer.invalid/release"}
        release = ArrRelease(release_resource, hashlib.sha256(json.dumps(release_resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), len(media))
        events: list[str] = []

        class Qbittorrent:
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, torrent_hash: str, category: str) -> QbittorrentObservation:
                events.append(f"stopped:{torrent_hash}:{category}"); return QbittorrentObservation("observed", len(media))
            def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool:
                events.append(f"tag:{torrent_hash}:{reservation_id}"); return True
            def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str) -> QbittorrentObservation:
                events.append(f"tagged:{torrent_hash}:{category}:{reservation_id}"); return QbittorrentObservation("observed", len(media))
            def resume(self, torrent_hash: str) -> bool:
                events.append(f"resume:{torrent_hash}"); return True
            def observe_active(self, torrent_hash: str, reservation_id: str, category: str) -> QbittorrentActivityObservation:
                events.append(f"active:{torrent_hash}:{reservation_id}:{category}"); return QbittorrentActivityObservation("observed", True)
            def terminal_observed(self, torrent_hash: str, reservation_id: str, category: str) -> QbittorrentActivityObservation:
                events.append(f"terminal:{torrent_hash}:{reservation_id}:{category}"); return QbittorrentActivityObservation("observed", True)

        class Arr:
            def stopped_qbittorrent_client(self, category: str) -> bool: return category == "media-interlock-radarr"
            def history_watermark(self) -> int | None: return 7
            def first_approved_release(self, entity_id: str) -> ArrRelease | None: return release if entity_id == "42" else None
            def grab_release(self, selected: ArrRelease) -> bool: events.append("arr-post"); return selected == release
            def observe_grab(self, entity_id: str, selected: ArrRelease, *, watermark: int) -> ArrGrabObservation:
                events.append("arr-observe"); return ArrGrabObservation("observed", "A" * 40, "a" * 40) if (entity_id, selected, watermark) == ("42", release, 7) else ArrGrabObservation("unknown")

        class Correlation:
            def candidate_identity(self, download_id: str, media_id: str) -> ArrCandidate | None:
                return ArrCandidate("movie.mkv", "radarr:tmdb-42", "Movie", {"Tmdb": "42"}) if (download_id, media_id) == ("A" * 40, "42") else None

        class Catalog:
            def submit_update(self, internal_path: str, update_type: str) -> CatalogSubmission:
                events.append(f"catalog-submit:{internal_path}:{update_type}"); return CatalogSubmission(True)
            def observe_catalog(self, expected: CatalogExpectation) -> CatalogObservation | None:
                events.append(f"catalog-observe:{expected.internal_path}")
                if expected.internal_path != "/jellyfin/library/radarr-tmdb-42/payload.mkv" or expected.expected_bytes != len(media): return None
                return CatalogObservation("item-42", "source-42", expected.internal_path, len(media))
            def direct_play_matches(self, observation: CatalogObservation, *, expected_bytes: int, expected_sha256: str) -> bool:
                events.append(f"direct-play:{observation.item_id}:{observation.media_source_id}")
                return observation.internal_path == "/jellyfin/library/radarr-tmdb-42/payload.mkv" and expected_bytes == len(media) and expected_sha256 == hashlib.sha256(media).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); staging = root / "staging"; canonical = root / "canonical"; staging.mkdir(); canonical.mkdir()
            (staging / "movie.mkv").write_bytes(media)
            fence_store = FenceStore.open(root / "fence-state")
            publisher_store = PublisherStore.open(root / "publisher-state")
            self.addCleanup(fence_store.close); self.addCleanup(publisher_store.close)
            fence_state = fence_store.load(FencePolicy(capacity_bytes=10_000, max_inflight=1))
            fence_service = FenceService(fence_state, fence_store, Qbittorrent(), None, categories={"radarr": "media-interlock-radarr"})
            fence = FenceDaemon(fence_service, FenceObservability(fence_state), readiness=lambda: (True, True, True))
            publisher_state = publisher_store.load(); publisher_service = PublisherService(publisher_state, publisher_store)
            processor = AssetPublisherWorkProcessor(publisher_service, {"radarr": Correlation()}, CandidateVerifier(staging), AssetGenerationPublisher(staging, canonical, namespace="library"), Catalog(), PathTranslation(canonical, "library", "/jellyfin/library"), library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f")
            publisher = PublisherDaemon(publisher_service, PublisherObservability(publisher_state), readiness=lambda: True, process=processor)
            fence_path, publisher_path = root / "fence.sock", root / "publisher.sock"
            fence_server = await asyncio.start_unix_server(fence.handle, path=fence_path)
            publisher_server = await asyncio.start_unix_server(publisher.handle, path=publisher_path)
            try:
                reconciler = ReconcilerService(ReconciliationState(), type("Store", (), {"save": lambda *_: None})(), {"radarr": Arr()}, UnixFenceClient(fence_path), {"radarr": "media-interlock-radarr"})
                self.assertEqual("bound", await asyncio.to_thread(reconciler.execute, SearchIntent(operation_id, "radarr", "42", False, "fixture"), now=1))
                terminal = fence_service.observe(operation_id)
                assert terminal is not None
                reader, writer = await asyncio.open_unix_connection(publisher_path)
                writer.write(terminal.encode()); await writer.drain()
                receipt = Envelope.decode(await reader.readuntil(b"\n"))
                writer.close(); await writer.wait_closed()
                fence_reader, fence_writer = await asyncio.open_unix_connection(fence_path)
                fence_writer.write(receipt.encode()); await fence_writer.drain()
                receipt_status = Envelope.decode(await fence_reader.readuntil(b"\n"))
                fence_writer.close(); await fence_writer.wait_closed()
            finally:
                fence_server.close(); publisher_server.close()
                await fence_server.wait_closed(); await publisher_server.wait_closed()

            self.assertEqual("custody_receipt", receipt.kind)
            self.assertEqual("ok", receipt_status.body["code"])
            self.assertEqual(PublicationState.DELIVERED, publisher_store.load().publication(operation_id).state)
            self.assertEqual("released", fence_state.reservation(operation_id).state)
            self.assertEqual(media, (canonical / "library" / "radarr-tmdb-42" / "payload.mkv").read_bytes())
        self.assertLess(events.index("arr-post"), events.index("resume:" + "a" * 40))
        self.assertLess(events.index("terminal:" + "a" * 40 + f":fence:{operation_id}:media-interlock-radarr"), events.index("catalog-submit:/jellyfin/library/radarr-tmdb-42/payload.mkv:created"))
