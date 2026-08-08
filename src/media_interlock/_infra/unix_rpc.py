"""Bounded canonical JSON frames for local Unix stream sockets."""

from __future__ import annotations

import json
import asyncio
from typing import Any


class FrameError(ValueError):
    """Raised for malformed, ambiguous, or oversized local frames."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("duplicate JSON key")
        result[key] = value
    return result


def encode_frame(value: object, maximum_bytes: int = 64 * 1024) -> bytes:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FrameError("frame must be canonical JSON") from exc
    if len(raw) + 1 > maximum_bytes:
        raise FrameError("frame exceeds maximum size")
    return raw + b"\n"


def decode_frame(raw: bytes, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    if len(raw) > maximum_bytes or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise FrameError("frame must be exactly one bounded newline-delimited value")
    try:
        value = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError("frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FrameError("frame must be a JSON object")
    if raw != encode_frame(value, maximum_bytes):
        raise FrameError("frame must use canonical JSON")
    return value


async def read_frame(reader: asyncio.StreamReader, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    """Read exactly one bounded frame from a Unix stream reader."""
    try:
        raw = await reader.readuntil(b"\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise FrameError("stream did not provide one bounded frame") from exc
    return decode_frame(raw, maximum_bytes)


async def write_frame(writer: asyncio.StreamWriter, value: object, maximum_bytes: int = 64 * 1024) -> None:
    """Write and drain exactly one bounded frame to a Unix stream writer."""
    writer.write(encode_frame(value, maximum_bytes))
    await writer.drain()
