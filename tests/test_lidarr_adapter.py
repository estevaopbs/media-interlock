from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _source_tree  # noqa: F401

from media_interlock.adapters.lidarr import LidarrAdapter
from media_interlock.config import MusicFencePolicy, ReconciliationPolicy, SecretReference


class LidarrAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, object | None]] = []
        self.missing: object = {
            "records": [
                {"id": 42, "monitored": True, "releaseDate": "2020-01-01T00:00:00Z"},
                {"id": 43, "monitored": False, "releaseDate": "2020-01-01T00:00:00Z"},
            ],
            "totalRecords": 2,
        }
        self.releases: object = [
            {
                "approved": True,
                "downloadAllowed": True,
                "protocol": "torrent",
                "guid": "unknown-seed",
                "title": "Unknown seed album",
                "size": 400,
                "downloadUrl": "https://indexer.invalid/unknown",
                "albumId": 42,
                "seeders": None,
                "indexer": "unreliable-indexer",
                "infoHash": "a" * 40,
                "customFormatScore": 7,
                "customFormats": [{"name": "Lossless"}],
            },
            {
                "approved": True,
                "downloadAllowed": True,
                "protocol": "torrent",
                "guid": "seeded",
                "title": "Seeded album",
                "size": 500,
                "magnetUrl": "magnet:?xt=urn:btih:bbbb",
                "albumId": 42,
                "seeders": 3,
                "indexer": "reliable-indexer",
                "infoHash": "b" * 40,
                "customFormatScore": 9,
                "customFormats": [{"name": "Lossless"}],
            },
        ]
        self.history: object = {
            "records": [{
                "id": 10,
                "eventType": "grabbed",
                "albumId": 42,
                "sourceTitle": "Seeded album",
                "downloadId": "b" * 40,
            }],
            "totalRecords": 1,
        }
        self.queue: object = {
            "records": [{
                "albumId": 42,
                "title": "Seeded album",
                "downloadId": "b" * 40,
                "protocol": "torrent",
                "size": 500,
            }],
            "totalRecords": 1,
        }
        self.download_clients: object = [{
            "id": 9,
            "enable": True,
            "protocol": "torrent",
            "implementation": "QBittorrent",
            "name": "qBittorrent stopped music",
            "fields": [
                {"name": "musicCategory", "value": "media-interlock-lidarr"},
                {"name": "initialState", "value": 2},
            ],
        }]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append(("GET", self.path, self.headers.get("X-Api-Key")))
                if self.path.startswith("/api/v1/wanted/missing?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.missing).encode("utf-8"))
                    return
                if self.path == "/api/v1/release?albumId=42":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.releases).encode("utf-8"))
                    return
                if self.path.startswith("/api/v1/history?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.history).encode("utf-8"))
                    return
                if self.path.startswith("/api/v1/queue?"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.queue).encode("utf-8"))
                    return
                if self.path == "/api/v1/downloadclient":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.download_clients).encode("utf-8"))
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(("POST", self.path, body))
                if self.path == "/api/v1/release":
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

    def test_reads_monitored_missing_albums_from_lidarr_v1_paging(self) -> None:
        host, port = self.server.server_address
        adapter = LidarrAdapter(
            f"http://{host}:{port}",
            SecretReference("env", "LIDARR_KEY"),
            secret_resolver=lambda _: "fixture-key",
        )

        monitored = adapter.missing_monitored_albums()

        self.assertEqual(("42",), tuple(album.album_id for album in monitored))
        self.assertEqual(1_577_836_800, monitored[0].released_at)
        self.assertEqual(
            ("GET", "/api/v1/wanted/missing?page=1&pageSize=100", "fixture-key"),
            self.requests[-1],
        )

    def test_selects_the_first_native_release_with_usable_seed_evidence(self) -> None:
        host, port = self.server.server_address
        adapter = LidarrAdapter(
            f"http://{host}:{port}",
            SecretReference("env", "LIDARR_KEY"),
            secret_resolver=lambda _: "fixture-key",
        )
        policy = ReconciliationPolicy(
            minimum_age_days=0,
            terminal_horizon_days=365,
            cooldown_seconds=86_400,
            cooldown_step_days=7,
            cooldown_multiplier=2.0,
            maximum_cooldown_seconds=0,
            final_search=False,
            max_attempts=8,
            max_searches_per_run=6,
            max_searches_per_hour=10,
            max_searches_per_day=20,
            max_grabs_per_run=1,
            minimum_candidate_score=0,
            minimum_score_gain=0,
            required_candidate_formats=("Lossless",),
            forbidden_candidate_formats=(),
        )
        health = MusicFencePolicy(
            minimum_reported_seeders=1,
            unknown_seeders_policy="reject",
            probe_only_indexers=(),
            metadata_probe_seconds=900,
            no_progress_seconds=3600,
            max_candidates_per_cycle=3,
            delete_invalid_payload=True,
        )

        selected = adapter.first_approved_release(
            adapter.album_releases("42"), policy, health, current_score=0
        )

        assert selected is not None
        self.assertEqual("seeded", selected.resource["guid"])
        self.assertEqual(("GET", "/api/v1/release?albumId=42", "fixture-key"), self.requests[-1])

    def test_posts_the_sealed_release_and_accepts_only_its_observed_hash(self) -> None:
        host, port = self.server.server_address
        adapter = LidarrAdapter(
            f"http://{host}:{port}",
            SecretReference("env", "LIDARR_KEY"),
            secret_resolver=lambda _: "fixture-key",
        )
        release = adapter.album_releases("42")[1]

        self.assertTrue(adapter.grab_release(release))
        observation = adapter.observe_grab("42", release, watermark=9)

        posted = [request for request in self.requests if request[0] == "POST"]
        self.assertEqual([( "POST", "/api/v1/release", self.releases[1])], posted)
        self.assertEqual("observed", observation.kind)
        self.assertEqual("b" * 40, observation.torrent_hash)
        self.assertTrue(adapter.stopped_qbittorrent_client("media-interlock-lidarr", 9))
