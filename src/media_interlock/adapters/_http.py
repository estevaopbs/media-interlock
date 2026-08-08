"""Bounded, no-redirect HTTP primitive for authenticated adapters."""

from __future__ import annotations

from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def request_bytes(request: Request, *, timeout: float) -> tuple[int, bytes]:
    with open_response(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("upstream response exceeds bound")
        return response.status, body


def open_response(request: Request, *, timeout: float):
    """Open a response without redirect handling for bounded streaming callers."""
    return build_opener(_NoRedirect()).open(request, timeout=timeout)
