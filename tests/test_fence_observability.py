from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import Envelope, StatusCode, acquisition_pre_admission
from media_interlock.fence.daemon import FenceDaemon
from media_interlock.fence.model import FencePolicy, FenceState, PreAdmissionIntent
from media_interlock.fence.observability import FenceObservability
from media_interlock.fence.service import FenceService


class FenceObservabilityTests(unittest.TestCase):
    def test_status_and_metrics_expose_only_bounded_aggregate_state(self) -> None:
        state = FenceState(FencePolicy(capacity_bytes=1_000, max_inflight=2))
        state.pre_admit(PreAdmissionIntent("12345678-1234-4678-9234-567812345678", "radarr", "42", "a" * 64, 400, "7"), qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)
        observed = FenceObservability(state)
        self.assertEqual("ready", observed.status(qbittorrent_ready=True, prowlarr_ready=True, publisher_ready=True)["status"])
        self.assertIn("reserved_bytes 400", observed.metrics())


class FenceUnixDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_admission_over_unix_persists_no_qbittorrent_effect(self) -> None:
        class Store:
            def save(self, _: FenceState) -> None: pass
        class Qbittorrent:
            def ready(self) -> bool: return True
        state = FenceState(FencePolicy(1_000, 1))
        daemon = FenceDaemon(FenceService(state, Store(), Qbittorrent(), None), FenceObservability(state), readiness=lambda: (True, True, True))
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
                writer.close(); await writer.wait_closed()
            finally:
                server.close(); await server.wait_closed()
