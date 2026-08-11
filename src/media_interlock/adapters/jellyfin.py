"""Jellyfin catalog refresh adapter using the pinned public API surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..config import SecretReference
from ._http import open_response, request_bytes


@dataclass(frozen=True)
class CatalogSubmission:
    accepted: bool
    delivered: bool = False


@dataclass(frozen=True)
class CatalogExpectation:
    """The immutable facts a catalog item must prove before delivery."""

    library_id: str
    internal_path: str
    item_type: str
    provider_ids: Mapping[str, str]
    expected_bytes: int
    known_item_id: str | None = None

    def valid(self) -> bool:
        return (
            bool(self.library_id)
            and self.internal_path.startswith("/")
            and self.item_type in {"Movie", "Episode"}
            and all(isinstance(key, str) and key and isinstance(value, str) and value for key, value in self.provider_ids.items())
            and isinstance(self.expected_bytes, int)
            and self.expected_bytes >= 0
            and (self.known_item_id is None or bool(self.known_item_id))
        )


@dataclass(frozen=True)
class CatalogObservation:
    item_id: str
    media_source_id: str
    internal_path: str
    bytes_observed: int


class JellyfinAdapter:
    # MediaSources can make even a modest episode page exceed the bounded
    # 4 MiB HTTP response limit. Keep the same 1,000-item observation ceiling
    # while requesting smaller pages from Jellyfin.
    _PAGE_SIZE = 10
    _MAX_PAGES = 100
    def __init__(self, base_url: str, api_key: SecretReference, *, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds

    def _request(self, method: str, path: str, body: object | None = None) -> bytes:
        data = None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"X-Emby-Token": self._resolve(self._api_key)}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        status, body = request_bytes(request, timeout=self._timeout)
        if status not in ({200} if method == "GET" else {200, 204}):
            raise RuntimeError("unexpected Jellyfin status")
        return body

    def _get_json(self, path: str) -> Any:
        return json.loads(self._request("GET", path).decode("utf-8"))

    def _post(self, path: str) -> bool:
        try:
            self._request("POST", path)
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def ready(self) -> bool:
        try:
            info = self._get_json("/System/Info")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(info, dict) and info.get("Version") == "10.11.11"

    def deliver(self, operation_id: str, generation_id: str) -> bool:
        # Kept only as a compatibility surface. A refresh acknowledgement is
        # never evidence that the catalog has adopted an exact media source.
        if not operation_id or not generation_id:
            return False
        self.submit_refresh()
        return False

    def submit_refresh(self) -> CatalogSubmission:
        try:
            self._request("POST", "/Library/Refresh")
        except (HTTPError, URLError, OSError, RuntimeError, TypeError, ValueError):
            return CatalogSubmission(False)
        return CatalogSubmission(True)

    def submit_update(self, internal_path: str, update_type: str) -> CatalogSubmission:
        if not internal_path.startswith("/") or update_type not in {"created", "modified"}:
            return CatalogSubmission(False)
        try:
            self._request("POST", "/Library/Media/Updated", {"Updates": [{"Path": internal_path, "UpdateType": update_type.title()}]})
        except (HTTPError, URLError, OSError, RuntimeError, TypeError, ValueError):
            return CatalogSubmission(False)
        return CatalogSubmission(True)

    def observe_catalog(self, expected: CatalogExpectation) -> CatalogObservation | None:
        """Find exactly one matching source by locally filtering library pages.

        Jellyfin's documented Items query is deliberately not given a Path
        predicate here.  The caller's configured library bounds the query and
        exact path/provider/source checks happen locally.
        """
        if not expected.valid():
            return None
        matches: list[CatalogObservation] = []
        try:
            for page in range(self._MAX_PAGES):
                start_index = page * self._PAGE_SIZE
                query = urlencode(
                    {
                        "ParentId": expected.library_id,
                        "Recursive": "true",
                        "IncludeItemTypes": expected.item_type,
                        "Fields": "Path,ProviderIds,MediaSources",
                        "EnableTotalRecordCount": "true",
                        "StartIndex": str(start_index),
                        "Limit": str(self._PAGE_SIZE),
                    }
                )
                response = self._get_json(f"/Items?{query}")
                if not isinstance(response, dict):
                    return None
                items = response.get("Items")
                total = response.get("TotalRecordCount")
                if not isinstance(items, list) or not isinstance(total, int) or total < 0:
                    return None
                for item in items:
                    observation = self._matching_source(item, expected)
                    if observation is not None:
                        matches.append(observation)
                if len(matches) > 1:
                    return None
                if start_index + len(items) >= total:
                    break
                if len(items) != self._PAGE_SIZE:
                    return None
            else:
                return None
        except (HTTPError, URLError, OSError, RuntimeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _matching_source(item: object, expected: CatalogExpectation) -> CatalogObservation | None:
        if not isinstance(item, dict):
            return None
        item_id = item.get("Id")
        provider_ids = item.get("ProviderIds")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item.get("Path") != expected.internal_path
            or item.get("Type") != expected.item_type
            or not isinstance(provider_ids, dict)
            or provider_ids != dict(expected.provider_ids)
            or (expected.known_item_id is not None and item_id != expected.known_item_id)
        ):
            return None
        sources = item.get("MediaSources")
        if not isinstance(sources, list):
            return None
        compatible = [
            source
            for source in sources
            if isinstance(source, dict)
            and isinstance(source.get("Id"), str)
            and source.get("Id")
            and source.get("Path") == expected.internal_path
            and source.get("Size") == expected.expected_bytes
        ]
        if len(compatible) != 1:
            return None
        return CatalogObservation(item_id, compatible[0]["Id"], expected.internal_path, expected.expected_bytes)

    def direct_play_matches(self, observation: CatalogObservation, *, expected_bytes: int, expected_sha256: str) -> bool:
        """Read the exact observed static source and compare its sealed digest."""
        if (
            not observation.item_id
            or not observation.media_source_id
            or not observation.internal_path.startswith("/")
            or expected_bytes < 0
            or len(expected_sha256) != 64
        ):
            return False
        query = urlencode({"MediaSourceId": observation.media_source_id, "static": "true"})
        request = Request(
            f"{self._base_url}/Videos/{observation.item_id}/stream?{query}",
            headers={"X-Emby-Token": self._resolve(self._api_key)},
            method="GET",
        )
        try:
            import hashlib

            digest = hashlib.sha256()
            actual_bytes = 0
            with open_response(request, timeout=self._timeout) as response:
                if response.status != 200:
                    return False
                while chunk := response.read(1024 * 1024):
                    actual_bytes += len(chunk)
                    if actual_bytes > expected_bytes:
                        return False
                    digest.update(chunk)
            return actual_bytes == expected_bytes and digest.hexdigest() == expected_sha256
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
