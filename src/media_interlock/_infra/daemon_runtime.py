"""Shared bounded shutdown handling for OCI daemon entrypoints."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable


async def run_until_shutdown(*workers: Awaitable[object]) -> None:
    """Run workers until SIGINT/SIGTERM, then cancel and await all of them."""

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    tasks = tuple(asyncio.create_task(worker) for worker in workers)
    try:
        await shutdown.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)
