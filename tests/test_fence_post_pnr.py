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
from media_interlock.config import SecretReference
from media_interlock.contracts import Envelope, StatusCode, post_pnr_adoption, post_pnr_adoption_query
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, PostPnrAdoptionIntent, ReservationState
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService, FenceSource
from media_interlock.fence.store import FenceStore


class FencePostPnrUnixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_recovers_durable_intent_and_tag_effect_boundaries_over_unix(self) -> None:
        torrent_hash = "c" * 40
        torrent = {"hash": torrent_hash, "tags": "", "category": "media-interlock-radarr", "save_path": "/downloads/movies", "size": 400, "amount_left": 400, "state": "pausedDL"}
        posts: list[str] = []
        hide_tagged = [False]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass

            def do_POST(self) -> None:
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                posts.append(self.path)
                if self.path == "/api/v2/auth/login":
                    self.send_response(200); self.send_header("Set-Cookie", "SID=fixture; Path=/"); self.end_headers(); self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/addTags" and fields.get("hashes") == [torrent_hash]:
                    torrent["tags"] = fields["tags"][0]; self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
                else: self.send_error(400)

            def do_GET(self) -> None:
                path, query = urlparse(self.path).path, parse_qs(urlparse(self.path).query)
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                if path == "/api/v2/app/webapiVersion": self.wfile.write(b"2.11.3")
                elif path == "/api/v2/app/version": self.wfile.write(b"v5.2.3")
                elif path == "/api/v2/torrents/info": self.wfile.write(json.dumps([] if hide_tagged[0] and query.get("tag") else [torrent]).encode())
                elif path == "/api/v3/downloadclient": self.wfile.write(json.dumps([{"id": 7, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "name": "movies", "fields": [{"name": "movieCategory", "value": torrent["category"]}, {"name": "initialState", "value": 2}]}]).encode())
                elif path == "/api/v3/history": self.wfile.write(json.dumps({"records": [{"id": 8, "eventType": "grabbed", "movieId": 42, "downloadId": torrent_hash.upper()}], "totalRecords": 1}).encode())
                elif path == "/api/v3/queue": self.wfile.write(json.dumps({"records": [{"movieId": 42, "downloadId": torrent_hash.upper(), "downloadClient": "movies", "protocol": "torrent", "size": 400}], "totalRecords": 1}).encode())
                else: self.wfile.write(b"{}")

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever); thread.start()
        self.addCleanup(http.server_close); self.addCleanup(thread.join); self.addCleanup(http.shutdown)
        host, port = http.server_address; base = f"http://{host}:{port}"

        async def exchange(path: Path, envelope: Envelope) -> Envelope:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(envelope.encode()); await writer.drain()
            response = Envelope.decode(await reader.readuntil(b"\n"))
            writer.close(); await writer.wait_closed()
            return response

        def daemon_for(store: FenceStore, state: object) -> FenceDaemon:
            adapter = QbittorrentAdapter(base, SecretReference("env", "USER"), SecretReference("env", "PASS"), secret_resolver=lambda _: "fixture")
            observer = RadarrAdapter(base, SecretReference("env", "KEY"), staging_root=None, secret_resolver=lambda _: "fixture")
            return FenceDaemon(FenceService(state, store, adapter, None, sources={"radarr": FenceSource(torrent["category"], Path(torrent["save_path"]), 7)}, observers={"radarr": observer}), FenceObservability(state), readiness=lambda: (True, True, True))  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            root, socket_path = Path(directory), Path(directory) / "fence.sock"
            operation_id = str(uuid.uuid4())
            store = FenceStore.open(root / "intent-state")
            state = store.load(FencePolicy(1_000, 1))
            self.assertTrue(state.adopt_post_pnr(PostPnrAdoptionIntent(operation_id, "radarr", 7, "42", torrent_hash, torrent["category"], torrent["save_path"], 400, 8), qbittorrent_ready=True).admitted)
            store.save(state); store.close()  # crash after durable GRAB_BOUND, before any qBittorrent effect
            restored_store = FenceStore.open(root / "intent-state")
            restored = restored_store.load(FencePolicy(1_000, 1))
            daemon = daemon_for(restored_store, restored); daemon.recover()
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                receipt = await exchange(socket_path, post_pnr_adoption_query(operation_id))
                self.assertEqual("post_pnr_adoption_receipt", receipt.kind)
                self.assertEqual("pausedDL", torrent["state"])
                self.assertEqual(1, posts.count("/api/v2/torrents/addTags"))
            finally:
                server.close(); await server.wait_closed(); restored_store.close()

            torrent["tags"] = ""; posts.clear(); hide_tagged[0] = True
            operation_id = str(uuid.uuid4())
            request = post_pnr_adoption(operation_id=operation_id, source="radarr", download_client_id=7, entity_id="42", torrent_hash=torrent_hash, category=torrent["category"], save_path=torrent["save_path"])
            store = FenceStore.open(root / "tag-state")
            state = store.load(FencePolicy(1_000, 1))
            daemon = daemon_for(store, state)
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                pending = await exchange(socket_path, request)
                self.assertEqual(StatusCode.INHIBITED.value, pending.body["code"])
                self.assertEqual(ReservationState.TAG_INTENT_RECORDED, state.reservation(operation_id).state)
                self.assertEqual(1, posts.count("/api/v2/torrents/addTags"))
            finally:
                server.close(); await server.wait_closed(); store.close()

            hide_tagged[0] = False
            restored_store = FenceStore.open(root / "tag-state")
            restored = restored_store.load(FencePolicy(1_000, 1))
            daemon = daemon_for(restored_store, restored); daemon.recover()
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                receipt = await exchange(socket_path, post_pnr_adoption_query(operation_id))
                self.assertEqual("post_pnr_adoption_receipt", receipt.kind)
                self.assertEqual(ReservationState.QBITTORRENT_STOPPED, restored.reservation(operation_id).state)
                self.assertEqual(1, posts.count("/api/v2/torrents/addTags"))
                self.assertEqual("pausedDL", torrent["state"])
            finally:
                server.close(); await server.wait_closed(); restored_store.close()

    async def test_real_unix_daemon_durably_claims_one_sealed_arr_grab_after_a_lost_response(self) -> None:
        torrent_hash = "a" * 40
        torrent = {"hash": torrent_hash, "tags": "", "category": "media-interlock-radarr", "save_path": "/downloads/movies", "size": 400, "amount_left": 400, "state": "pausedDL"}
        music = {"hash": "b" * 40, "tags": "lidarr", "category": "media-music", "save_path": "/downloads/music", "size": 100, "amount_left": 0, "state": "uploading"}
        posts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass

            def do_POST(self) -> None:
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                posts.append(self.path)
                if self.path == "/api/v2/auth/login":
                    self.send_response(200); self.send_header("Set-Cookie", "SID=fixture; Path=/"); self.end_headers(); self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/addTags" and fields.get("hashes") == [torrent_hash]:
                    torrent["tags"] = fields["tags"][0]; self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
                else:
                    self.send_error(400)

            def do_GET(self) -> None:
                path, query = urlparse(self.path).path, parse_qs(urlparse(self.path).query)
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                if path == "/api/v2/app/webapiVersion": self.wfile.write(b"2.11.3")
                elif path == "/api/v2/app/version": self.wfile.write(b"v5.2.3")
                elif path == "/api/v2/torrents/info":
                    selected = [item for item in (torrent, music) if not query.get("hashes") or item["hash"] in query["hashes"]]
                    self.wfile.write(json.dumps(selected).encode())
                elif path == "/api/v3/downloadclient":
                    self.wfile.write(json.dumps([{"id": 7, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "name": "movies", "fields": [{"name": "movieCategory", "value": torrent["category"]}, {"name": "initialState", "value": 2}]}]).encode())
                elif path == "/api/v3/history":
                    self.wfile.write(json.dumps({"records": [{"id": 8, "eventType": "grabbed", "movieId": 42, "downloadId": torrent_hash.upper()}], "totalRecords": 1}).encode())
                elif path == "/api/v3/queue":
                    self.wfile.write(json.dumps({"records": [{"movieId": 42, "downloadId": torrent_hash.upper(), "downloadClient": "movies", "protocol": "torrent", "size": 400}], "totalRecords": 1}).encode())
                else: self.wfile.write(b"{}")

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever); thread.start()
        self.addCleanup(http.server_close); self.addCleanup(thread.join); self.addCleanup(http.shutdown)
        host, port = http.server_address
        base = f"http://{host}:{port}"
        request = post_pnr_adoption(operation_id=str(uuid.uuid4()), source="radarr", download_client_id=7, entity_id="42", torrent_hash=torrent_hash, category=torrent["category"], save_path=torrent["save_path"])

        async def exchange(path: Path, envelope: Envelope) -> Envelope:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(envelope.encode()); await writer.drain()
            response = Envelope.decode(await reader.readuntil(b"\n"))
            writer.close(); await writer.wait_closed()
            return response

        with tempfile.TemporaryDirectory() as directory:
            root, socket_path = Path(directory), Path(directory) / "fence.sock"
            store = FenceStore.open(root / "state")
            adapter = QbittorrentAdapter(base, SecretReference("env", "USER"), SecretReference("env", "PASS"), secret_resolver=lambda _: "fixture")
            observer = RadarrAdapter(base, SecretReference("env", "KEY"), staging_root=None, secret_resolver=lambda _: "fixture")
            state = store.load(FencePolicy(1_000, 1))
            daemon = FenceDaemon(FenceService(state, store, adapter, None, sources={"radarr": FenceSource(torrent["category"], Path(torrent["save_path"]), 7)}, observers={"radarr": observer}), FenceObservability(state), readiness=lambda: (True, True, True))
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                # The request reaches the real daemon, but the client loses its response.
                _, lost = await asyncio.open_unix_connection(socket_path)
                lost.write(request.encode()); await lost.drain(); lost.close(); await lost.wait_closed()
                for _ in range(20):
                    if state.post_pnr_adoption(request.operation_id) is not None: break
                    await asyncio.sleep(0.01)
                receipt = await exchange(socket_path, request)
                self.assertEqual("post_pnr_adoption_receipt", receipt.kind)
                self.assertEqual("adopted", receipt.body["state"])
                self.assertEqual(torrent["tags"], receipt.body["fence_reservation_id"])
                self.assertEqual("pausedDL", torrent["state"])
                self.assertEqual("lidarr", music["tags"])
                self.assertEqual(1, posts.count("/api/v2/torrents/addTags"))
                metrics = daemon._observability.metrics()
                self.assertNotIn(torrent_hash, metrics)
                self.assertNotIn(torrent["save_path"], metrics)
                self.assertNotIn(request.operation_id, metrics)
                conflict = await exchange(socket_path, post_pnr_adoption(operation_id=request.operation_id, source="radarr", download_client_id=7, entity_id="42", torrent_hash=torrent_hash, category="drift", save_path=torrent["save_path"]))
                self.assertEqual(StatusCode.CONFLICT.value, conflict.body["code"])
            finally:
                server.close(); await server.wait_closed(); store.close()

            restored_store = FenceStore.open(root / "state")
            restored = restored_store.load(FencePolicy(1_000, 1))
            self.assertEqual(ReservationState.QBITTORRENT_STOPPED, restored.reservation(request.operation_id).state)
            restored_daemon = FenceDaemon(FenceService(restored, restored_store, adapter, None, sources={"radarr": FenceSource(torrent["category"], Path(torrent["save_path"]), 7)}, observers={"radarr": observer}), FenceObservability(restored), readiness=lambda: (True, True, True))
            server = await asyncio.start_unix_server(restored_daemon.handle, path=socket_path)
            try:
                self.assertEqual(receipt, await exchange(socket_path, post_pnr_adoption_query(request.operation_id)))
            finally:
                server.close(); await server.wait_closed(); restored_store.close()
