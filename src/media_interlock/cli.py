"""Shared human and machine result rendering conventions."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .contracts import StatusCode
from .runtime import MediaInterlockRuntime


def render_result(status: StatusCode | str, message: str, *, as_json: bool = False) -> str:
    """Render a bounded status result; callers provide redacted messages only."""
    status_value = StatusCode(status).value
    if as_json:
        return json.dumps(
            {"version": "v1", "status": status_value, "message": message},
            sort_keys=True,
            separators=(",", ":"),
        )
    return f"{status_value}: {message}"


def main(argv: list[str] | None = None) -> int:
    """Public probe surface for the single MediaInterlock runtime."""
    parser = argparse.ArgumentParser(prog="media-interlock")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(__version__)
        return 0
    if arguments.config is None:
        parser.error("--config is required")
    try:
        load_config(arguments.config)
    except ConfigError as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    if arguments.check_config:
        print(render_result("ok", "configuration valid", as_json=arguments.json))
        return 0
    if arguments.daemon:
        runtime: MediaInterlockRuntime | None = None
        try:
            runtime = MediaInterlockRuntime.from_config(load_config(arguments.config))
            asyncio.run(runtime.run())
        except (ConfigError, OSError, RuntimeError) as exc:
            print(render_result("unavailable", str(exc), as_json=arguments.json))
            return 1
        finally:
            if runtime is not None:
                runtime.close()
        return 0
    parser.error("--daemon is required for normal execution")
    return 2
