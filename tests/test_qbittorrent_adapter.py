from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import _source_tree  # noqa: F401

from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.config import SecretReference


class QbittorrentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.redirect_login = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
                outer.requests.append(("POST", self.path, parse_qs(raw)))
                if self.path == "/api/v2/auth/login":
                    if outer.redirect_login:
                        self.send_response(302)
                        self.send_header("Location", "/redirected")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Set-Cookie", "SID=fixture; Path=/")
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                elif self.path == "/api/v2/torrents/start":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Ok.")
                else:
                    self.send_error(404)

            def do_GET(self) -> None:
                outer.requests.append(("GET", self.path, {}))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.path == "/api/v2/app/preferences":
                    self.wfile.write(b'{"start_paused_enabled":true}')
                elif self.path == "/api/v2/app/webapiVersion":
                    self.wfile.write(b"2.11.3")
                elif self.path == "/api/v2/app/version":
                    self.wfile.write(b"v5.2.3")
                elif self.path.startswith("/api/v2/torrents/info?tag=fence-r-1"):
                    self.wfile.write(b'[{"tags":"fence-r-1","category":"media-interlock","save_path":"/staging","hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":400,"state":"pausedDL"}]')
                else:
                    self.wfile.write(b'[]')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def adapter(self) -> QbittorrentAdapter:
        host, port = self.server.server_address
        return QbittorrentAdapter(f"http://{host}:{port}", SecretReference("env", "QBIT_USER"), SecretReference("env", "QBIT_PASS"), staging_root=Path("/staging"), secret_resolver=lambda reference: {"QBIT_USER": "fixture-user", "QBIT_PASS": "fixture-pass"}[reference.reference])

    def test_readiness_and_resume_use_documented_cookie_api(self) -> None:
        adapter = self.adapter()

        self.assertTrue(adapter.ready())
        self.assertTrue(adapter.resume("a" * 40))

        self.assertEqual(("POST", "/api/v2/auth/login", {"username": ["fixture-user"], "password": ["fixture-pass"]}), self.requests[0])
        self.assertIn(("POST", "/api/v2/torrents/start", {"hashes": ["a" * 40]}), self.requests)

    def test_missing_paused_setting_or_ambiguous_observation_fails_closed(self) -> None:
        adapter = self.adapter()
        adapter._get_json = lambda _: {}  # type: ignore[method-assign]
        self.assertFalse(adapter.ready())
        adapter._login = lambda: True  # type: ignore[method-assign]
        adapter._get_json = lambda _: []  # type: ignore[method-assign]
        self.assertEqual("absent", adapter.observe_existing_stopped("a" * 40, "media-interlock").kind)
        adapter._get_json = lambda _: [{"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}, {"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}]  # type: ignore[method-assign]
        self.assertEqual("ambiguous", adapter.observe_existing_stopped("a" * 40, "media-interlock").kind)
        adapter._get_json = lambda _: {"unexpected": "schema"}  # type: ignore[method-assign]
        self.assertEqual("unknown", adapter.observe_existing_stopped("a" * 40, "media-interlock").kind)

    def test_error_or_unknown_transfer_state_is_not_active(self) -> None:
        adapter = self.adapter()
        for state in ("error", "missingFiles", "checkingResumeData", "unknown"):
            with self.subTest(state=state):
                adapter._get_json = lambda _: [{"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": state}]  # type: ignore[method-assign]
                self.assertEqual("unknown", adapter.observe_active("a" * 40, "fence-r-1", "media-interlock").kind)

    def test_pre_v5_application_or_web_api_profile_is_unready(self) -> None:
        adapter = self.adapter()
        adapter._get_text = lambda path: "4.6.7" if path.endswith("version") else "2.11.3"  # type: ignore[method-assign]
        self.assertFalse(adapter.ready())

    def test_authenticated_login_redirect_is_not_followed(self) -> None:
        self.redirect_login = True
        self.assertFalse(self.adapter().ready())
        self.assertEqual([("POST", "/api/v2/auth/login", {"username": ["fixture-user"], "password": ["fixture-pass"]})], self.requests)

    def test_existing_arr_torrent_must_be_exactly_stopped_before_fence_tagging(self) -> None:
        adapter = self.adapter()
        adapter._login = lambda: True  # type: ignore[method-assign]
        adapter._get_json = lambda _: [{"tags": "", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}]  # type: ignore[method-assign]
        posts: list[tuple[str, dict[str, str]]] = []
        adapter._post = lambda path, fields: posts.append((path, fields)) or b"Ok."  # type: ignore[method-assign]

        observation = adapter.observe_existing_stopped("a" * 40, "media-interlock")
        self.assertEqual("observed", observation.kind)
        self.assertEqual(400, observation.observed_bytes)
        self.assertTrue(adapter.apply_reservation_tag("a" * 40, "fence-r-1"))

        self.assertEqual([("/api/v2/torrents/addTags", {"hashes": "a" * 40, "tags": "fence-r-1"})], posts)
