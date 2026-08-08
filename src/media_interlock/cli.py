"""Shared human and machine result rendering conventions."""

from __future__ import annotations

import json

from .contracts import StatusCode


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
