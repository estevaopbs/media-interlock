from __future__ import annotations

import json
import asyncio
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.adapters.jellyfin import JellyfinAdapter
from media_interlock.adapters.radarr import RadarrAdapter
from media_interlock.config import SecretReference
from media_interlock.contracts import publisher_operation_query, terminal_acquisition
from media_interlock.publisher.generation import AssetGenerationPublisher
from media_interlock.publisher.model import PublicationState
from media_interlock.publisher.daemon import PublisherDaemon
from media_interlock.publisher.observability import PublisherObservability
from media_interlock.publisher.service import AssetPublisherWorkProcessor, PathTranslation, PublisherService
from media_interlock.publisher.store import PublisherStore
from media_interlock.publisher.filesystem import CandidateVerifier


class PublisherVerticalIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        self.catalog_path = "/jellyfin/library/radarr-tmdb-42/payload.mkv"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append(("GET", self.path, self.headers.get("X-Api-Key") or self.headers.get("X-Emby-Token")))
                if self.path.startswith("/api/v3/history?"):
                    payload = {"records": [{"eventType": "downloadFolderImported", "downloadId": "grab-42", "movieId": 42, "data": {"importedPath": str(outer.staging / "movie.mkv")}}]}
                elif self.path == "/api/v3/movie/42":
                    payload = {"id": 42, "tmdbId": 42}
                elif self.path.startswith("/Items?"):
                    payload = {
                        "Items": [{
                            "Id": "jellyfin-item",
                            "Path": outer.catalog_path,
                            "Type": "Movie",
                            "ProviderIds": {"Tmdb": "42"},
                            "MediaSources": [{
                                "Id": "source-id",
                                "Path": outer.catalog_path,
                                "Size": len(b"synthetic-media"),
                            }],
                        }],
                        "TotalRecordCount": 1,
                    }
                elif self.path.startswith("/Videos/jellyfin-item/stream?"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"synthetic-media")
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def do_POST(self) -> None:
                outer.requests.append(("POST", self.path, self.headers.get("X-Emby-Token")))
                if self.path != "/Library/Media/Updated":
                    self.send_error(404)
                    return
                self.send_response(204)
                self.end_headers()

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.staging = Path(self.temporary.name) / "staging"
        self.canonical = Path(self.temporary.name) / "canonical"
        self.staging.mkdir()
        self.canonical.mkdir()
        (self.staging / "movie.mkv").write_bytes(b"synthetic-media")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def test_unix_terminal_to_exact_catalog_observation_is_durable(self) -> None:
        host, port = self.server.server_address
        key = SecretReference("env", "FIXTURE_KEY")
        correlation = RadarrAdapter(f"http://{host}:{port}", key, staging_root=self.staging, secret_resolver=lambda _: "fixture-key")
        catalog = JellyfinAdapter(f"http://{host}:{port}", key, secret_resolver=lambda _: "fixture-key")
        operation_id = str(uuid.uuid4())
        terminal = terminal_acquisition(
            operation_id=operation_id,
            fence_reservation_id=f"fence:{uuid.uuid4()}",
            source="radarr",
            upstream_id="grab-42",
            media_id="42",
            bytes_reserved=400,
            download_id="grab-42",
        )
        store = PublisherStore.open(Path(self.temporary.name) / "publisher-state")
        self.addCleanup(store.close)
        service = PublisherService(store.load(), store)

        generations = AssetGenerationPublisher(self.staging, self.canonical, namespace="library")
        processor = AssetPublisherWorkProcessor(
            service,
            {"radarr": correlation},
            CandidateVerifier(self.staging),
            generations,
            catalog,
            PathTranslation(self.canonical, "library", "/jellyfin/library"),
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
        )
        daemon = PublisherDaemon(service, PublisherObservability(service._state), readiness=lambda: True, process=processor)
        socket_path = Path(self.temporary.name) / "publisher.sock"

        async def terminal_over_unix_socket() -> None:
            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(terminal.encode())
                await writer.drain()
                receipt = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual(operation_id, receipt.operation_id)
                self.assertEqual("custody_receipt", receipt.kind)
                writer.close()
                await writer.wait_closed()
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(publisher_operation_query(operation_id).encode())
                await writer.drain()
                public_receipt = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual("publisher_operation_receipt", public_receipt.kind)
                self.assertEqual("visible-confirmed", public_receipt.body["state"])
                self.assertEqual("radarr:tmdb-42", public_receipt.body["asset_slot"])
                self.assertEqual("jellyfin-item", public_receipt.body["item_id"])
                self.assertEqual("source-id", public_receipt.body["media_source_id"])
                self.assertEqual("/jellyfin/library/radarr-tmdb-42/payload.mkv", public_receipt.body["expected_catalog_path"])
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

        from media_interlock.contracts import Envelope
        asyncio.run(terminal_over_unix_socket())

        self.assertEqual(PublicationState.DELIVERED, store.load().publication(operation_id).state)
        self.assertEqual(b"synthetic-media", (self.canonical / "library" / "radarr-tmdb-42" / "payload.mkv").read_bytes())
        self.assertEqual(
            ["GET", "GET", "POST", "GET", "GET"],
            [request[0] for request in self.requests],
        )
        self.assertTrue(any(path.startswith("/Items?") and "ParentId=2f9e0f39-70de-4502-85ce-7ed03cd2f01f" in path for _, path, _ in self.requests))

    def test_wrong_catalog_binding_stays_pending_over_unix_until_exact_retry(self) -> None:
        host, port = self.server.server_address
        key = SecretReference("env", "FIXTURE_KEY")
        correlation = RadarrAdapter(f"http://{host}:{port}", key, staging_root=self.staging, secret_resolver=lambda _: "fixture-key")
        catalog = JellyfinAdapter(f"http://{host}:{port}", key, secret_resolver=lambda _: "fixture-key")
        operation_id = str(uuid.uuid4())
        terminal = terminal_acquisition(
            operation_id=operation_id, fence_reservation_id=f"fence:{uuid.uuid4()}", source="radarr",
            upstream_id="grab-42", media_id="42", bytes_reserved=400, download_id="grab-42",
        )
        store = PublisherStore.open(Path(self.temporary.name) / "publisher-wrong-binding")
        self.addCleanup(store.close)
        service = PublisherService(store.load(), store)
        processor = AssetPublisherWorkProcessor(
            service, {"radarr": correlation}, CandidateVerifier(self.staging),
            AssetGenerationPublisher(self.staging, self.canonical, namespace="library"), catalog,
            PathTranslation(self.canonical, "library", "/jellyfin/library"),
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
        )
        daemon = PublisherDaemon(
            service, PublisherObservability(service._state), readiness=lambda: True, process=processor,
            retry=lambda: processor(operation_id),
        )
        socket_path = Path(self.temporary.name) / "publisher-wrong-binding.sock"
        self.catalog_path = "/jellyfin/library/wrong/payload.mkv"

        async def exercise() -> None:
            from media_interlock.contracts import Envelope

            server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(terminal.encode())
                await writer.drain()
                self.assertEqual("custody_receipt", Envelope.decode(await reader.readuntil(b"\n")).kind)
                writer.close()
                await writer.wait_closed()

                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(publisher_operation_query(operation_id).encode())
                await writer.drain()
                pending = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual("pending", pending.body["state"])
                writer.close()
                await writer.wait_closed()

                self.catalog_path = "/jellyfin/library/radarr-tmdb-42/payload.mkv"
                daemon.retry_once()
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(publisher_operation_query(operation_id).encode())
                await writer.drain()
                receipt = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual("publisher_operation_receipt", receipt.kind)
                self.assertEqual("visible-confirmed", receipt.body["state"])
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(exercise())
        self.assertEqual(PublicationState.DELIVERED, store.load().publication(operation_id).state)
