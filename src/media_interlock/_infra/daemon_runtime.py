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
    shutdown_task = asyncio.create_task(shutdown.wait())
    try:
        done, _ = await asyncio.wait(
            (*tasks, shutdown_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task not in done:
            completed = next(task for task in tasks if task in done)
            await completed
    finally:
        for task in (*tasks, shutdown_task):
            task.cancel()
        await asyncio.gather(*tasks, shutdown_task, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)
