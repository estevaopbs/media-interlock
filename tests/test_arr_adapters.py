from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.adapters.sonarr import SonarrAdapter
from media_interlock.config import SecretReference
from media_interlock.publisher.filesystem import CandidateSafetyError, CandidateVerifier


class ArrCorrelationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload: object = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "episodeId": 42, "data": {"importedPath": "/data/library/movie.mkv"}}]}
        self.entity_payload: object = {"id": 42, "tmdbId": 42}
        self.request: tuple[str, str | None] | None = None
        self.command_request: tuple[str, str | None, object] | None = None
        self.release_payload: object = [{"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}]
        self.queue_payload: object = {"records": []}
        self.download_clients: object = []
        self.cutoff_payload: object = {"records": [], "totalRecords": 0}
        self.series_payload: object = []
        self.profiles_payload: object = []
        self.episodes_payload: object = []
        self.movies_payload: object = []
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
                if self.path.startswith("/api/v3/queue?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.queue_payload).encode())
                    return
                if self.path.startswith("/api/v3/wanted/cutoff?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.cutoff_payload).encode())
                    return
                if self.path == "/api/v3/series":
                    data = outer.series_payload
                elif self.path == "/api/v3/qualityprofile":
                    data = outer.profiles_payload
                elif self.path.startswith("/api/v3/episode?"):
                    data = outer.episodes_payload
                elif self.path == "/api/v3/movie?includeMovieFile=true":
                    data = outer.movies_payload
                else:
                    data = None
                if data is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                    return
                if self.path == "/api/v3/downloadclient":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.download_clients).encode())
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
                self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def adapter(self, type_: type[RadarrAdapter] | type[SonarrAdapter], *, staging_root: Path = Path("/staging/movies")):
        host, port = self.server.server_address
        return type_(
            f"http://{host}:{port}", SecretReference("env", "ARR_KEY"),
            arr_import_path_prefix="/data/library", staging_root=staging_root,
            secret_resolver=lambda _: "fixture-key",
        )

    def test_exact_single_import_is_correlated_for_radarr_and_sonarr(self) -> None:
        for adapter_type in (RadarrAdapter, SonarrAdapter):
            with self.subTest(adapter=adapter_type.__name__):
                self.assertEqual("movie.mkv", self.adapter(adapter_type).candidate_relative_path("grab-42", "42"))
                assert self.request is not None
                self.assertIn("downloadId=grab-42", self.request[0])
                self.assertEqual("fixture-key", self.request[1])

    def test_incremental_import_history_uses_history_id_and_does_not_escape_staging(self) -> None:
        self.payload = {"records": [
            {"id": 7, "eventType": "downloadFolderImported", "downloadId": "grab-7", "movieId": 42, "data": {"importedPath": "/data/library/movie.mkv"}},
            {"id": 8, "eventType": "downloadFolderImported", "downloadId": "grab-8", "movieId": 43, "data": {"importedPath": "/outside/ignored.mkv"}},
            {"id": 9, "eventType": "other", "data": {}},
        ], "totalRecords": 3}

        cursor, imports = self.adapter(RadarrAdapter).imported_after(0, maximum=8)

        self.assertEqual(9, cursor)
        self.assertEqual(((7, "grab-7", "42", "movie.mkv"),), tuple(
            (item.history_id, item.download_id, item.media_id, item.relative_path) for item in imports
        ))

    def test_initial_import_lookback_skips_old_events_but_advances_the_cursor(self) -> None:
        self.payload = {"records": [
            {"id": 7, "eventType": "downloadFolderImported", "date": "2026-08-01T00:00:00Z", "downloadId": "grab-7", "movieId": 42, "data": {"importedPath": "/data/library/old.mkv"}},
            {"id": 8, "eventType": "downloadFolderImported", "date": "2026-08-10T00:00:00Z", "downloadId": "grab-8", "movieId": 43, "data": {"importedPath": "/data/library/recent.mkv"}},
        ], "totalRecords": 2}

        cursor, imports = self.adapter(RadarrAdapter).imported_after(
            0,
            maximum=8,
            not_before="2026-08-08T00:00:00Z",
        )

        self.assertEqual(8, cursor)
        self.assertEqual(((8, "grab-8", "43", "recent.mkv"),), tuple(
            (item.history_id, item.download_id, item.media_id, item.relative_path) for item in imports
        ))

    def test_shared_arr_prefix_maps_nested_files_to_distinct_publisher_stagings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie_staging = root / "staging" / "movies"
            show_staging = root / "staging" / "shows"
            for staging, payload in ((movie_staging, b"movie"), (show_staging, b"episode")):
                (staging / "title").mkdir(parents=True)
                (staging / "title" / "media.mkv").write_bytes(payload)
                (staging / "title" / "media.en.srt").write_bytes(b"subtitle")
            self.payload = {"records": [{
                "eventType": "downloadFolderImported", "downloadId": "grab-42",
                "movieId": 42, "episodeId": 42,
                "data": {"importedPath": "/data/library/title/media.mkv"},
            }]}

            movie_relative = self.adapter(RadarrAdapter, staging_root=movie_staging).candidate_relative_path("grab-42", "42")
            show_relative = self.adapter(SonarrAdapter, staging_root=show_staging).candidate_relative_path("grab-42", "42")

            self.assertEqual("title/media.mkv", movie_relative)
            self.assertEqual("title/media.mkv", show_relative)
            assert movie_relative is not None and show_relative is not None
            self.assertEqual(b"movie", (movie_staging / movie_relative).read_bytes())
            self.assertEqual(b"episode", (show_staging / show_relative).read_bytes())
            self.assertEqual(b"subtitle", (movie_staging / "title" / "media.en.srt").read_bytes())
            self.assertEqual(b"subtitle", (show_staging / "title" / "media.en.srt").read_bytes())

    def test_directory_and_symlink_never_become_filesystem_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            outside = root / "outside.mkv"
            staging.mkdir()
            (staging / "title").mkdir()
            outside.write_bytes(b"outside")
            (staging / "title" / "media.mkv").symlink_to(outside)
            adapter = self.adapter(RadarrAdapter, staging_root=staging)

            self.payload = {"records": [{
                "eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42,
                "data": {"importedPath": "/data/library/title"},
            }]}
            self.assertEqual("title", adapter.candidate_relative_path("grab-42", "42"))
            with self.assertRaises(CandidateSafetyError):
                CandidateVerifier(staging).verify("title")

            self.payload = {"records": [{
                "eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42,
                "data": {"importedPath": "/data/library/title/media.mkv"},
            }]}
            self.assertEqual("title/media.mkv", adapter.candidate_relative_path("grab-42", "42"))
            with self.assertRaises(CandidateSafetyError):
                CandidateVerifier(staging).verify("title/media.mkv")

    def test_ambiguous_or_outside_import_fails_closed(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        self.payload = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": "/data/library/a.mkv"}}, {"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": "/data/library/b.mkv"}}]}
        self.assertIsNone(adapter.candidate_relative_path("grab-42", "42"))
        for imported_path in (
            "/outside/movie.mkv",
            "/data/library-other/movie.mkv",
            "/data/library",
            "data/library/movie.mkv",
            "/data/library/../outside/movie.mkv",
            "/data/library/title/../../outside/movie.mkv",
            "/data//library/movie.mkv",
        ):
            with self.subTest(imported_path=imported_path):
                self.payload = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": imported_path}}]}
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

    def test_sonarr_never_substitutes_a_matching_series_for_the_requested_episode(self) -> None:
        self.payload = {"records": [{
            "eventType": "downloadFolderImported", "downloadId": "grab-42",
            "episodeId": 99, "seriesId": 42,
            "data": {"importedPath": "/data/library/wrong-episode.mkv"},
        }]}
        adapter = self.adapter(SonarrAdapter)

        self.assertIsNone(adapter.candidate_relative_path("grab-42", "42"))

    def test_interactive_first_approved_torrent_is_selected_and_posted_intact(self) -> None:
        expected = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        adapter = self.adapter(RadarrAdapter)
        release = adapter.first_approved_release("42")
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), release.selector_fingerprint)
        self.assertTrue(adapter.grab_release(release))
        self.assertEqual(expected, self.release_post)
        assert self.request is not None
        self.assertEqual("/api/v3/release?movieId=42", self.request[0])

    def test_cutoff_inventory_exposes_release_age_generation_and_current_score(self) -> None:
        self.cutoff_payload = {
            "records": [{
                "id": 42,
                "airDateUtc": "2026-08-02T15:00:00Z",
                "episodeFileId": 7,
                "episodeFile": {"id": 7, "customFormatScore": 4_000},
            }],
            "totalRecords": 1,
        }

        entities = self.adapter(SonarrAdapter).cutoff_entities()

        self.assertIsNotNone(entities)
        assert entities is not None
        self.assertEqual(1, len(entities))
        self.assertEqual("sonarr", entities[0].source)
        self.assertEqual("42", entities[0].entity_id)
        self.assertEqual("7", entities[0].generation)
        self.assertEqual(4_000, entities[0].current_score)
        self.assertEqual(1_785_682_800, entities[0].released_at)

    def test_full_inventory_keeps_unmonitored_files_below_custom_format_cutoff(self) -> None:
        self.series_payload = [{"id": 5, "qualityProfileId": 8}]
        self.profiles_payload = [{
            "id": 8,
            "upgradeAllowed": True,
            "cutoff": 3,
            "cutoffFormatScore": 8_000,
            "minUpgradeFormatScore": 1,
            "items": [{"quality": {"id": 3}, "items": [], "allowed": True}],
        }]
        self.episodes_payload = [{
            "id": 42,
            "hasFile": True,
            "monitored": False,
            "airDateUtc": "2026-08-02T15:00:00Z",
            "episodeFileId": 7,
            "episodeFile": {
                "id": 7,
                "customFormatScore": 4_000,
                "quality": {"quality": {"id": 3}},
            },
        }]

        entities = self.adapter(SonarrAdapter).upgrade_entities()

        self.assertIsNotNone(entities)
        assert entities is not None
        self.assertEqual(("42",), tuple(entity.entity_id for entity in entities))
        self.assertEqual(4_000, entities[0].current_score)

    def test_radarr_full_inventory_finds_existing_file_below_profile_cutoff(self) -> None:
        self.profiles_payload = [{
            "id": 7,
            "upgradeAllowed": True,
            "cutoff": 3,
            "cutoffFormatScore": 57_000,
            "minUpgradeFormatScore": 1,
            "items": [{"quality": {"id": 3}, "items": [], "allowed": True}],
        }]
        self.movies_payload = [{
            "id": 42,
            "hasFile": True,
            "qualityProfileId": 7,
            "digitalRelease": "2026-08-02T15:00:00Z",
            "movieFileId": 9,
            "movieFile": {
                "id": 9,
                "customFormatScore": 4_000,
                "quality": {"quality": {"id": 3}},
            },
        }]

        entities = self.adapter(RadarrAdapter).upgrade_entities()

        self.assertIsNotNone(entities)
        assert entities is not None
        self.assertEqual(("42",), tuple(entity.entity_id for entity in entities))
        self.assertEqual("9", entities[0].generation)
        self.assertEqual(4_000, entities[0].current_score)

    def test_candidate_policy_filters_scores_and_formats_without_reordering_arr(self) -> None:
        self.release_payload = [
            {
                "approved": True, "protocol": "torrent", "guid": "original",
                "title": "fixture.original", "size": 400,
                "downloadUrl": "https://indexer.invalid/original",
                "customFormatScore": 4_000,
                "customFormats": [{"name": "Original"}],
            },
            {
                "approved": True, "protocol": "torrent", "guid": "dual",
                "title": "fixture.dual", "size": 500,
                "downloadUrl": "https://indexer.invalid/dual",
                "customFormatScore": 8_000,
                "customFormats": [{"name": "Original + PT-BR"}, {"name": "Erai-raws"}],
            },
        ]
        configured = __import__("test_reconciler_scheduler").policy(
            minimum_candidate_score=8_000,
            minimum_score_gain=1_000,
            required_candidate_formats=("Original + PT-BR",),
            forbidden_candidate_formats=("AI upscale",),
        )

        result = self.adapter(SonarrAdapter).approved_release("42", configured, current_score=4_000)

        self.assertTrue(result.available)
        self.assertIsNotNone(result.release)
        assert result.release is not None
        self.assertEqual("fixture.dual", result.release.resource["title"])

    def test_valid_empty_interactive_search_is_distinct_from_adapter_failure(self) -> None:
        self.release_payload = []

        result = self.adapter(SonarrAdapter).approved_release(
            "42", __import__("test_reconciler_scheduler").policy(), current_score=4_000
        )

        self.assertTrue(result.available)
        self.assertIsNone(result.release)

    def test_interactive_selection_never_skips_an_invalid_first_approved_decision(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        valid = {"approved": True, "protocol": "torrent", "guid": "later", "title": "fixture.later", "size": 400, "downloadUrl": "https://indexer.invalid/later"}
        for first in (
            {"approved": True, "protocol": "usenet", "guid": "first", "title": "fixture.first", "size": 400, "downloadUrl": "https://indexer.invalid/first"},
            {"approved": True, "protocol": "torrent", "guid": "first", "title": "fixture.first", "size": 0, "downloadUrl": "https://indexer.invalid/first"},
            {"approved": True, "protocol": "torrent", "guid": "first", "title": "fixture.first", "size": 400},
            {"approved": True, "protocol": "torrent", "guid": "first", "size": 400, "downloadUrl": "https://indexer.invalid/first"},
        ):
            with self.subTest(first=first):
                self.release_payload = [first, valid]
                self.assertIsNone(adapter.first_approved_release("42"))

    def test_post_grab_requires_one_later_history_and_queue_match(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        release = adapter.first_approved_release("42")
        assert release is not None
        download_id = "A" * 40
        self.payload = {"records": [{"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie.2026", "downloadId": download_id}], "totalRecords": 1}
        self.queue_payload = {"records": [{"id": 9, "movieId": 42, "title": "fixture.movie.2026", "downloadId": download_id, "protocol": "torrent", "size": 400}], "totalRecords": 1}

        observation = adapter.observe_grab("42", release, watermark=7)

        self.assertEqual("observed", observation.kind)
        self.assertEqual(download_id, observation.download_id)
        self.assertEqual(download_id.lower(), observation.torrent_hash)

    def test_post_grab_accepts_queue_entry_waiting_for_magnet_metadata(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        release = adapter.first_approved_release("42")
        assert release is not None
        download_id = "A" * 40
        self.payload = {"records": [{"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie.2026", "downloadId": download_id}], "totalRecords": 1}
        self.queue_payload = {"records": [{"id": 9, "movieId": 42, "title": "fixture.movie.2026", "downloadId": download_id, "protocol": "torrent", "size": 0}], "totalRecords": 1}

        observation = adapter.observe_grab("42", release, watermark=7)

        self.assertEqual("observed", observation.kind)
        self.assertEqual(download_id.lower(), observation.torrent_hash)

    def test_grab_observation_never_converts_absence_or_ambiguity_to_a_grab(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        release = adapter.first_approved_release("42")
        assert release is not None
        self.payload = {"records": [], "totalRecords": 0}
        self.assertEqual("absent", adapter.observe_grab("42", release, watermark=7).kind)

        self.payload = {"records": [
            {"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie.2026", "downloadId": "a" * 40},
            {"id": 9, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie.2026", "downloadId": "b" * 40},
        ], "totalRecords": 2}
        self.assertEqual("ambiguous", adapter.observe_grab("42", release, watermark=7).kind)

    def test_external_grabs_require_a_later_history_event_and_the_configured_client(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        self.download_clients = [{
            "id": 7,
            "name": "media-interlock-radarr",
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        }]
        self.payload = {"records": [
            {"id": 7, "eventType": "grabbed", "movieId": 41, "downloadId": "a" * 40},
            {"id": 8, "eventType": "grabbed", "movieId": 42, "downloadId": "b" * 40},
        ], "totalRecords": 2}
        self.queue_payload = {"records": [{
            "id": 9,
            "movieId": 42,
            "downloadId": "b" * 40,
            "downloadClient": "media-interlock-radarr",
            "protocol": "torrent",
            "size": 400,
        }], "totalRecords": 1}

        observation = adapter.external_grabs_after(7, category="media-interlock-radarr", download_client_id=7)

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(8, observation.watermark)
        self.assertEqual(1, len(observation.grabs))
        self.assertEqual(("42", "b" * 40, "b" * 40, 400, 8), (
            observation.grabs[0].entity_id,
            observation.grabs[0].download_id,
            observation.grabs[0].torrent_hash,
            observation.grabs[0].expected_bytes,
            observation.grabs[0].history_id,
        ))

    def test_external_stopped_magnet_uses_positive_arr_history_size_until_metadata_arrives(self) -> None:
        adapter = self.adapter(SonarrAdapter)
        self.download_clients = [{
            "id": 7,
            "name": "media-interlock-sonarr",
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "tvCategory", "value": "media-interlock-sonarr"}],
        }]
        download_id = "b" * 40
        self.payload = {"records": [{
            "id": 8,
            "eventType": "grabbed",
            "episodeId": 42,
            "downloadId": download_id.upper(),
            "data": {"size": "1503238553"},
        }], "totalRecords": 1}
        self.queue_payload = {"records": [{
            "id": 9,
            "episodeId": 42,
            "downloadId": download_id.upper(),
            "downloadClient": "media-interlock-sonarr",
            "protocol": "torrent",
            "size": 0,
        }], "totalRecords": 1}

        observation = adapter.external_grabs_after(7, category="media-interlock-sonarr", download_client_id=7)

        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(1503238553, observation.grabs[0].expected_bytes)

    def test_external_stopped_magnet_rejects_missing_or_invalid_history_size(self) -> None:
        adapter = self.adapter(SonarrAdapter)
        self.download_clients = [{
            "id": 7,
            "name": "media-interlock-sonarr",
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "tvCategory", "value": "media-interlock-sonarr"}],
        }]
        download_id = "b" * 40
        self.queue_payload = {"records": [{
            "id": 9,
            "episodeId": 42,
            "downloadId": download_id.upper(),
            "downloadClient": "media-interlock-sonarr",
            "protocol": "torrent",
            "size": 0,
        }], "totalRecords": 1}
        for invalid_size in (None, "", "0", "+400", " 400", True, 0, -1):
            with self.subTest(size=invalid_size):
                self.payload = {"records": [{
                    "id": 8,
                    "eventType": "grabbed",
                    "episodeId": 42,
                    "downloadId": download_id.upper(),
                    "data": {} if invalid_size is None else {"size": invalid_size},
                }], "totalRecords": 1}

                self.assertIsNone(adapter.external_grabs_after(7, category="media-interlock-sonarr", download_client_id=7))

    def test_external_grabs_fail_closed_on_missing_queue_or_ambiguous_client_attribution(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        self.download_clients = [{
            "id": 7,
            "name": "media-interlock-radarr",
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        }]
        self.payload = {"records": [{"id": 8, "eventType": "grabbed", "movieId": 42, "downloadId": "b" * 40}], "totalRecords": 1}
        self.queue_payload = {"records": [], "totalRecords": 0}
        self.assertIsNone(adapter.external_grabs_after(7, category="media-interlock-radarr", download_client_id=7))

        self.queue_payload = {"records": [{"movieId": 42, "downloadId": "b" * 40, "downloadClient": "media-interlock-radarr", "protocol": "torrent", "size": 400}], "totalRecords": 1}
        self.download_clients.append({
            "id": 8,
            "name": "same-visible-name",
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        })
        self.assertIsNone(adapter.external_grabs_after(7, category="media-interlock-radarr", download_client_id=7))

    def test_only_one_enabled_source_category_client_with_initial_state_stop_is_ready(self) -> None:
        adapter = self.adapter(RadarrAdapter)
        self.download_clients = [{
            "id": 7,
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [
                {"name": "initialState", "value": 2, "order": 3, "label": "Initial State", "type": "select", "advanced": False, "privacy": "normal"},
                {"name": "movieCategory", "value": "media-interlock-radarr", "order": 4, "label": "Category", "type": "textbox", "advanced": True, "privacy": "normal"},
            ],
        }]
        self.assertTrue(adapter.stopped_qbittorrent_client("media-interlock-radarr", 7))

        self.download_clients = [{
            "id": 7,
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 0}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        }]
        self.assertFalse(adapter.stopped_qbittorrent_client("media-interlock-radarr", 7))

        self.download_clients = [{
            "id": 7,
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        }, {
            "id": 9,
            "enable": True,
            "protocol": "usenet",
            "implementation": "SABnzbd",
            "fields": [],
        }]
        self.assertTrue(adapter.stopped_qbittorrent_client("media-interlock-radarr", 7))

        self.download_clients.append({
            "id": 8,
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}],
        })
        self.assertFalse(adapter.stopped_qbittorrent_client("media-interlock-radarr", 7))
