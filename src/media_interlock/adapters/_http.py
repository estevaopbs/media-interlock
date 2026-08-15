"""Bounded, no-redirect HTTP primitive for authenticated adapters."""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def request_bytes(
    request: Request,
    *,
    timeout: float,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, bytes]:
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
        raise ValueError("response bound is invalid")
    try:
        with open_response(request, timeout=timeout) as response:
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise RuntimeError("upstream response exceeds bound")
            return response.status, body
    except HTTPError as exc:
        exc.close()
        raise


def open_response(request: Request, *, timeout: float):
    """Open a response without redirect handling for bounded streaming callers."""
    return build_opener(_NoRedirect()).open(request, timeout=timeout)
