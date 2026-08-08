from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, FenceState, QbittorrentActivityObservation, QbittorrentObservation
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService
from media_interlock.reconciler import cli


class ReconcilerCliTests(unittest.TestCase):
    def test_cli_uses_arr_http_and_real_fence_socket_once(self) -> None:
        events: list[str] = []
        grabbed = [False]
        release = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie", "size": 400, "downloadUrl": "https://indexer.invalid/release"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass
            def do_GET(self) -> None:
                if self.path == "/api/v3/downloadclient":
                    data = [{"enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}]}]
                elif self.path.startswith("/api/v3/release?"):
                    data = [release]
                elif self.path.startswith("/api/v3/history?"):
                    data = {"records": [] if not grabbed[0] else [{"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie", "downloadId": "a" * 40}], "totalRecords": 0 if not grabbed[0] else 1}
                elif self.path.startswith("/api/v3/queue?"):
                    data = {"records": [{"id": 9, "movieId": 42, "title": "fixture.movie", "downloadId": "a" * 40, "protocol": "torrent", "size": 400}], "totalRecords": 1}
                else:
                    self.send_error(404); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(data).encode())
            def do_POST(self) -> None:
                nonlocal grabbed
                if self.path != "/api/v3/release" or json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode()) != release:
                    self.send_error(400); return
                grabbed[0] = True; events.append("arr-post"); self.send_response(200); self.end_headers()

        class Store:
            def save(self, _: FenceState) -> None: events.append("fence-save")
        class Qb:
            def ready(self) -> bool: return True
            def observe_existing_stopped(self, _: str, category: str) -> QbittorrentObservation: events.append(f"stopped:{category}"); return QbittorrentObservation("observed", 400)
            def apply_reservation_tag(self, _: str, __: str) -> bool: events.append("tag"); return True
            def observe_tagged_stopped(self, _: str, category: str, __: str) -> QbittorrentObservation: events.append(f"tagged:{category}"); return QbittorrentObservation("observed", 400)
            def resume(self, _: str) -> bool: events.append("resume"); return True
            def observe_active(self, _: str, __: str, category: str) -> QbittorrentActivityObservation: events.append(f"active:{category}"); return QbittorrentActivityObservation("observed", True)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http_thread = threading.Thread(target=http.serve_forever); http_thread.start()
        self.addCleanup(http.server_close); self.addCleanup(http_thread.join); self.addCleanup(http.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); staging = root / "staging"; runtime = root / "runtime"; staging.mkdir(); runtime.mkdir()
            fence_path = runtime / "fence.sock"; fence_state = FenceState(FencePolicy(1_000, 1))
            daemon = FenceDaemon(FenceService(fence_state, Store(), Qb(), None, categories={"radarr": "media-interlock-radarr", "sonarr": "media-interlock-sonarr"}), FenceObservability(fence_state), readiness=lambda: (True, True, True))
            ready, stop = threading.Event(), threading.Event()
            async def serve() -> None:
                server = await asyncio.start_unix_server(daemon.handle, path=fence_path); ready.set()
                while not stop.is_set(): await asyncio.sleep(0.001)
                server.close(); await server.wait_closed()
            socket_thread = threading.Thread(target=lambda: asyncio.run(serve())); socket_thread.start()
            self.assertTrue(ready.wait(2))
            host, port = http.server_address
            config = root / "media-interlock.toml"
            rows = ["[shared]", f'runtime_dir = "{runtime}"', "", "[fence]", f'state_dir = "{root / "fence-state"}"', f'socket_path = "{fence_path}"', f'staging_root = "{staging}"', 'radarr_category = "media-interlock-radarr"', 'sonarr_category = "media-interlock-sonarr"', "capacity_bytes = 1000", "max_inflight = 1", "", "[reconciler]", f'state_dir = "{root / "reconciler-state"}"', f'socket_path = "{runtime / "reconciler.sock"}"', "", "[reconciler.movie]", "minimum_age_days = 30", "terminal_horizon_days = 365", "cooldown_seconds = 0", "max_attempts = 3", "max_searches_per_run = 5", "", "[reconciler.episode]", "minimum_age_days = 7", "terminal_horizon_days = 180", "cooldown_seconds = 0", "max_attempts = 2", "max_searches_per_run = 5", "", "[adapters.radarr]", f'base_url = "http://{host}:{port}"', 'api_key = "env:ARR_FIXTURE_KEY"']
            config.write_text("\n".join(rows) + "\n", encoding="utf-8")
            prior = os.environ.get("ARR_FIXTURE_KEY"); os.environ["ARR_FIXTURE_KEY"] = "fixture"
            try:
                rendered = io.StringIO()
                with contextlib.redirect_stdout(rendered):
                    result = cli.main(["--config", str(config), "--source", "radarr", "--entity", "42", "--checkpoint", "fixture", "--json"])
            finally:
                stop.set(); socket_thread.join(2)
                if prior is None: os.environ.pop("ARR_FIXTURE_KEY", None)
                else: os.environ["ARR_FIXTURE_KEY"] = prior

        self.assertEqual(0, result)
        self.assertEqual({"version": "v1", "status": "ok", "message": "bound"}, json.loads(rendered.getvalue()))
        self.assertEqual(1, events.count("arr-post")); self.assertLess(events.index("arr-post"), events.index("resume"))
        self.assertEqual("qbittorrent_active", fence_state.reservation(next(iter(fence_state.records()))["operation_id"]).state)
