from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import Envelope, StatusCode, terminal_acquisition
from media_interlock.publisher.daemon import PublisherDaemon
from media_interlock.publisher.model import PublisherState
from media_interlock.publisher.observability import PublisherObservability
from media_interlock.publisher.service import PublisherService


class PublisherDaemonTests(unittest.IsolatedAsyncioTestCase):
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
