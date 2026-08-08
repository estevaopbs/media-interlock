"""Prowlarr health capability used only to inhibit Fence admission."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..config import SecretReference
from ._http import request_bytes


class ProwlarrAdapter:
    def __init__(self, base_url: str, api_key: SecretReference, *, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds

    def _get(self, path: str) -> Any:
        request = Request(f"{self._base_url}{path}", headers={"X-Api-Key": self._resolve(self._api_key)})
        status, body = request_bytes(request, timeout=self._timeout)
        if status != 200:
            raise RuntimeError("unexpected Prowlarr status")
        return json.loads(body.decode("utf-8"))

    def ready(self) -> bool:
        try:
            health = self._get("/api/v1/health")
            indexers = self._get("/api/v1/indexer")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(health, list) or not isinstance(indexers, list):
            return False
        if health:
            return False
        return any(isinstance(indexer, dict) and indexer.get("enable") is True for indexer in indexers)
