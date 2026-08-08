from __future__ import annotations

import unittest
import asyncio
import hashlib
import tempfile
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.fence.model import AcquisitionIntent, FencePolicy, FenceState
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.service import FenceService
from media_interlock.contracts import Envelope, StatusCode, acquisition_intent, acquisition_pre_admission, custody_receipt


class FenceObservabilityTests(unittest.TestCase):
    def test_status_and_metrics_expose_only_bounded_aggregate_state(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=2))
        operation_id = "12345678-1234-4678-9234-567812345678"
        state.admit(AcquisitionIntent(operation_id, "radarr", "grab-42", "movie-42", 400, "fixture"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.mark_qbittorrent_stopped(operation_id, "a" * 40)
        state.request_resume(operation_id)
        state.mark_qbittorrent_active(operation_id)
        observed = FenceObservability(state)

        self.assertEqual({"version": "v1", "status": "ready", "reserved_bytes": 400, "inflight": 1}, observed.status(qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True))
        self.assertEqual("media_interlock_fence_reserved_bytes 400\nmedia_interlock_fence_inflight 1\n", observed.metrics())
        self.assertEqual("inhibited", observed.status(qbittorrent_ready=False, prowlarr_ready=True, publisher_ready=True)["status"])


class FenceUnixDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_admission_over_unix_persists_only_a_reservation_before_arr_grab(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def ready(self) -> bool:
                return True

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)
        daemon = FenceDaemon(service, FenceObservability(state), readiness=lambda: (True, True, True))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fence.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(acquisition_pre_admission(operation_id="12345678-1234-4678-9234-567812345678", source="radarr", media_id="42", selector_fingerprint="a" * 64, expected_bytes=400, watermark="7").encode())
                await writer.drain()
                response = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual(StatusCode.OK.value, response.body["code"])
                self.assertEqual("pre_admitted", state.reservation(response.operation_id).state)
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

    async def test_acquisition_intent_drives_durable_fenced_admission(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        class Qbittorrent:
            def ready(self) -> bool:
                return True

            def add_stopped(self, _: str, __: str) -> tuple[str, int]:
                return "a" * 40, 400

            def resume(self, _: str) -> bool:
                return True

            def observe_active(self, _: str, __: str) -> bool:
                return True

        locator = "magnet:?xt=urn:btih:fixture"
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        service = FenceService(state, Store(), Qbittorrent(), prowlarr=None)
        daemon = FenceDaemon(service, FenceObservability(state), readiness=lambda: (True, True, True))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fence.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(acquisition_intent(operation_id="12345678-1234-4678-9234-567812345678", source="radarr", source_locator=locator, upstream_id="grab-42", media_id="movie-42", bytes_reserved=400, source_fingerprint=hashlib.sha256(locator.encode()).hexdigest()).encode())
                await writer.drain()
                response = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual(StatusCode.OK.value, response.body["code"])
                self.assertEqual("qbittorrent_active", state.reservation(response.operation_id).state)
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()

    async def test_custody_receipt_is_handled_over_versioned_unix_socket(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None:
                pass

        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=1))
        intent = AcquisitionIntent("12345678-1234-4678-9234-567812345678", "radarr", "grab-42", "movie-42", 400, "fixture")
        state.admit(intent, qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        state.mark_qbittorrent_stopped(intent.operation_id, "a" * 40)
        state.request_resume(intent.operation_id)
        state.mark_qbittorrent_active(intent.operation_id)
        state.complete(intent.operation_id)
        service = FenceService(state, Store(), object(), prowlarr=None)
        daemon = FenceDaemon(service, FenceObservability(state), readiness=lambda: (True, True, True))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fence.sock"
            server = await asyncio.start_unix_server(daemon.handle, path=path)
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(custody_receipt(intent.operation_id, state.reservation(intent.operation_id).reservation_id, "publisher-r-1").encode())
                await writer.drain()
                response = Envelope.decode(await reader.readuntil(b"\n"))
                self.assertEqual(StatusCode.OK.value, response.body["code"])
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()
