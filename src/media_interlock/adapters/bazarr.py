"""Bazarr subtitle capability readiness using its status API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..config import SecretReference
from ._http import request_bytes


class BazarrAdapter:
    """Narrow readiness check; subtitle work remains Bazarr-owned."""

    def __init__(self, base_url: str, api_key: SecretReference, *, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds

    def _get(self) -> Any:
        request = Request(f"{self._base_url}/api/system/status", headers={"X-Api-Key": self._resolve(self._api_key)})
        status, body = request_bytes(request, timeout=self._timeout)
        if status != 200:
            raise RuntimeError("unexpected Bazarr status")
        return json.loads(body.decode("utf-8"))

    def ready(self) -> bool:
        try:
            response = self._get()
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        data = response.get("data") if isinstance(response, dict) else None
        return isinstance(data, dict) and data.get("bazarr_version") == "1.6.0"
