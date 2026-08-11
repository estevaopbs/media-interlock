from __future__ import annotations

import threading
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import _source_tree  # noqa: F401

from media_interlock.adapters.jellyfin import CatalogExpectation, CatalogObservation, JellyfinAdapter
from media_interlock.config import SecretReference


class JellyfinAdapterTests(unittest.TestCase):
    def test_catalog_observation_can_require_explicit_provider_absence(self) -> None:
        expected = CatalogExpectation(
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
            internal_path="/jellyfin/library/import/payload.mkv",
            item_type="Movie",
            provider_ids={},
            expected_bytes=11,
        )
        self.items = [{
            "Id": "movie-item", "Path": expected.internal_path, "Type": "Movie", "ProviderIds": {},
            "MediaSources": [{"Id": "source-a", "Path": expected.internal_path, "Size": 11}],
        }]

        self.assertIsNotNone(self.adapter().observe_catalog(expected))
        self.items[0]["ProviderIds"] = {"Tmdb": "42"}
        self.assertIsNone(self.adapter().observe_catalog(expected))
    def test_refresh_204_is_only_submission_not_delivery(self) -> None:
        adapter = self.adapter()

        submission = adapter.submit_update("/media/library/radarr-movie-a/payload", "modified")

        self.assertTrue(submission.accepted)
        self.assertFalse(submission.delivered)

    def test_catalog_observation_requires_one_exact_item_and_source(self) -> None:
        self.items = [
            {
                "Id": "movie-item",
                "Path": "/jellyfin/library/radarr-movie-a/payload",
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "42"},
                "MediaSources": [{"Id": "source-a", "Path": "/jellyfin/library/radarr-movie-a/payload", "Size": 11}],
            }
        ]

        observation = self.adapter().observe_catalog(
            CatalogExpectation(
                library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
                internal_path="/jellyfin/library/radarr-movie-a/payload",
                item_type="Movie",
                provider_ids={"Tmdb": "42"},
                expected_bytes=11,
            )
        )

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual("movie-item", observation.item_id)
        self.assertEqual("source-a", observation.media_source_id)
        self.assertIn("ParentId=2f9e0f39-70de-4502-85ce-7ed03cd2f01f", self.requests[0][1])
        self.assertIn("Limit=10", self.requests[0][1])
        self.assertNotIn("Path", parse_qs(urlsplit(self.requests[0][1]).query))

    def test_catalog_observation_fails_closed_for_zero_multiple_or_divergent_items(self) -> None:
        expected = CatalogExpectation(
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
            internal_path="/jellyfin/library/radarr-movie-a/payload",
            item_type="Movie",
            provider_ids={"Tmdb": "42"},
            expected_bytes=11,
        )
        exact = {
            "Id": "movie-item",
            "Path": expected.internal_path,
            "Type": "Movie",
            "ProviderIds": {"Tmdb": "42"},
            "MediaSources": [{"Id": "source-a", "Path": expected.internal_path, "Size": 11}],
        }
        for items in ([], [exact, {**exact, "Id": "movie-item-2"}], [{**exact, "ProviderIds": {"Imdb": "tt42"}}]):
            with self.subTest(items=items):
                self.items = items
                self.assertIsNone(self.adapter().observe_catalog(expected))

    def test_catalog_observation_fails_closed_when_a_known_item_identity_changes(self) -> None:
        expected = CatalogExpectation(
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
            internal_path="/jellyfin/library/radarr-movie-a/payload",
            item_type="Movie",
            provider_ids={"Tmdb": "42"},
            expected_bytes=11,
            known_item_id="previous-item",
        )
        self.items = [{
            "Id": "replacement-item", "Path": expected.internal_path, "Type": "Movie",
            "ProviderIds": {"Tmdb": "42"},
            "MediaSources": [{"Id": "source-a", "Path": expected.internal_path, "Size": 11}],
        }]

        self.assertIsNone(self.adapter().observe_catalog(expected))

    def test_direct_play_requires_the_observed_item_source_size_and_hash(self) -> None:
        self.stream_body = b"sealed-media"
        observation = CatalogObservation(
            "movie-item", "source-a", "/jellyfin/library/radarr-movie-a/payload", len(self.stream_body)
        )

        self.assertTrue(self.adapter().direct_play_matches(observation, expected_bytes=len(self.stream_body), expected_sha256=sha256(self.stream_body).hexdigest()))
        self.assertFalse(self.adapter().direct_play_matches(observation, expected_bytes=len(self.stream_body), expected_sha256="0" * 64))
        self.assertTrue(any(path.endswith("MediaSourceId=source-a&static=true") for method, path, _ in self.requests if method == "GET"))
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        self.items: list[object] = []
        self.stream_body: bytes | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append(("GET", self.path, self.headers.get("X-Emby-Token")))
                if self.path == "/System/Info":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"Version":"10.11.11"}')
                    return
                if urlsplit(self.path).path == "/Items":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    body = ('{"Items":' + __import__("json").dumps(outer.items) + ',"TotalRecordCount":' + str(len(outer.items)) + "}").encode("utf-8")
                    self.wfile.write(body)
                    return
                if urlsplit(self.path).path == "/Videos/movie-item/stream" and outer.stream_body is not None:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(outer.stream_body)))
                    self.end_headers()
                    self.wfile.write(outer.stream_body)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                outer.requests.append(("POST", self.path, self.headers.get("X-Emby-Token")))
                if self.path in {"/Library/Refresh", "/Library/Media/Updated"}:
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def adapter(self) -> JellyfinAdapter:
        host, port = self.server.server_address
        return JellyfinAdapter(f"http://{host}:{port}", SecretReference("env", "JELLYFIN_KEY"), secret_resolver=lambda _: "fixture-key")

    def test_readiness_and_refresh_use_authenticated_public_api(self) -> None:
        adapter = self.adapter()

        self.assertTrue(adapter.ready())
        self.assertFalse(adapter.deliver("operation", "generation"))

        self.assertEqual([("GET", "/System/Info", "fixture-key"), ("POST", "/Library/Refresh", "fixture-key")], self.requests)

    def test_unknown_version_or_upstream_failure_is_unready_or_undelivered(self) -> None:
        adapter = self.adapter()
        adapter._get_json = lambda _: {"Version": "10.12.0"}  # type: ignore[method-assign]
        self.assertFalse(adapter.ready())
        adapter._post = lambda _: False  # type: ignore[method-assign]
        self.assertFalse(adapter.deliver("operation", "generation"))
