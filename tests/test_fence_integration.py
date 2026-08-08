from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import _source_tree  # noqa: F401

from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.config import SecretReference
from media_interlock.contracts import custody_receipt
from media_interlock.fence.model import FencePolicy, PreAdmissionIntent, ReservationState
from media_interlock.fence.service import FenceService
from media_interlock.fence.store import FenceStore


class FenceVerticalIntegrationTests(unittest.TestCase):
    def test_pre_admission_to_arr_observed_hash_to_terminal_custody_uses_only_existing_torrent(self) -> None:
        torrent = {"tags": "", "category": "media-interlock-radarr", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}
        posts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                posts.append(self.path)
                if self.path == "/api/v2/auth/login":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "SID=fixture; Path=/")
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/addTags":
                    torrent["tags"] = fields["tags"][0]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/start":
                    torrent["state"] = "downloading"
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                else:
                    self.send_error(400)

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.path == "/api/v2/app/preferences":
                    self.wfile.write(b'{"start_paused_enabled":true}')
                elif self.path == "/api/v2/app/webapiVersion":
                    self.wfile.write(b"2.11.3")
                elif self.path == "/api/v2/app/version":
                    self.wfile.write(b"v5.2.3")
                elif self.path.startswith("/api/v2/torrents/info"):
                    self.wfile.write(json.dumps([torrent]).encode())
                else:
                    self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        adapter = QbittorrentAdapter(f"http://{host}:{port}", SecretReference("env", "USER"), SecretReference("env", "PASS"), staging_root=Path("/staging"), secret_resolver=lambda _: "fixture")
        with tempfile.TemporaryDirectory() as directory:
            store = FenceStore.open(Path(directory) / "state")
            self.addCleanup(store.close)
            state = store.load(FencePolicy(capacity_bytes=1_000, max_inflight=1))
            service = FenceService(state, store, adapter, prowlarr=None, categories={"radarr": "media-interlock-radarr"})
            intent = PreAdmissionIntent("12345678-1234-4678-9234-567812345678", "radarr", "movie-42", "b" * 64, 400, "7")

            self.assertTrue(service.pre_admit(intent, publisher_ready=True).admitted)
            self.assertTrue(service.bind_grab(intent.operation_id, torrent["hash"].upper(), torrent["hash"]))
            self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(intent.operation_id).state)
            self.assertNotIn("/api/v2/torrents/add", posts)
            torrent.update({"state": "uploading", "progress": 1})
            terminal = service.observe(intent.operation_id)
            assert terminal is not None
            self.assertEqual(torrent["hash"].upper(), terminal.body["download_id"])
            self.assertTrue(service.accept_custody(custody_receipt(intent.operation_id, terminal.body["fence_reservation_id"], "publisher-r-1")))
            self.assertEqual(ReservationState.RELEASED, state.reservation(intent.operation_id).state)
