from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import _source_tree  # noqa: F401

from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.contracts import custody_receipt
from media_interlock.config import SecretReference
from media_interlock.fence.model import AcquisitionIntent, FencePolicy, ReservationState
from media_interlock.fence.service import FenceService
from media_interlock.fence.store import FenceStore


class FenceVerticalIntegrationTests(unittest.TestCase):
    def test_admit_to_terminal_to_custody_flow_uses_durable_state_and_disposable_qbittorrent(self) -> None:
        torrent: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                if self.path == "/api/v2/auth/login":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "SID=fixture; Path=/")
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                    return
                if self.path == "/api/v2/torrents/add" and fields.get("paused") == ["true"]:
                    torrent.update({"tags": fields["tags"][0], "category": fields["category"][0], "save_path": fields["savepath"][0], "hash": "a" * 40, "size": 400, "state": "pausedDL"})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                    return
                if self.path == "/api/v2/torrents/start" and fields.get("hashes") == ["a" * 40]:
                    torrent["state"] = "downloading"
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                    return
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
                    self.wfile.write(json.dumps([torrent] if torrent else []).encode())
                else:
                    self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        adapter = QbittorrentAdapter(f"http://{host}:{port}", SecretReference("env", "USER"), SecretReference("env", "PASS"), staging_root=Path("/staging"), category="media-interlock", secret_resolver=lambda _: "fixture")
        with tempfile.TemporaryDirectory() as directory:
            store = FenceStore.open(Path(directory) / "state")
            self.addCleanup(store.close)
            state = store.load(FencePolicy(capacity_bytes=1_000, max_inflight=1))
            service = FenceService(state, store, adapter, prowlarr=None)
            intent = AcquisitionIntent("12345678-1234-4678-9234-567812345678", "radarr", "grab-42", "movie-42", 400, hashlib.sha256(b"magnet:?xt=urn:btih:fixture").hexdigest())

            self.assertTrue(service.admit(intent, source="magnet:?xt=urn:btih:fixture", publisher_ready=True).admitted)
            self.assertEqual(ReservationState.QBITTORRENT_ACTIVE, state.reservation(intent.operation_id).state)
            torrent.update({"state": "uploading", "progress": 1})
            terminal = service.observe(intent.operation_id)
            assert terminal is not None
            self.assertTrue(service.accept_custody(custody_receipt(intent.operation_id, terminal.body["fence_reservation_id"], "publisher-r-1")))
            self.assertEqual(ReservationState.RELEASED, state.reservation(intent.operation_id).state)
