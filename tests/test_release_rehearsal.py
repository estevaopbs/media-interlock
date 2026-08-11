from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
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
from media_interlock._infra.advisory_lease import AdvisoryLease, LeaseUnavailable
from media_interlock.config import SecretReference
from media_interlock.config import load_config
from media_interlock.contracts import (
    CONTRACT_VERSION,
    Envelope,
    publisher_assisted_complete,
    publisher_assisted_intent,
    publisher_operation_query,
)
from media_interlock.fence import cli as fence_cli
from media_interlock.fence.store import FenceStore
from media_interlock.publisher.generation import AssetGenerationPublisher
from media_interlock.publisher.model import PublicationState
from media_interlock.publisher import cli as publisher_cli
from media_interlock.publisher.filesystem import BundleVerifier
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
                elif self.path == "/api/v3/downloadclient": payload = [{"id": 7, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2}, {"name": "movieCategory", "value": "media-interlock-radarr"}, {"name": "tvCategory", "value": "media-interlock-sonarr"}]}]
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
            self.assertTrue(RadarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-radarr", 7))
            self.assertTrue(SonarrAdapter(base, key, staging_root=staging, secret_resolver=lambda _: "fixture").stopped_qbittorrent_client("media-interlock-sonarr", 7))
            self.assertTrue(QbittorrentAdapter(base, key, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(ProwlarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(BazarrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        self.assertTrue(SeerrAdapter(base, key, secret_resolver=lambda _: "fixture").ready())
        from media_interlock.adapters.jellyfin import JellyfinAdapter
        self.assertTrue(JellyfinAdapter(base, key, secret_resolver=lambda _: "fixture").ready())

    def test_real_http_adapters_and_unix_daemons_complete_durable_handoff(self) -> None:
        media = b"synthetic-release-media"
        episode_media = b"synthetic-release-episode"
        torrent_hash = "a" * 40
        download_id = torrent_hash.upper()
        episode_hash = "c" * 40
        episode_download_id = episode_hash.upper()
        release = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie", "size": len(media), "downloadUrl": "https://indexer.invalid/release"}
        episode_release = {"approved": True, "protocol": "torrent", "guid": "release-84", "title": "fixture.episode", "size": len(episode_media), "downloadUrl": "https://indexer.invalid/episode"}
        events: list[str] = []
        mutation_hashes: list[str] = []
        peer_mutation_hashes: list[str] = []
        grabbed = {"radarr": False, "sonarr": False}
        catalog_visible = [False]
        assisted_visible = [False]
        torrent = {"hash": torrent_hash, "category": "media-interlock-radarr", "save_path": "", "size": len(media), "state": "pausedDL", "tags": "", "progress": 0}
        episode_torrent = {"hash": episode_hash, "category": "media-interlock-sonarr", "save_path": "", "size": len(episode_media), "state": "pausedDL", "tags": "", "progress": 0}
        peer_torrent = {"hash": "d" * 40, "category": "synthetic-peer", "save_path": "/synthetic/peer", "size": 456, "state": "pausedDL", "tags": "peer", "progress": 0}
        foreign_torrent = {"hash": "b" * 40, "category": "synthetic-unrelated", "save_path": "/synthetic/unrelated", "size": 123, "state": "pausedDL", "tags": "foreign", "progress": 0}
        foreign_before = dict(foreign_torrent)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None: pass
            def _json(self, value: object, status: int = 200) -> None:
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(value).encode())
            def do_GET(self) -> None:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(self.path); query = parse_qs(parsed.query)
                if parsed.path == "/api/v3/downloadclient": self._json([
                    {"id": 7, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2, "type": "select"}, {"name": "movieCategory", "value": "media-interlock-radarr"}]},
                    {"id": 8, "enable": True, "protocol": "torrent", "implementation": "QBittorrent", "fields": [{"name": "initialState", "value": 2, "type": "select"}, {"name": "tvCategory", "value": "media-interlock-sonarr"}]},
                ])
                elif parsed.path == "/api/v3/release": self._json([episode_release] if "episodeId" in query else [release])
                elif parsed.path == "/api/v3/history":
                    if "downloadId" in query:
                        requested = query["downloadId"]
                        if requested == [download_id]:
                            records = [{"eventType": "downloadFolderImported", "downloadId": download_id, "movieId": 42, "data": {"importedPath": "/data/library/movie.mkv"}}]
                        elif requested == [episode_download_id]:
                            records = [{"eventType": "downloadFolderImported", "downloadId": episode_download_id, "episodeId": 84, "data": {"importedPath": "/data/library/episode.mkv"}}]
                        elif requested == ["assisted-import-43"]:
                            records = [{"eventType": "downloadFolderImported", "downloadId": "assisted-import-43", "movieId": 43, "data": {"importedPath": "/data/library/assisted/feature.mkv"}}]
                        else:
                            records = []
                    else:
                        records = ([] if not grabbed["radarr"] else [{"id": 8, "eventType": "grabbed", "movieId": 42, "sourceTitle": "fixture.movie", "downloadId": download_id}]) + ([] if not grabbed["sonarr"] else [{"id": 9, "eventType": "grabbed", "episodeId": 84, "sourceTitle": "fixture.episode", "downloadId": episode_download_id}])
                    self._json({"records": records, "totalRecords": len(records)})
                elif parsed.path == "/api/v3/queue": self._json({"records": [
                    {"id": 10, "movieId": 42, "title": "fixture.movie", "downloadId": download_id, "protocol": "torrent", "size": len(media)},
                    {"id": 11, "episodeId": 84, "title": "fixture.episode", "downloadId": episode_download_id, "protocol": "torrent", "size": len(episode_media)},
                ], "totalRecords": 2})
                elif parsed.path == "/api/v3/movie/42": self._json({"id": 42, "tmdbId": 42})
                elif parsed.path == "/api/v3/movie/43": self._json({"id": 43, "tmdbId": 43})
                elif parsed.path == "/api/v3/episode/84": self._json({"id": 84, "tvdbId": 84})
                elif parsed.path == "/api/v2/app/webapiVersion": self.send_response(200); self.end_headers(); self.wfile.write(b"2.11.3")
                elif parsed.path == "/api/v2/app/version": self.send_response(200); self.end_headers(); self.wfile.write(b"v5.2.3")
                elif parsed.path == "/api/v2/app/preferences": self._json({"start_paused_enabled": True})
                elif parsed.path == "/api/v2/torrents/info":
                    requested = query.get("hashes", [""])[0]
                    self._json([foreign_torrent] if requested == foreign_torrent["hash"] else [peer_torrent] if requested == peer_torrent["hash"] else [torrent] if requested == torrent_hash else [episode_torrent] if requested == episode_hash else [])
                elif parsed.path == "/api/v1/health": self._json([])
                elif parsed.path == "/api/v1/indexer": self._json([{"enable": True}])
                elif parsed.path == "/System/Info": self._json({"Version": "10.11.11"})
                elif parsed.path == "/api/system/status": self._json({"data": {"bazarr_version": "1.6.0"}})
                elif parsed.path == "/api/v1/settings/main": self._json({"applicationTitle": "fixture"})
                elif parsed.path == "/Items":
                    movie_path = "/jellyfin/library/movie.mkv"
                    episode_path = "/jellyfin/series/episode.mkv"
                    items = [] if not catalog_visible[0] else [
                        {"Id": "item-42", "Path": movie_path, "Type": "Movie", "ProviderIds": {"Tmdb": "42"}, "MediaSources": [{"Id": "source-42", "Path": movie_path, "Size": len(media)}]},
                        {"Id": "item-84", "Path": episode_path, "Type": "Episode", "ProviderIds": {"Tvdb": "84"}, "MediaSources": [{"Id": "source-84", "Path": episode_path, "Size": len(episode_media)}]},
                    ]
                    if assisted_visible[0]:
                        assisted_path = "/jellyfin/library/assisted/feature.mkv"
                        items.append({"Id": "item-43", "Path": assisted_path, "Type": "Movie", "ProviderIds": {"Tmdb": "43"}, "MediaSources": [{"Id": "source-43", "Path": assisted_path, "Size": len(b"assisted-media")}]})
                    self._json({"Items": items, "TotalRecordCount": len(items)})
                elif parsed.path == "/Videos/item-42/stream": self.send_response(200); self.end_headers(); self.wfile.write(media)
                elif parsed.path == "/Videos/item-84/stream": self.send_response(200); self.end_headers(); self.wfile.write(episode_media)
                elif parsed.path == "/Videos/item-43/stream": self.send_response(200); self.end_headers(); self.wfile.write(b"assisted-media")
                else: self.send_error(404)
            def do_POST(self) -> None:
                if self.path == "/api/v2/auth/login": self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
                elif self.path == "/api/v3/release":
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                    if body == release: grabbed["radarr"] = True; events.append("arr-post:radarr")
                    elif body == episode_release: grabbed["sonarr"] = True; events.append("arr-post:sonarr")
                    else: self.send_error(400); return
                    self.send_response(200); self.end_headers()
                elif self.path == "/api/v2/torrents/addTags":
                    from urllib.parse import parse_qs
                    fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                    mutation_hashes.append(fields["hashes"][0])
                    selected = torrent if fields["hashes"] == [torrent_hash] else episode_torrent if fields["hashes"] == [episode_hash] else None
                    if selected is None: self.send_error(400); return
                    selected["tags"] = fields["tags"][0]; events.append("tag:radarr" if selected is torrent else "tag:sonarr"); self.send_response(200); self.end_headers()
                elif self.path == "/api/v2/torrents/start":
                    from urllib.parse import parse_qs
                    fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                    selected = torrent if fields["hashes"] == [torrent_hash] else episode_torrent if fields["hashes"] == [episode_hash] else peer_torrent if fields["hashes"] == [peer_torrent["hash"]] else None
                    if selected is None: self.send_error(400); return
                    selected["state"] = "downloading"
                    if selected is peer_torrent: peer_mutation_hashes.append(peer_torrent["hash"])
                    else: mutation_hashes.append(fields["hashes"][0]); events.append("resume:radarr" if selected is torrent else "resume:sonarr")
                    self.send_response(200); self.end_headers()
                elif self.path == "/Library/Media/Updated":
                    submitted = sum(item.startswith("catalog-submit:") for item in events)
                    events.append(("catalog-submit:radarr", "catalog-submit:sonarr", "catalog-submit:assisted", "catalog-submit:assisted")[submitted])
                    self.send_response(204); self.end_headers()
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
                runtime = runtime_factory()
                store, daemon = runtime[:2]
                lock = runtime[-1]
                try:
                    # Mirror the production entrypoints: durable Fence state
                    # is reconciled before serving and Publisher retries only
                    # after its runtime has recovered pending publication.
                    recover = getattr(daemon, "recover", None)
                    if recover is not None: recover()
                    retry_once = getattr(daemon, "retry_once", None)
                    if retry_once is not None: retry_once()
                    if path.exists(): path.unlink()
                    server = await asyncio.start_unix_server(daemon.handle, path=path); ready.set()
                    while not stop.is_set(): await asyncio.sleep(0.001)
                    server.close(); await server.wait_closed()
                finally:
                    store.close()
                    if isinstance(lock, tuple):
                        for held_lock in lock:
                            held_lock.close()
                    elif lock is not None:
                        lock.close()
            thread = threading.Thread(target=lambda: asyncio.run(serve())); thread.start(); self.assertTrue(ready.wait(5)); return stop, thread

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); staging = root / "staging" / "movies"; canonical = root / "canonical"; runtime = root / "runtime"
            sonarr_staging = root / "staging" / "shows"; sonarr_canonical = root / "sonarr-canonical"
            staging.mkdir(parents=True); canonical.mkdir(); runtime.mkdir(); sonarr_staging.mkdir(parents=True); sonarr_canonical.mkdir(); lock_path = root / "qbittorrent-mutation.lock"; lock_path.touch(mode=0o600); (staging / "movie.mkv").write_bytes(media); (sonarr_staging / "episode.mkv").write_bytes(episode_media); (staging / "assisted").mkdir(); (staging / "assisted" / "feature.mkv").write_bytes(b"assisted-media"); (staging / "assisted" / "feature.en.srt").write_bytes(b"subtitle"); torrent["save_path"] = str(root / "downloads-movies"); episode_torrent["save_path"] = str(root / "downloads-episodes")
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler); http_thread = threading.Thread(target=http.serve_forever); http_thread.start()
            self.addCleanup(http.server_close); self.addCleanup(http_thread.join); self.addCleanup(http.shutdown)
            host, port = http.server_address; base = f"http://{host}:{port}"; config_path = root / "media-interlock.toml"
            peer = subprocess.Popen(
                [sys.executable, "-c", "import fcntl, os, sys, time, urllib.parse, urllib.request; fd=os.open(sys.argv[1], os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX); request=urllib.request.Request(sys.argv[2], data=urllib.parse.urlencode({'hashes': sys.argv[3]}).encode(), method='POST'); urllib.request.urlopen(request).read(); print('held', flush=True); time.sleep(60)", str(lock_path), base + "/api/v2/torrents/start", peer_torrent["hash"]],
                stdout=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: peer.poll() is None and peer.kill())
            assert peer.stdout is not None
            self.addCleanup(peer.stdout.close)
            self.assertEqual("held\n", peer.stdout.readline())
            peer_lease = AdvisoryLease.open(lock_path, timeout_ms=5)
            with self.assertRaises(LeaseUnavailable):
                peer_lease.acquire()
            peer.kill()
            peer.wait(timeout=5)
            with peer_lease.acquire():
                pass
            peer_lease.close()
            source_rows = lambda name, kind, client_id, category, save, stage, canon, namespace, library: [f"[sources.{name}]", f'kind = "{kind}"', f"download_client_id = {client_id}", f'category = "{category}"', f'qbittorrent_save_path = "{save}"', 'arr_import_path_prefix = "/data/library"', f'staging_root = "{stage}"', f'canonical_root = "{canon}"', 'download_pool = "video"', 'staging_pool = "video"', 'canonical_pool = "video"', f'namespace = "{namespace}"', f'jellyfin_library_id = "{library}"', f'jellyfin_path_prefix = "/jellyfin/{namespace}"', "bundle_settle_seconds = 0", ""]
            rows = ["[shared]", f'runtime_dir = "{runtime}"', "", "[fence]", f'state_dir = "{root / "fence-state"}"', f'socket_path = "{runtime / "fence.sock"}"', "capacity_bytes = 10000", "max_inflight = 1", f'mutation_lock_path = "{root / "qbittorrent-mutation.lock"}"', 'mutation_lock_version = "shared-qbittorrent-mutation/v1"', "mutation_lock_timeout_ms = 10", "", "[publisher]", f'state_dir = "{root / "publisher-state"}"', f'socket_path = "{runtime / "publisher.sock"}"', "", "[reconciler]", f'state_dir = "{root / "reconciler-state"}"', f'socket_path = "{runtime / "reconciler.sock"}"', "", "[reconciler.movie]", "minimum_age_days = 0", "terminal_horizon_days = 1", "cooldown_seconds = 0", "max_attempts = 3", "max_searches_per_run = 1", "", "[reconciler.episode]", "minimum_age_days = 0", "terminal_horizon_days = 1", "cooldown_seconds = 0", "max_attempts = 3", "max_searches_per_run = 1", "", "[capacity_pools.video]", f'probe_path = "{root}"', "minimum_free_bytes = 0", "safety_margin_bytes = 0", ""]
            rows.extend(source_rows("radarr", "movie", 7, "media-interlock-radarr", root / "downloads-movies", staging, canonical, "library", "2f9e0f39-70de-4502-85ce-7ed03cd2f01f"))
            rows.extend(source_rows("sonarr", "episode", 8, "media-interlock-sonarr", root / "downloads-episodes", sonarr_staging, sonarr_canonical, "series", "6d3e0f39-70de-4502-85ce-7ed03cd2f01f"))
            rows.extend(sum(([f"[adapters.{name}]", f'base_url = "{base}"', 'username = "env:MI_FIXTURE_USER"', 'password = "env:MI_FIXTURE_PASSWORD"', ""] if name == "qbittorrent" else [f"[adapters.{name}]", f'base_url = "{base}"', 'api_key = "env:MI_FIXTURE_KEY"', ""] for name in ("radarr", "sonarr", "qbittorrent", "jellyfin", "bazarr", "seerr", "prowlarr")), []))
            config_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            old_environment = {name: os.environ.get(name) for name in ("MI_FIXTURE_KEY", "MI_FIXTURE_USER", "MI_FIXTURE_PASSWORD")}; os.environ.update({"MI_FIXTURE_KEY": "fixture", "MI_FIXTURE_USER": "fixture", "MI_FIXTURE_PASSWORD": "fixture"})
            publisher_stop = fence_stop = None
            try:
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                fence_stop, fence_thread = start_daemon(runtime / "fence.sock", lambda: fence_cli._runtime(load_config(config_path)))
                self.assertEqual(0, reconciler_cli.main(["--config", str(config_path), "--source", "radarr", "--entity", "42", "--checkpoint", "fixture", "--json"]))
                reconciler_store = ReconcilerStore.open(root / "reconciler-state")
                reconciler_state = reconciler_store.load()
                self.assertTrue(reconciler_state.observed(reconciler_state.intents()[0].operation_id), reconciler_state.records())
                operation_id = reconciler_state.intents()[0].operation_id
                reconciler_store.close()
                # A daemon restart after the grab binding must resume from its
                # durable state; it must not repeat the Arr POST or tag effect.
                fence_stop.set(); fence_thread.join(5)
                fence_stop, fence_thread = start_daemon(runtime / "fence.sock", lambda: fence_cli._runtime(load_config(config_path)))
                torrent.update({"state": "uploading", "progress": 1})
                terminal = exchange(runtime / "fence.sock", Envelope(CONTRACT_VERSION, "observe", operation_id, {})); self.assertEqual("terminal_acquisition", terminal.kind)
                # The terminal is durable at Fence before the separate
                # Publisher owner comes back, so a lost delivery has no
                # filesystem rollback path.
                publisher_stop.set(); publisher_thread.join(5)
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                receipt = exchange(runtime / "publisher.sock", terminal); self.assertEqual("custody_receipt", receipt.kind)
                self.assertEqual("ok", exchange(runtime / "fence.sock", receipt).body["code"])
                pending = exchange(runtime / "publisher.sock", publisher_operation_query(operation_id))
                self.assertEqual("pending", pending.body["state"])
                # A 204 left this generation CATALOG_PENDING.  Restart before
                # it becomes observable: recovery must observe/adopt the
                # durable candidate instead of publishing or notifying again.
                publisher_stop.set(); publisher_thread.join(5)
                catalog_visible[0] = True
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                publisher_stop.set(); publisher_thread.join(5)
                recovered_store = PublisherStore.open(root / "publisher-state")
                self.assertEqual(PublicationState.DELIVERED, recovered_store.load().publication(operation_id).state)
                recovered_store.close()
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                self.assertEqual(receipt, exchange(runtime / "publisher.sock", terminal))
                public_receipt = exchange(runtime / "publisher.sock", publisher_operation_query(operation_id))
                self.assertEqual("publisher_operation_receipt", public_receipt.kind)
                self.assertEqual("visible-confirmed", public_receipt.body["state"])
                self.assertEqual(operation_id, public_receipt.body["generation_id"])
                self.assertEqual("radarr:tmdb-42", public_receipt.body["asset_slot"])
                self.assertEqual("/jellyfin/library/movie.mkv", public_receipt.body["expected_catalog_path"])
                self.assertEqual(public_receipt, exchange(runtime / "publisher.sock", publisher_operation_query(operation_id)))
                self.assertEqual(0, reconciler_cli.main(["--config", str(config_path), "--source", "sonarr", "--entity", "84", "--checkpoint", "fixture", "--json"]))
                reconciler_store = ReconcilerStore.open(root / "reconciler-state")
                sonarr_operation_id = next(intent.operation_id for intent in reconciler_store.load().intents() if intent.source == "sonarr")
                reconciler_store.close()
                episode_torrent.update({"state": "uploading", "progress": 1})
                sonarr_terminal = exchange(runtime / "fence.sock", Envelope(CONTRACT_VERSION, "observe", sonarr_operation_id, {})); self.assertEqual("terminal_acquisition", sonarr_terminal.kind)
                sonarr_receipt = exchange(runtime / "publisher.sock", sonarr_terminal); self.assertEqual("custody_receipt", sonarr_receipt.kind)
                self.assertEqual("ok", exchange(runtime / "fence.sock", sonarr_receipt).body["code"])
                sonarr_public_receipt = exchange(runtime / "publisher.sock", publisher_operation_query(sonarr_operation_id))
                self.assertEqual("publisher_operation_receipt", sonarr_public_receipt.kind)
                self.assertEqual("sonarr:tvdb-84", sonarr_public_receipt.body["asset_slot"])

                assisted_operation_id = str(uuid.uuid4())
                assisted_bundle = BundleVerifier(staging, settle_seconds=0).verify("assisted/feature.mkv")
                assisted_manifest = {
                    "source": "radarr", "upstream_id": "assisted-import-43", "media_id": "43",
                    "asset_slot": "radarr:tmdb-43", "item_type": "Movie", "provider_ids": {"Tmdb": "43"},
                    "candidate_relative_path": "assisted/feature.mkv",
                    "bundle_members": [{
                        "path": member.relative_path, "bytes": member.bytes_verified,
                        "allocated": member.allocated_bytes, "device": member.device,
                        "inode": member.inode, "modified_ns": member.modified_ns,
                        "sha256": member.sha256,
                    } for member in assisted_bundle.members],
                    "inspection": {
                        "audio_languages": list(assisted_bundle.inspection.audio_languages),
                        "subtitle_languages": list(assisted_bundle.inspection.subtitle_languages),
                        "container_evidence": list(assisted_bundle.inspection.container_evidence),
                    },
                    "expected_catalog_path": "/jellyfin/library/assisted/feature.mkv",
                }
                assisted_complete = publisher_assisted_complete(operation_id=assisted_operation_id, manifest=assisted_manifest)
                assisted_intent = publisher_assisted_intent(
                    operation_id=assisted_operation_id, source="radarr", upstream_id="assisted-import-43",
                    media_id="43", expected_bytes=assisted_bundle.bytes_verified,
                    manifest_sha256=str(assisted_complete.body["manifest_sha256"]),
                )
                accepted = exchange(runtime / "publisher.sock", assisted_intent)
                self.assertEqual("accepted", accepted.body["state"])

                # The completion response is deliberately lost after the
                # durable socket request. Repeating the same envelope must be
                # idempotent and remain pending while item 43 is not visible.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as lost:
                    lost.connect(str(runtime / "publisher.sock")); lost.sendall(assisted_complete.encode())
                pending = exchange(runtime / "publisher.sock", assisted_complete)
                self.assertEqual("pending", pending.body["state"])
                self.assertEqual("pending", exchange(runtime / "publisher.sock", publisher_operation_query(assisted_operation_id)).body["state"])

                publisher_stop.set(); publisher_thread.join(5)
                assisted_visible[0] = True
                publisher_stop, publisher_thread = start_daemon(runtime / "publisher.sock", lambda: publisher_cli._runtime(load_config(config_path)))
                assisted_receipt = exchange(runtime / "publisher.sock", publisher_operation_query(assisted_operation_id))
                self.assertEqual("publisher_operation_receipt", assisted_receipt.kind)
                self.assertEqual("visible-confirmed", assisted_receipt.body["state"])
                self.assertEqual("radarr:tmdb-43", assisted_receipt.body["asset_slot"])
                self.assertEqual(assisted_receipt, exchange(runtime / "publisher.sock", publisher_operation_query(assisted_operation_id)))
            finally:
                if fence_stop is not None: fence_stop.set(); fence_thread.join(5)
                if publisher_stop is not None: publisher_stop.set(); publisher_thread.join(5)
                for name, value in old_environment.items():
                    if value is None: os.environ.pop(name, None)
                    else: os.environ[name] = value
            publisher_store = PublisherStore.open(root / "publisher-state")
            publisher_state = publisher_store.load()
            publisher_store.close()
            fence_store = FenceStore.open(root / "fence-state")
            fence_state = fence_store.load(__import__("media_interlock.fence.model", fromlist=["FencePolicy"]).FencePolicy(10000, 1))
            fence_store.close()
            self.assertEqual(PublicationState.DELIVERED, publisher_state.publication(operation_id).state)
            self.assertEqual("released", fence_state.reservation(operation_id).state)
            self.assertEqual(media, (canonical / "library" / "movie.mkv").read_bytes())
            self.assertEqual(PublicationState.DELIVERED, publisher_state.publication(sonarr_operation_id).state)
            self.assertEqual("released", fence_state.reservation(sonarr_operation_id).state)
            self.assertEqual(episode_media, (sonarr_canonical / "series" / "episode.mkv").read_bytes())
            self.assertEqual(PublicationState.DELIVERED, publisher_state.publication(assisted_operation_id).state)
            self.assertEqual(b"assisted-media", (canonical / "library" / "assisted" / "feature.mkv").read_bytes())
            self.assertEqual(b"subtitle", (canonical / "library" / "assisted" / "feature.en.srt").read_bytes())
            self.assertEqual(foreign_before, foreign_torrent)
        self.assertEqual(
            ["arr-post:radarr", "tag:radarr", "resume:radarr", "catalog-submit:radarr",
             "arr-post:sonarr", "tag:sonarr", "resume:sonarr", "catalog-submit:sonarr",
             "catalog-submit:assisted", "catalog-submit:assisted"],
            events,
        )
        self.assertEqual([torrent_hash, torrent_hash, "c" * 40, "c" * 40], mutation_hashes)
        self.assertEqual(["d" * 40], peer_mutation_hashes)
        self.assertEqual("downloading", peer_torrent["state"])
