"""Shared strict Arr v3 imported-download correlation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..config import SecretReference
from ._http import request_bytes


@dataclass(frozen=True)
class ArrCandidate:
    relative_path: str
    asset_slot: str
    item_type: str
    provider_ids: dict[str, str]


class ArrHistoryAdapter:
    media_keys: tuple[str, ...] = ()
    source_name = ""
    item_type = ""
    def __init__(self, base_url: str, api_key: SecretReference, *, staging_root: Path, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._staging_root = staging_root.resolve(strict=False)
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds

    def _history(self, download_id: str) -> Any:
        query = urlencode({"downloadId": download_id, "pageSize": "1000"})
        request = Request(f"{self._base_url}/api/v3/history?{query}", headers={"X-Api-Key": self._resolve(self._api_key)})
        status, body = request_bytes(request, timeout=self._timeout)
        if status != 200:
            raise RuntimeError("unexpected Arr status")
        return json.loads(body.decode("utf-8"))

    def _entity(self, path: str) -> Any:
        request = Request(f"{self._base_url}{path}", headers={"X-Api-Key": self._resolve(self._api_key)})
        status, body = request_bytes(request, timeout=self._timeout)
        if status != 200:
            raise RuntimeError("unexpected Arr status")
        return json.loads(body.decode("utf-8"))

    def _matched_import(self, download_id: str, media_id: str) -> tuple[str, str] | None:
        if not download_id or not media_id:
            return None
        try:
            response = self._history(download_id)
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        records = response.get("records") if isinstance(response, dict) else None
        if not isinstance(records, list):
            return None
        candidates: list[tuple[str, str]] = []
        for record in records:
            if not isinstance(record, dict) or record.get("eventType") != "downloadFolderImported" or record.get("downloadId") != download_id:
                continue
            matched = any(_public_id(record.get(key)) == media_id for key in self.media_keys)
            if not matched:
                continue
            data = record.get("data")
            imported = data.get("importedPath") if isinstance(data, dict) else None
            if not isinstance(imported, str) or not imported:
                return None
            try:
                relative = Path(imported).resolve(strict=False).relative_to(self._staging_root)
            except ValueError:
                return None
            if not relative.parts:
                return None
            entity_id = self._entity_id(record)
            if entity_id is None:
                return None
            candidates.append((str(relative), entity_id))
        return candidates[0] if len(candidates) == 1 else None

    def candidate_relative_path(self, download_id: str, media_id: str) -> str | None:
        matched = self._matched_import(download_id, media_id)
        return None if matched is None else matched[0]

    def candidate_identity(self, download_id: str, media_id: str) -> ArrCandidate | None:
        matched = self._matched_import(download_id, media_id)
        if matched is None:
            return None
        relative_path, entity_id = matched
        try:
            entity = self._entity(self._entity_path(entity_id))
            return self._identity_from_entity(entity_id, relative_path, entity)
        except (HTTPError, URLError, OSError, RuntimeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _entity_path(self, media_id: str) -> str:
        raise NotImplementedError

    def _identity_from_entity(self, media_id: str, relative_path: str, entity: object) -> ArrCandidate | None:
        raise NotImplementedError

    def _entity_id(self, record: dict[str, Any]) -> str | None:
        raise NotImplementedError


def _public_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return str(value)
