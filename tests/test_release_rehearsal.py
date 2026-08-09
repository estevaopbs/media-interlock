from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.bazarr import BazarrAdapter
from media_interlock.adapters.jellyfin import CatalogExpectation, CatalogObservation, CatalogSubmission
from media_interlock.adapters.prowlarr import ProwlarrAdapter
from media_interlock.adapters.qbittorrent import QbittorrentAdapter
from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.adapters.seerr import SeerrAdapter
from media_interlock.adapters.sonarr import SonarrAdapter
from media_interlock.config import SecretReference
from media_interlock.config import load_config
from media_interlock.contracts import CONTRACT_VERSION, Envelope
from media_interlock.fence import cli as fence_cli
from media_interlock.fence.store import FenceStore
from media_interlock.publisher.generation import AssetGenerationPublisher
from media_interlock.publisher.model import PublicationState
from media_interlock.publisher import cli as publisher_cli
from media_interlock.publisher.store import PublisherStore
from media_interlock.reconciler import cli as reconciler_cli
from media_interlock.reconciler.store import ReconcilerStore


class ReleaseRehearsalTests(unittest.TestCase):
    def test_all_declared_adapters_use_their_public_http_readiness_boundaries(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass
            def do_POST(self) -> None:
                if self.path == "/api/v2/auth/login":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"Ok."); return
                self.send_error(404)
            def do_GET(self) -> None:
                payload: object
                if self.path == "/System/Info": payload = {"Version": "10.11.11"}
                elif self.path == "/api/system/status": payload = {"data": {"bazarr_version": "1.6.0"}}
                elif self.path == "/api/v1/settings/main": payload = {"applicationTitle": "fixture"}
                elif self.path == "/api/v1/health": payload = []
                elif self.path == "/api/v1/indexer": payload = [{"enable": True}]
                elif self.path == "/api/v3/downloadclient": payload = [{"enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}, {"name": "tvCategory", "value": "media-interlock-sonarr"}]}]
                elif self.path == "/api/v2/app/preferences": payload = {"start_paused_enabled": True}
                elif self.path == "/api/v2/app/webapiVersion":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"2.11.3"); return
                elif self.path == "/api/v2/app/version":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"v5.2.3"); return
                else: self.send_error(404); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(payload).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); thread = threading.Thread(target=server.serve_forever); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join); self.addCleanup(server.shutdown)
        host, port = server.server_address; base = f"http://{host}:{port}"; key = SecretReference("env", "FIXTURE")
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            self.assertTrue(RadarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-radarr"))
            self.assertTrue(SonarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-sonarr"))
            self.assertTrue(QbittorrentAdapter(base, key, key, staging_root=staging, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(ProwlarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(BazarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(SeerrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        from media_interlock.adapters.jellyfin import JellyfinAdapter
        self.assertTrue(JellyfinAdapter(base, key, secret_resolver=lambda _: "fixture").ready())

    def test_real_http_adapters_and_unix_daemons_complete_durable_handoff(self) -> None:
        media = b"synthetic-release-media"
        torrent_hash = "a" * 40
        download_id = torrent_hash.upper()
        release = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie", "size": len(media), "downloadUrl": "https://indexer.invalid/release"}
        events: list[str] = []
        grabbed = [False]
        torrent = {"hash": torrent_hash, "category": "media-interlock-radarr", "save_path": "", "size": len(media), "state": "pausedDL", "tags": "", "progress": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass
            def _json(self, value: object, status: int = 200) -> None:
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(value).encode())
            def do_GET(self) -> None:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(self.path); query = parse_qs(parsed.query)
                if parsed.path == "/api/v3/downloadclient": self._json([{"enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2, "type": "select"}, {"name": "movieCategory", "value": "media-interlock-radarr"}, {"name": "tvCategory", "value": "media-interlock-sonarr"}]}])
                elif parsed.path == "/api/v3/release": self._json([release])
                elif parsed.path == "/api/v3/history":
                    if "downloadId" in query: self._json({"records": [{"eventType": "downloadFolderImported", "downloadId": download_id, "movieId": 42, "data": {"importedPath": str(staging / "movie.mkv")}}], "totalRecords": 1})
                    else: self._json({"records": [] if not grabbed[0] else [{"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie", "downloadId": download_id}], "totalRecords": 0 if not grabbed[0] else 1})
                elif parsed.path == "/api/v3/queue": self._json({"records": [{"id": 9, "movieId": 42, "title": "fixture.movie", "downloadId": download_id, "protocol": "torrent", "size": len(media)}], "totalRecords": 1})
                elif parsed.path == "/api/v3/movie/42": self._json({"id": 42, "tmdbId": 42})
                elif parsed.path == "/api/v2/app/webapiVersion": self.send_response(200); self.end_headers(); self.wfile.write(b"2.11.3")
                elif parsed.path == "/api/v2/app/version": self.send_response(200); self.end_headers(); self.wfile.write(b"v5.2.3")
                elif parsed.path == "/api/v2/app/preferences": self._json({"start_paused_enabled": True})
                elif parsed.path == "/api/v2/torrents/info": self._json([torrent])
                elif parsed.path == "/api/v1/health": self._json([])
                elif parsed.path == "/api/v1/indexer": self._json([{"enable": True}])
                elif parsed.path == "/System/Info": self._json({"Version": "10.11.11"})
                elif parsed.path == "/api/system/status": self._json({"data": {"bazarr_version": "1.6.0"}})
                elif parsed.path == "/api/v1/settings/main": self._json({"applicationTitle": "fixture"})
                elif parsed.path == "/Items":
                    internal = "/jellyfin/library/radarr-tmdb-42/payload.mkv"
                    self._json({"Items": [{"Id": "item-42", "Path": internal, "Type": "Movie", "ProviderIds": {"Tmdb": "42"}, "MediaSources": [{"Id": "source-42", "Path": internal, "Size": len(media)}]}], "TotalRecordCount": 1})
                elif parsed.path == "/Videos/item-42/stream": self.send_response(200); self.end_headers(); self.wfile.write(media)
                else: self.send_error(404)
            def do_POST(self) -> None:
                if self.path == "/api/v2/auth/login": self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
                elif self.path == "/api/v3/release":
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode()); grabbed[0] = body == release; events.append("arr-post"); self.send_response(200); self.end_headers()
                elif self.path == "/api/v2/torrents/addTags":
                    from urllib.parse import parse_qs
                    torrent["tags"] = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())["tags"][0]; events.append("tag"); self.send_response(200); self.end_headers()
                elif self.path == "/api/v2/torrents/start": torrent["state"] = "downloading"; events.append("resume"); self.send_response(200); self.end_headers()
                elif self.path == "/Library/Media/Updated": events.append("catalog-submit"); self.send_response(204); self.end_headers()
                else: self.send_error(404)

        def exchange(path: Path, envelope: Envelope) -> Envelope:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5); client.connect(str(path)); client.sendall(envelope.encode())
                frame = bytearray()
                while not frame.endswith(b"\n"): frame.extend(client.recv(65536))
            return Envelope.decode(bytes(frame))

        def start_daemon(path: Path, runtime_factory: object) -> tuple[threading.Event, threading.Thread]:
            ready, stop = threading.Event(), threading.Event()
            async def serve() -> None:
                store, daemon, lock = runtime_factory()
                try:
                    if path.exists(): path.unlink()
                    server = await asyncio.start_unix_server(daemon.handle, path=path); ready.set()
                    while not stop.is_set(): await asyncio.sleep(0.001)
                    server.close(); await server.wait_closed()
                finally:
                    store.close()
                    if lock is not None: lock.close()
            thread = threading.Thread(target=lambda: asyncio.run(serve())); thread.start(); self.assertTrue(ready.wait(5)); return stop, thread

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); staging = root / "staging"; canonical = root / "canonical"; runtime = root / "runtime"
            staging.mkdir(); canonical.mkdir(); runtime.mkdir(); (staging / "movie.mkv").write_bytes(media); torrent["save_path"] = str(staging)
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler); http_thread = threading.Thread(target=http.serve_forever); http_thread.start()
            self.addCleanup(http.server_close); self.addCleanup(http_thread.join); self.addCleanup(http.shutdown)
            host, port = http.server_address; base = f"http://{host}:{port}"; config_path = root / "media-interlock.toml"
            config_path.write_text("\n".join(["[shared]", f'runtime_dir = "{runtime}"', "", "[fence]", f'state_dir = "{root / "fence-state"}"', f'socket_path = "{runtime / "fence.sock"}"', f'staging_root = "{staging}"', 'radarr_category = "media-interlock-radarr"', 'sonarr_category = "media-interlock-sonarr"', "capacity_bytes = 10000", "max_inflight = 1", "", "[publisher]", f'state_dir = "{root / "publisher-state"}"', f'socket_path = "{runtime / "publisher.sock"}"', f'staging_root = "{staging}"', f'canonical_root = "{canonical}"', 'jellyfin_library_id = "2f9e0f39-70de-4502-85ce-7ed03cd2f01f"', 'namespace = "library"', 'jellyfin_path_prefix = "/jellyfin/library"', "", "[reconciler]", f'state_dir = "{root / "reconciler-state"}"', f'socket_path = "{runtime / "reconciler.sock"}"', "", "[reconciler.movie]", "minimum_age_days = 0", "terminal_horizon_days = 1", "cooldown_seconds = 0", "max_attempts = 3", "max_searches_per_run = 1", "", "[reconciler.episode]", "minimum_age_days = 0", "terminal_horizon_days = 1", "cooldown_seconds = 0", "max_attempts = 3", "max_searches_per_run = 1", *sum(([f"[adapters.{name}]", f'base_url = "{base}"', 'username = "env:MI_FIXTURE_USER"', 'password = "env:MI_FIXTURE_PASSWORD"', ""] if name == "qbittorrent" else [f"[adapters.{name}]", f'base_url = "{base}"', 'api_key = "env:MI_FIXTURE_KEY"', ""] for name in ("radarr", "sonarr", "qbittorrent", "jellyfin", "bazarr", "seerr", "prowlarr")), [])]) + "\n", encoding="utf-8")
            old_environment = {name: os.environ.get(name) for name in ("MI_FIXTURE_KEY", "MI_FIXTURE_USER", "MI_FIXTURE_PASSWORD")}; os.environ.update({"MI_FIXTURE_KEY": "fixture", "MI_FIXTURE_USER": "fixture", "MI_FIXTURE_PASSWORD": "fixture"})
            publisher_stop = fence_stop = None
            try:
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                fence_stop, fence_thread = start_daemon(runtime / "fence.sock", lambda: (*fence_cli._runtime(load_config(config_path))[:2], None))
                self.assertEqual(0, reconciler_cli.main(["--config", str(config_path), "--source", "radarr", "--entity", "42", "--checkpoint", "fixture", "--json"]))
                reconciler_store = ReconcilerStore.open(root / "reconciler-state")
                reconciler_state = reconciler_store.load()
                self.assertTrue(reconciler_state.observed(reconciler_state.intents()[0].operation_id), reconciler_state.records())
                operation_id = reconciler_state.intents()[0].operation_id
                reconciler_store.close()
                # A daemon restart after the grab binding must resume from its
                # durable state; it must not repeat the Arr POST or tag effect.
                fence_stop.set(); fence_thread.join(5)
                fence_stop, fence_thread = start_daemon(runtime / "fence.sock", lambda: (*fence_cli._runtime(load_config(config_path))[:2], None))
                torrent.update({"state": "uploading", "progress": 1})
                terminal = exchange(runtime / "fence.sock", Envelope(CONTRACT_VERSION, "observe", operation_id, {})); self.assertEqual("terminal_acquisition", terminal.kind)
                # The terminal is durable at Fence before the separate
                # Publisher owner comes back, so a lost delivery has no
                # filesystem rollback path.
                publisher_stop.set(); publisher_thread.join(5)
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                receipt = exchange(runtime / "publisher.sock", terminal); self.assertEqual("custody_receipt", receipt.kind)
                self.assertEqual("ok", exchange(runtime / "fence.sock", receipt).body["code"])
                publisher_stop.set(); publisher_thread.join(5)
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                self.assertEqual(receipt, exchange(runtime / "publisher.sock", terminal))
            finally:
                if fence_stop is not None: fence_stop.set(); fence_thread.join(5)
                if publisher_stop is not None: publisher_stop.set(); publisher_thread.join(5)
                for name, value in old_environment.items():
                    if value is None: os.environ.pop(name, None)
                    else: os.environ[name] = value
            self.assertEqual(PublicationState.DELIVERED, PublisherStore.open(root / "publisher-state").load().publication(operation_id).state)
            self.assertEqual("released", FenceStore.open(root / "fence-state").load(__import__("media_interlock.fence.model", fromlist=["FencePolicy"]).FencePolicy(10000, 1)).reservation(operation_id).state)
            self.assertEqual(media, (canonical / "library" / "radarr-tmdb-42" / "payload.mkv").read_bytes())
        self.assertEqual(["arr-post", "tag", "resume", "catalog-submit"], events)
