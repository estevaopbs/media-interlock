from __future__ import annotations

import asyncio
import signal
import unittest
from unittest.mock import patch


class DaemonRuntimeTest(unittest.TestCase):
    def test_sigterm_stops_and_awaits_daemon_workers(self) -> None:
        from media_interlock._infra.daemon_runtime import run_until_shutdown

        async def exercise() -> None:
            callbacks: dict[signal.Signals, object] = {}
            removed: list[signal.Signals] = []
            started = asyncio.Event()
            stopped = asyncio.Event()
            real_loop = asyncio.get_running_loop()

            class SignalLoop:
                def add_signal_handler(self, sig, callback) -> None:
                    callbacks[sig] = callback

                def remove_signal_handler(self, sig) -> bool:
                    removed.append(sig)
                    return True

            async def worker() -> None:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            with patch(
                "media_interlock._infra.daemon_runtime.asyncio.get_running_loop",
                return_value=SignalLoop(),
            ):
                task = real_loop.create_task(run_until_shutdown(worker()))
                await asyncio.wait_for(started.wait(), timeout=1)
                callbacks[signal.SIGTERM]()
                await asyncio.wait_for(task, timeout=1)

            self.assertTrue(stopped.is_set())
            self.assertEqual(
                set(removed),
                {signal.SIGINT, signal.SIGTERM},
            )

        asyncio.run(exercise())

    def test_worker_failure_propagates_and_cancels_peers(self) -> None:
        from media_interlock._infra.daemon_runtime import run_until_shutdown

        async def exercise() -> None:
            stopped = asyncio.Event()

            async def failing() -> None:
                await asyncio.sleep(0)
                raise RuntimeError("worker failed")

            async def peer() -> None:
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                await asyncio.wait_for(
                    run_until_shutdown(failing(), peer()),
                    timeout=1,
                )
            self.assertTrue(stopped.is_set())

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
