from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import (
    Envelope,
    StatusCode,
    publisher_assisted_complete,
    publisher_assisted_intent,
    publisher_operation_query,
    terminal_acquisition,
)
from media_interlock.publisher.daemon import PublisherDaemon
from media_interlock.publisher.model import PublicationState, PublisherState
from media_interlock.publisher.observability import PublisherObservability
from media_interlock.publisher.service import PublisherService


class PublisherDaemonTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manifest() -> dict[str, object]:
        return {
            "source": "radarr", "upstream_id": "import-42", "media_id": "42", "asset_slot": "radarr:tmdb-42",
            "item_type": "Movie", "provider_ids": {"Tmdb": "42"}, "candidate_relative_path": "movie.mkv",
            "bundle_members": [{"path": "movie.mkv", "bytes": 5, "allocated": 4096, "device": 1, "inode": 2, "modified_ns": 3, "sha256": "a" * 64}],
            "inspection": {"audio_languages": [], "subtitle_languages": [], "container_evidence": ["container:mkv"]},
            "expected_catalog_path": "/jellyfin/library/radarr-tmdb-42/payload.mkv",
        }

    @staticmethod
    async def exchange(path: Path, request: Envelope) -> Envelope:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(request.encode())
        await writer.drain()
        response = Envelope.decode(await reader.readuntil(b"\n"))
        writer.close()
        await writer.wait_closed()
        return response

    async def test_terminal_acquisition_returns_durable_custody_receipt(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        state = PublisherState()
        daemon = PublisherDaemon(PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: True)
        terminal = terminal_acquisition(operation_id="12345678-1234-4678-9234-567812345678", fence_reservation_id="fence:12345678-1234-4678-9234-567812345678", source="radarr", upstream_id="grab-42", media_id="movie-42", bytes_reserved=400, download_id="a" * 40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(terminal.encode())
                await writer.drain()
                receipt = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual("custody_receipt", receipt.kind)
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

    async def test_status_and_metrics_are_bounded(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        state = PublisherState()
        daemon = PublisherDaemon(PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: False)
        status = daemon._dispatch(Envelope("v1", "status", "12345678-1234-4678-9234-567812345678", {"code": "ok", "message": "readiness"}))
        metrics = daemon._dispatch(Envelope("v1", "metrics", "12345678-1234-4678-9234-567812345678", {}))
        self.assertEqual(StatusCode.INHIBITED.value, status.body["code"])
        self.assertEqual("media_interlock_publisher_publications 0\n", metrics.body["text"])

    async def test_unready_daemon_retains_fence_custody(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        state = PublisherState()
        daemon = PublisherDaemon(PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: False)
        terminal = terminal_acquisition(operation_id="12345678-1234-4678-9234-567812345678", fence_reservation_id="fence:12345678-1234-4678-9234-567812345678", source="radarr", upstream_id="grab-42", media_id="movie-42", bytes_reserved=400, download_id="a" * 40)

        response = daemon._dispatch(terminal)

        self.assertEqual(StatusCode.INHIBITED.value, response.body["code"])
        with self.assertRaises(KeyError):
            state.publication(terminal.operation_id)

    async def test_terminal_receipt_starts_durable_publisher_processing(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        seen: list[str] = []
        state = PublisherState()
        daemon = PublisherDaemon(PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: True, process=seen.append)
        terminal = terminal_acquisition(operation_id="12345678-1234-4678-9234-567812345678", fence_reservation_id="fence:12345678-1234-4678-9234-567812345678", source="radarr", upstream_id="grab-42", media_id="movie-42", bytes_reserved=400, download_id="a" * 40)

        receipt = daemon._dispatch(terminal)

        self.assertEqual("custody_receipt", receipt.kind)
        self.assertEqual([terminal.operation_id], seen)

    async def test_pending_processor_retains_fence_custody_until_adoption_is_committed(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        state = PublisherState()
        daemon = PublisherDaemon(PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: True, process=lambda _: False)
        terminal = terminal_acquisition(operation_id="12345678-1234-4678-9234-567812345678", fence_reservation_id="fence:12345678-1234-4678-9234-567812345678", source="radarr", upstream_id="grab-42", media_id="movie-42", bytes_reserved=400, download_id="a" * 40)

        response = daemon._dispatch(terminal)

        self.assertEqual("status", response.kind)
        self.assertEqual(StatusCode.INHIBITED.value, response.body["code"])
        self.assertEqual(PublicationState.CUSTODY_RESERVED, state.publication(terminal.operation_id).state)

    async def test_owner_intake_is_dispatched_without_a_fence_receipt(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        seen: list[str] = []
        state = PublisherState()
        service = PublisherService(state, Store())
        def intake(envelope: Envelope) -> bool:
            seen.append(envelope.kind)
            service.record_assisted_intent(
                operation_id=envelope.operation_id, source=str(envelope.body["source"]), upstream_id=str(envelope.body["upstream_id"]),
                media_id=str(envelope.body["media_id"]), expected_bytes=int(envelope.body["expected_bytes"]), manifest_digest=str(envelope.body["manifest_sha256"]),
            )
            return True
        daemon = PublisherDaemon(
            service, PublisherObservability(state), readiness=lambda: True, intake=intake,
        )
        request = publisher_assisted_intent(
            operation_id="12345678-1234-4678-9234-567812345678", source="radarr", upstream_id="import-42", media_id="42", expected_bytes=5, manifest_sha256="a" * 64,
        )

        response = daemon._dispatch(request)

        self.assertEqual("publisher_operation_status", response.kind)
        self.assertEqual("accepted", response.body["state"])
        self.assertEqual(["publisher_assisted_intent"], seen)

    async def test_assisted_complete_and_query_are_pending_until_retry_persists_terminal_receipt(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        operation_id = "12345678-1234-4678-9234-567812345678"
        manifest = self.manifest()
        complete = publisher_assisted_complete(operation_id=operation_id, manifest=manifest)
        state = PublisherState()
        state.record_assisted_intent(
            operation_id=operation_id, source="radarr", upstream_id="import-42", media_id="42", expected_bytes=5,
            manifest_digest=str(complete.body["manifest_sha256"]),
        )
        process_calls: list[str] = []

        def intake(_: Envelope) -> bool:
            if state.publication(operation_id).state is PublicationState.CUSTODY_RESERVED:
                state.mark_candidate_verified(operation_id, "movie.mkv", 5, "a" * 64)
                state.bind_asset_identity(operation_id, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
            return True

        def retry() -> None:
            state.record_generation_intent(operation_id, None)
            state.mark_generation_committed(operation_id)
            state.bind_catalog_expectation(operation_id, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
            state.mark_notification_attempted(operation_id)
            state.mark_catalog_observed(operation_id, "item-1", "source-1")
            state.mark_catalog_delivered(operation_id)

        daemon = PublisherDaemon(
            PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: True,
            intake=intake, process=lambda identifier: process_calls.append(identifier) is not None, retry=retry,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                self.assertEqual("accepted", (await self.exchange(path, publisher_operation_query(operation_id))).body["state"])
                pending = await self.exchange(path, complete)
                self.assertEqual({"state": "pending"}, dict(pending.body))
                self.assertEqual([operation_id], process_calls)

                # Simulate a response lost after the durable request was handled.
                _, writer = await asyncio.open_unix_connection(path)
                writer.write(complete.encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                self.assertEqual("pending", (await self.exchange(path, publisher_operation_query(operation_id))).body["state"])

                daemon.retry_once()
                receipt = await self.exchange(path, publisher_operation_query(operation_id))
                self.assertEqual("publisher_operation_receipt", receipt.kind)
                self.assertEqual("visible-confirmed", receipt.body["state"])
                self.assertEqual("a" * 64, receipt.body["generation_sha256"])
                self.assertEqual(receipt, await self.exchange(path, publisher_operation_query(operation_id)))
            finally:
                server.close()
                await server.wait_closed()

    async def test_query_distinguishes_unavailable_conflict_and_catalog_confirmed_without_receipt(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        operation_id = "12345678-1234-4678-9234-567812345678"
        state = PublisherState()
        state.adopt_terminal(terminal_acquisition(
            operation_id=operation_id, fence_reservation_id=f"fence:{operation_id}", source="radarr", upstream_id="import-42",
            media_id="42", bytes_reserved=5, download_id="a" * 40,
        ))
        state.mark_candidate_verified(operation_id, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(operation_id, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(operation_id, None)
        state.mark_generation_committed(operation_id)
        state.bind_catalog_expectation(operation_id, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        state.mark_notification_attempted(operation_id)
        state.mark_catalog_observed(operation_id, "item-1", "source-1")
        daemon = PublisherDaemon(
            PublisherService(state, Store()), PublisherObservability(state), readiness=lambda: True, intake=lambda _: False,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                confirmed = await self.exchange(path, publisher_operation_query(operation_id))
                self.assertEqual({"state": "catalog-confirmed"}, dict(confirmed.body))
                metrics = await self.exchange(path, Envelope("v1", "metrics", operation_id, {}))
                for private_value in (operation_id, "a" * 64, "item-1", "source-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv"):
                    self.assertNotIn(private_value, metrics.body["text"])
                absent = await self.exchange(path, publisher_operation_query("87654321-4321-4678-9234-567812345678"))
                self.assertEqual({"state": "unavailable"}, dict(absent.body))
                conflicting = publisher_assisted_complete(operation_id=operation_id, manifest=self.manifest())
                _, writer = await asyncio.open_unix_connection(path)
                writer.write(conflicting.encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                self.assertEqual({"state": "conflict"}, dict((await self.exchange(path, publisher_operation_query(operation_id))).body))
                self.assertEqual({"state": "conflict"}, dict((await self.exchange(path, conflicting)).body))
            finally:
                server.close()
                await server.wait_closed()

    async def test_daemon_exposes_a_retry_tick_for_catalog_pending_work(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        retries: list[str] = []
        daemon = PublisherDaemon(
            PublisherService(PublisherState(), Store()), PublisherObservability(PublisherState()),
            readiness=lambda: True, retry=lambda: retries.append("retry"),
        )

        daemon.retry_once()

        self.assertEqual(["retry"], retries)
