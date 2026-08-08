from __future__ import annotations

import json
import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.adapters.sonarr import SonarrAdapter
from media_interlock.config import SecretReference


class ArrCorrelationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload: object = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "episodeId": 42, "data": {"importedPath": "/staging/movie.mkv"}}]}
        self.entity_payload: object = {"id": 42, "tmdbId": 42}
        self.request: tuple[str, str | None] | None = None
        self.command_request: tuple[str, str | None, object] | None = None
        self.release_payload: object = [{"approved": True, "protocol": "torrent", "guid": "release-42", "size": 400, "downloadUrl": "https://indexer.invalid/release"}]
        self.release_post: object | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.request = (self.path, self.headers.get("X-Api-Key"))
                if self.path.startswith("/api/v3/release?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.release_payload).encode())
                    return
                if self.path.startswith("/api/v3/history?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.payload).encode())
                    return
                if self.path == "/api/v3/movie/42":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.entity_payload).encode())
                    return
                if self.path == "/api/v3/episode/42":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"id": 42, "tvdbId": 99}).encode())
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                outer.command_request = (self.path, self.headers.get("X-Api-Key"), body)
                if self.path == "/api/v3/release":
                    outer.release_post = body
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.path != "/api/v3/command":
                    self.send_error(404)
                    return
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":71}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def adapter(self, type_: type[RadarrAdapter] | type[SonarrAdapter]):
        host, port = self.server.server_address
        return type_(f"http://{host}:{port}", SecretReference("env", "ARR_KEY"), staging_root=Path("/staging"), secret_resolver=lambda _: "fixture-key")

    def test_exact_single_import_is_correlated_for_radarr_and_sonarr(self) -> None:
        for adapter_type in (RadarrAdapter, SonarrAdapter):
            with self.subTest(adapter=adapter_type.__name__):
                self.assertEqual("movie.mkv", self.adapter(adapter_type).candidate_relative_path("grab-42", "42"))
                assert self.request is not None
                self.assertIn("downloadId=grab-42", self.request[0])
                self.assertEqual("fixture-key", self.request[1])

    def test_ambiguous_or_outside_import_fails_closed(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        self.payload = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": "/staging/a.mkv"}}, {"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": "/staging/b.mkv"}}]}
        self.assertIsNone(adapter.candidate_relative_path("grab-42", "42"))
        self.payload = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": "/outside/movie.mkv"}}]}
        self.assertIsNone(adapter.candidate_relative_path("grab-42", "42"))
        self.assertIsNone(adapter.candidate_relative_path("grab-42", "different-media"))

    def test_radarr_derives_asset_slot_and_provider_identity_from_public_api(self) -> None:
        identity = self.adapter(RadarrAdapter).candidate_identity("grab-42", "42")

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual("movie.mkv", identity.relative_path)
        self.assertEqual("radarr:tmdb-42", identity.asset_slot)
        self.assertEqual("Movie", identity.item_type)
        self.assertEqual({"Tmdb": "42"}, identity.provider_ids)

    def test_sonarr_derives_episode_slot_and_provider_identity_from_public_api(self) -> None:
        identity = self.adapter(SonarrAdapter).candidate_identity("grab-42", "42")

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual("sonarr:tvdb-99", identity.asset_slot)
        self.assertEqual("Episode", identity.item_type)
        self.assertEqual({"Tvdb": "99"}, identity.provider_ids)

    def test_native_search_submits_exact_single_public_entity_command(self) -> None:
        self.assertEqual("71", self.adapter(RadarrAdapter).search_movie("42"))
        self.assertEqual(("/api/v3/command", "fixture-key", {"name": "MoviesSearch", "movieIds": [42]}), self.command_request)
        self.assertEqual("71", self.adapter(SonarrAdapter).search_episode("42"))
        self.assertEqual(("/api/v3/command", "fixture-key", {"name": "EpisodeSearch", "episodeIds": [42]}), self.command_request)

    def test_interactive_first_approved_torrent_is_selected_and_posted_intact(self) -> None:
        expected = {"approved": True, "protocol": "torrent", "guid": "release-42", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        adapter = self.adapter(RadarrAdapter)
        release = adapter.first_approved_release("42")
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), release.selector_fingerprint)
        self.assertTrue(adapter.grab_release(release))
        self.assertEqual(expected, self.release_post)
        assert self.request is not None
        self.assertEqual("/api/v3/release?movieId=42", self.request[0])

    def test_interactive_selection_never_skips_an_invalid_first_approved_decision(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        valid = {"approved": True, "protocol": "torrent", "guid": "later", "size": 400, "downloadUrl": "https://indexer.invalid/later"}
        for first in (
            {"approved": True, "protocol": "usenet", "guid": "first", "size": 400, "downloadUrl": "https://indexer.invalid/first"},
            {"approved": True, "protocol": "torrent", "guid": "first", "size": 0, "downloadUrl": "https://indexer.invalid/first"},
            {"approved": True, "protocol": "torrent", "guid": "first", "size": 400},
        ):
            with self.subTest(first=first):
                self.release_payload = [first, valid]
                self.assertIsNone(adapter.first_approved_release("42"))
