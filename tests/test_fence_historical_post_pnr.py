from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import _source_tree  # noqa: F401

from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.adapters.sonarr import SonarrAdapter
from media_interlock.config import SecretReference
from media_interlock.contracts import StatusCode, post_pnr_historical_adoption, post_pnr_historical_adoption_query
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, PostPnrHistoricalAdoptionIntent, ReservationState
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService, FenceSource
from media_interlock.fence.store import FenceStore


class HistoricalPostPnrIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.torrent_hash = "a" * 40
        self.source = "sonarr"
        self.category = "media-interlock-sonarr"
        self.save_path = "/downloads/shows"
        self.client_name = "shows"
        self.entity_ids = ("42", "43", "44")
        self.torrent = {"hash": self.torrent_hash, "tags": "", "category": self.category, "save_path": self.save_path, "size": 400, "amount_left": 400, "state": "pausedDL"}
        self.music = {"hash": "b" * 40, "tags": "lidarr", "category": "media-music", "save_path": "/downloads/music", "size": 100, "amount_left": 0, "state": "uploading"}
        self.history = [
            {"id": 8 + index, "eventType": "grabbed", "episodeId": int(entity_id), "downloadId": self.torrent_hash.upper()}
            for index, entity_id in enumerate(self.entity_ids)
        ]
        self.queue: list[dict[str, object]] = []
        self.posts: list[str] = []
        self.hide_tagged = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass

            def do_POST(self) -> None:
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                fixture.posts.append(self.path)
                if self.path == "/api/v2/auth/login":
                    self.send_response(200); self.send_header("Set-Cookie", "SID=fixture; Path=/"); self.end_headers(); self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/addTags" and fields.get("hashes") == [fixture.torrent_hash]:
                    fixture.torrent["tags"] = fields["tags"][0]; self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
                else:
                    self.send_error(400)

            def do_GET(self) -> None:
                path, query = urlparse(self.path).path, parse_qs(urlparse(self.path).query)
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                if path == "/api/v2/app/webapiVersion": self.wfile.write(b"2.11.3")
                elif path == "/api/v2/app/version": self.wfile.write(b"v5.2.3")
                elif path == "/api/v2/torrents/info":
                    selected = [item for item in (fixture.torrent, fixture.music) if not query.get("hashes") or item["hash"] in query["hashes"]]
                    if fixture.hide_tagged and query.get("tag"):
                        selected = []
                    self.wfile.write(json.dumps(selected).encode())
                elif path == "/api/v3/downloadclient":
                    category_field = "movieCategory" if fixture.source == "radarr" else "tvCategory"
                    self.wfile.write(json.dumps([{"id": 7, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "name": fixture.client_name, "fields": [{"name": category_field, "value": fixture.category}, {"name": "initialState", "value": 2}]}]).encode())
                elif path == "/api/v3/history": self.wfile.write(json.dumps({"records": fixture.history, "totalRecords": len(fixture.history)}).encode())
                elif path == "/api/v3/queue": self.wfile.write(json.dumps({"records": fixture.queue, "totalRecords": len(fixture.queue)}).encode())
                else: self.wfile.write(b"{}")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever); self.thread.start()
        host, port = self.http.server_address
        self.base = f"http://{host}:{port}"

    async def asyncTearDown(self) -> None:
        self.http.shutdown(); self.http.server_close(); self.thread.join()

    def _daemon(self, root: Path) -> tuple[FenceStore, FenceDaemon]:
        store = FenceStore.open(root / "state")
        state = store.load(FencePolicy(1_000, 2))
        qbit = QbittorrentAdapter(self.base, SecretReference("env", "USER"), SecretReference("env", "PASS"), secret_resolver=lambda _: "fixture")
        observer_type = RadarrAdapter if self.source == "radarr" else SonarrAdapter
        observer = observer_type(self.base, SecretReference("env", "KEY"), staging_root=None, secret_resolver=lambda _: "fixture")
        service = FenceService(state, store, qbit, None, sources={self.source: FenceSource(self.category, Path(self.save_path), 7)}, observers={self.source: observer})
        return store, FenceDaemon(service, FenceObservability(state), readiness=lambda: (True, True, True))

    async def _exchange(self, socket_path: Path, envelope):
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(envelope.encode()); await writer.drain()
        response = type(envelope).decode(await reader.readuntil(b"\n"))
        writer.close(); await writer.wait_closed()
        return response

    def _request(self, operation_id: str, entity_ids: tuple[str, ...] | None = None):
        return post_pnr_historical_adoption(operation_id=operation_id, source=self.source, download_client_id=7, entity_ids=self.entity_ids if entity_ids is None else entity_ids, torrent_hash=self.torrent_hash, category=self.category, save_path=self.save_path)

    async def test_sonarr_pack_without_queue_is_one_durable_adoption_and_recovers_response_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, socket_path, operation_id = Path(directory), Path(directory) / "fence.sock", str(uuid.uuid4())
            store, daemon = self._daemon(root)
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                request = self._request(operation_id)
                # Deliver the effect but lose the first response, then recover only by query.
                _, lost = await asyncio.open_unix_connection(socket_path)
                lost.write(request.encode()); await lost.drain(); lost.close(); await lost.wait_closed()
                for _ in range(40):
                    if daemon._service.post_pnr_historical_receipt(operation_id) is not None: break
                    await asyncio.sleep(0.01)
                receipt = await self._exchange(socket_path, post_pnr_historical_adoption_query(operation_id))
                self.assertEqual("post_pnr_historical_adoption_receipt", receipt.kind, receipt.body)
                self.assertEqual(list(self.entity_ids), receipt.body["entity_ids"])
                self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
                self.assertEqual("pausedDL", self.torrent["state"])
                self.assertEqual("lidarr", self.music["tags"])
                self.assertEqual("media-music", self.music["category"])
                self.assertEqual("/downloads/music", self.music["save_path"])
                metrics = daemon._observability.metrics()
                status = daemon.status()
                for private in (*self.entity_ids, self.torrent_hash, self.save_path, operation_id):
                    self.assertNotIn(private, metrics)
                    self.assertNotIn(private, json.dumps(status))
            finally:
                server.close(); await server.wait_closed(); store.close()

            restored_store, restored = self._daemon(root)
            restored.recover()
            self.assertEqual(ReservationState.QBITTORRENT_STOPPED, restored._service._state.reservation(operation_id).state)
            server = await asyncio.start_unix_server(restored.handle, path=socket_path)
            try:
                self.assertEqual(receipt, await self._exchange(socket_path, post_pnr_historical_adoption_query(operation_id)))
                self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            finally:
                server.close(); await server.wait_closed(); restored_store.close()

    async def test_restart_recovers_historical_intent_and_lost_tag_readback_without_repeating_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, socket_path, operation_id = Path(directory), Path(directory) / "fence.sock", str(uuid.uuid4())
            # Simulate a crash after durable intent and before any qBittorrent effect.
            store, daemon = self._daemon(root)
            state = daemon._service._state
            intent = PostPnrHistoricalAdoptionIntent(operation_id, self.source, 7, self.entity_ids, self.torrent_hash, self.category, self.save_path, 400, (8, 9, 10))
            self.assertTrue(state.adopt_post_pnr_historical(intent, qbittorrent_ready=True).admitted)
            store.save(state); store.close()
            restored_store, restored = self._daemon(root)
            restored.recover()
            self.assertEqual(ReservationState.QBITTORRENT_STOPPED, restored._service._state.reservation(operation_id).state)
            self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            saved = restored._service._state.post_pnr_historical_adoption(operation_id)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(("42", "43", "44"), saved.entity_ids)
            self.assertEqual((8, 9, 10), saved.history_ids)
            self.assertEqual(400, saved.expected_bytes)
            restored_store.close()

            # Simulate a tag effect whose read-back response is lost. Recovery
            # must observe the tag and persist it, never issue a second addTags.
            self.torrent["tags"] = ""; self.posts.clear(); self.hide_tagged = True
            second_root = root / "readback"
            store, daemon = self._daemon(second_root)
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            second_id = str(uuid.uuid4())
            try:
                pending = await self._exchange(socket_path, self._request(second_id))
                self.assertEqual(StatusCode.INHIBITED.value, pending.body["code"])
                self.assertEqual(ReservationState.TAG_INTENT_RECORDED, daemon._service._state.reservation(second_id).state)
                self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            finally:
                server.close(); await server.wait_closed(); store.close()
            self.hide_tagged = False
            restored_store, restored = self._daemon(second_root)
            restored.recover()
            self.assertEqual(ReservationState.QBITTORRENT_STOPPED, restored._service._state.reservation(second_id).state)
            self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            restored_store.close()

    async def test_queue_evidence_and_qbittorrent_identity_are_exact_and_second_operation_conflicts(self) -> None:
        self.queue = [
            {"episodeId": int(entity_id), "downloadId": self.torrent_hash.upper(), "downloadClient": self.client_name, "protocol": "torrent", "size": 400}
            for entity_id in self.entity_ids
        ]
        with tempfile.TemporaryDirectory() as directory:
            root, socket_path = Path(directory), Path(directory) / "fence.sock"
            store, daemon = self._daemon(root)
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                first = await self._exchange(socket_path, self._request(str(uuid.uuid4())))
                self.assertEqual("post_pnr_historical_adoption_receipt", first.kind, first.body)
                second = await self._exchange(socket_path, self._request(str(uuid.uuid4())))
                self.assertEqual(StatusCode.CONFLICT.value, second.body["code"])
                self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            finally:
                server.close(); await server.wait_closed(); store.close()

    async def test_radarr_singleton_without_queue_is_accepted(self) -> None:
        self.source, self.category, self.save_path, self.client_name, self.entity_ids = "radarr", "media-interlock-radarr", "/downloads/movies", "movies", ("42",)
        self.torrent.update({"category": self.category, "save_path": self.save_path})
        self.history = [{"id": 8, "eventType": "grabbed", "movieId": 42, "downloadId": self.torrent_hash.upper()}]
        with tempfile.TemporaryDirectory() as directory:
            store, daemon = self._daemon(Path(directory))
            result = daemon._dispatch(self._request(str(uuid.uuid4())))
            self.assertEqual("post_pnr_historical_adoption_receipt", result.kind, result.body)
            self.assertEqual(["42"], result.body["entity_ids"])
            self.assertEqual(1, self.posts.count("/api/v2/torrents/addTags"))
            store.close()

    async def test_ambiguity_divergence_and_qbittorrent_drift_do_not_tag(self) -> None:
        music_before = json.dumps(self.music, sort_keys=True, separators=(",", ":"))
        cases = (
            ("zero", [], [], {}),
            ("partial", self.history[:2], [], {}),
            ("additional", self.history + [{"id": 99, "eventType": "grabbed", "episodeId": 99, "downloadId": self.torrent_hash.upper()}], [], {}),
            ("duplicate", self.history + [{"id": 99, "eventType": "grabbed", "episodeId": 44, "downloadId": self.torrent_hash.upper()}], [], {}),
            ("queue-drift", self.history, [{"episodeId": int(entity_id), "downloadId": self.torrent_hash.upper(), "downloadClient": self.client_name, "protocol": "torrent", "size": 401} for entity_id in self.entity_ids], {}),
            ("active", self.history, [], {"state": "downloading"}),
            ("owned", self.history, [], {"tags": "fence:another"}),
            ("path", self.history, [], {"save_path": "/wrong"}),
            ("absent", self.history, [], {"hash": "c" * 40}),
        )
        for label, history, queue, drift in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                self.history, self.queue = history, queue
                self.torrent.update({"hash": self.torrent_hash, "tags": "", "save_path": self.save_path, "state": "pausedDL"}); self.torrent.update(drift)
                self.posts.clear()
                store, daemon = self._daemon(Path(directory))
                result = daemon._dispatch(self._request(str(uuid.uuid4())))
                self.assertEqual(StatusCode.CONFLICT.value, result.body["code"])
                self.assertNotIn("/api/v2/torrents/addTags", self.posts)
                self.assertEqual(music_before, json.dumps(self.music, sort_keys=True, separators=(",", ":")))
                store.close()
