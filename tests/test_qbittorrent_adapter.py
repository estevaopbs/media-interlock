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
from media_interlock.fence.model import QbittorrentHealthObservation


class QbittorrentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.authorization: list[str | None] = []
        self.redirect_login = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                outer.authorization.append(self.headers.get("Authorization"))
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
                outer.authorization.append(self.headers.get("Authorization"))
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
        return QbittorrentAdapter(f"http://{host}:{port}", SecretReference("env", "QBIT_USER"), SecretReference("env", "QBIT_PASS"), secret_resolver=lambda reference: {"QBIT_USER": "fixture-user", "QBIT_PASS": "fixture-pass"}[reference.reference])

    def test_readiness_and_resume_use_documented_cookie_api(self) -> None:
        adapter = self.adapter()

        self.assertTrue(adapter.ready())
        self.assertTrue(adapter.resume("a" * 40))

        self.assertEqual(("POST", "/api/v2/auth/login", {"username": ["fixture-user"], "password": ["fixture-pass"]}), self.requests[0])
        self.assertIn(("POST", "/api/v2/torrents/start", {"hashes": ["a" * 40]}), self.requests)

    def test_api_key_authentication_skips_login_and_is_sent_on_every_request(self) -> None:
        host, port = self.server.server_address
        adapter = QbittorrentAdapter(
            f"http://{host}:{port}",
            None,
            None,
            api_key=SecretReference("env", "QBIT_API_KEY"),
            secret_resolver=lambda _: "fixture-api-key",
        )

        self.assertTrue(adapter.ready())
        self.assertTrue(adapter.resume("a" * 40))
        self.assertFalse(any(path == "/api/v2/auth/login" for _, path, _ in self.requests))
        self.assertTrue(self.authorization)
        self.assertEqual({"Bearer fixture-api-key"}, set(self.authorization))

    def test_global_start_paused_preference_does_not_replace_per_arr_stop_contracts(self) -> None:
        adapter = self.adapter()
        adapter._get_json = lambda _: {"start_paused_enabled": False}  # type: ignore[method-assign]
        self.assertTrue(adapter.ready())
        adapter._login = lambda: True  # type: ignore[method-assign]
        adapter._get_json = lambda _: []  # type: ignore[method-assign]
        self.assertEqual("absent", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging")).kind)
        adapter._get_json = lambda _: [{"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}, {"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": "pausedDL"}]  # type: ignore[method-assign]
        self.assertEqual("ambiguous", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging")).kind)
        adapter._get_json = lambda _: {"unexpected": "schema"}  # type: ignore[method-assign]
        self.assertEqual("unknown", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging")).kind)

    def test_error_or_unknown_transfer_state_is_not_active(self) -> None:
        adapter = self.adapter()
        for state in ("error", "missingFiles", "checkingResumeData", "unknown"):
            with self.subTest(state=state):
                adapter._get_json = lambda _: [{"tags": "fence-r-1", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 400, "state": state}]  # type: ignore[method-assign]
                self.assertEqual("unknown", adapter.observe_active("a" * 40, "fence-r-1", "media-interlock", save_path=Path("/staging")).kind)

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

        observation = adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging"))
        self.assertEqual("observed", observation.kind)
        self.assertEqual(400, observation.observed_bytes)
        self.assertTrue(adapter.apply_reservation_tag("a" * 40, "fence-r-1"))

        self.assertEqual([("/api/v2/torrents/addTags", {"hashes": "a" * 40, "tags": "fence-r-1"})], posts)

    def test_stopped_magnet_without_metadata_is_distinguished_and_can_start(self) -> None:
        adapter = self.adapter()
        adapter._login = lambda: True  # type: ignore[method-assign]
        torrent = {"tags": "", "category": "media-interlock", "save_path": "/staging", "hash": "a" * 40, "size": 0, "amount_left": 0, "state": "stoppedDL", "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40}
        adapter._get_json = lambda _: [torrent]  # type: ignore[method-assign]

        self.assertEqual("metadata_pending", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging")).kind)
        torrent.pop("magnet_uri")
        self.assertEqual("unknown", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging")).kind)
        reservation_id = "fence:12345678-1234-4678-9234-567812345678"
        torrent.update(tags=reservation_id, state="metaDL", magnet_uri="magnet:?xt=urn:btih:" + "a" * 40)
        observation = adapter.observe_active("a" * 40, reservation_id, "media-interlock", save_path=Path("/staging"))
        self.assertEqual("observed", observation.kind)
        self.assertTrue(observation.active)
        self.assertIsNone(observation.observed_bytes)

    def test_owned_active_candidate_health_requires_exact_video_ownership(self) -> None:
        adapter = self.adapter()
        adapter._login = lambda: True  # type: ignore[method-assign]
        reservation_id = "fence:12345678-1234-4678-9234-567812345678"
        torrent = {
            "tags": reservation_id, "category": "media-interlock-radarr", "save_path": "/downloads/radarr",
            "hash": "a" * 40, "size": 0, "total_size": -1, "downloaded": 0,
            "availability": 0, "num_seeds": 0, "num_leechs": 0, "state": "queuedDL",
        }
        adapter._get_json = lambda _: [torrent]  # type: ignore[method-assign]

        observed = adapter.observe_candidate_health(
            "a" * 40, reservation_id, "media-interlock-radarr", save_path=Path("/downloads/radarr")
        )

        self.assertEqual(QbittorrentHealthObservation("observed", metadata_known=False, downloaded_bytes=0, availability=0.0, peers=0), observed)
        self.assertEqual(
            "unknown",
            adapter.observe_candidate_health("a" * 40, reservation_id, "music", save_path=Path("/downloads/radarr")).kind,
        )

    def test_observation_compares_the_profile_save_path_not_a_staging_root(self) -> None:
        adapter = self.adapter()
        adapter._login = lambda: True  # type: ignore[method-assign]
        adapter._get_json = lambda _: [{"tags": "", "category": "media-interlock", "save_path": "/downloads/radarr", "hash": "a" * 40, "size": 400, "state": "pausedDL"}]  # type: ignore[method-assign]

        self.assertEqual("observed", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/downloads/radarr")).kind)
        self.assertEqual("unknown", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/staging/radarr")).kind)

    def test_existing_fence_owner_tag_inhibits_a_second_reservation(self) -> None:
        adapter = self.adapter()
        adapter._login = lambda: True  # type: ignore[method-assign]
        adapter._get_json = lambda _: [{"tags": "manual,fence:12345678-1234-4678-9234-567812345678", "category": "media-interlock", "save_path": "/downloads/radarr", "hash": "a" * 40, "size": 400, "state": "pausedDL"}]  # type: ignore[method-assign]

        self.assertEqual("unknown", adapter.observe_existing_stopped("a" * 40, "media-interlock", save_path=Path("/downloads/radarr")).kind)
