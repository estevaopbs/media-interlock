"""Shared strict Arr v3 imported-download correlation."""

from __future__ import annotations

import json
import hashlib
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


@dataclass(frozen=True)
class ArrRelease:
    resource: dict[str, object]
    selector_fingerprint: str
    expected_bytes: int


@dataclass(frozen=True)
class ArrGrabObservation:
    """One bounded public Arr observation; never an inference from absence."""

    kind: str
    download_id: str | None = None
    torrent_hash: str | None = None


@dataclass(frozen=True)
class ArrExternalGrab:
    """A public Arr grab that can be reconciled without a release request."""

    entity_id: str
    download_id: str
    torrent_hash: str
    expected_bytes: int
    history_id: int


@dataclass(frozen=True)
class ArrExternalObservation:
    """One bounded observer pass, including its safe causal watermark."""

    watermark: int
    grabs: tuple[ArrExternalGrab, ...]


class ArrHistoryAdapter:
    media_keys: tuple[str, ...] = ()
    source_name = ""
    item_type = ""
    release_entity_key = ""
    category_field_name = ""
    def __init__(self, base_url: str, api_key: SecretReference, *, staging_root: Path | None, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._staging_root = None if staging_root is None else staging_root.resolve(strict=False)
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

    def first_approved_release(self, entity_id: str) -> ArrRelease | None:
        public_id = _public_id(int(entity_id)) if entity_id.isdecimal() else None
        if public_id != entity_id or not self.release_entity_key:
            return None
        request = Request(f"{self._base_url}/api/v3/release?{urlencode({self.release_entity_key: entity_id})}", headers={"X-Api-Key": self._resolve(self._api_key)})
        try:
            status, body = request_bytes(request, timeout=self._timeout)
            releases = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if status != 200 or not isinstance(releases, list):
            return None
        for resource in releases:
            if not isinstance(resource, dict):
                return None
            if resource.get("approved") is not True:
                continue
            if resource.get("protocol") != "torrent":
                return None
            guid, title, locator, size = resource.get("guid"), resource.get("title"), resource.get("downloadUrl"), resource.get("size")
            if not isinstance(guid, str) or not guid or not isinstance(title, str) or not title or not isinstance(locator, str) or not locator or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                return None
            try:
                encoded = json.dumps(resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                return None
            return ArrRelease(resource, hashlib.sha256(encoded).hexdigest(), size)
        return None

    def grab_release(self, release: ArrRelease) -> bool:
        if not isinstance(release, ArrRelease):
            return False
        try:
            body = json.dumps(release.resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if hashlib.sha256(body).hexdigest() != release.selector_fingerprint:
            return False
        request = Request(f"{self._base_url}/api/v3/release", data=body, headers={"X-Api-Key": self._resolve(self._api_key), "Content-Type": "application/json"}, method="POST")
        try:
            status, _ = request_bytes(request, timeout=self._timeout)
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return status == 200

    def _paged(self, endpoint: str) -> list[dict[str, object]] | None:
        records: list[dict[str, object]] = []
        for page in range(1, 11):
            query = urlencode({"page": page, "pageSize": 100})
            request = Request(f"{self._base_url}/api/v3/{endpoint}?{query}", headers={"X-Api-Key": self._resolve(self._api_key)})
            try:
                status, body = request_bytes(request, timeout=self._timeout)
                response = json.loads(body.decode("utf-8"))
            except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
                return None
            page_records = response.get("records") if isinstance(response, dict) else None
            if status != 200 or not isinstance(page_records, list) or not all(isinstance(record, dict) for record in page_records):
                return None
            records.extend(page_records)
            total = response.get("totalRecords")
            if not isinstance(total, int) or isinstance(total, bool) or total < len(records):
                return None
            if len(records) >= total:
                return records
        return None

    def history_watermark(self) -> int | None:
        """Return the highest observed public history identity before a grab."""
        history = self._paged("history")
        if history is None:
            return None
        identifiers = [record.get("id") for record in history]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in identifiers):
            return None
        return max(identifiers, default=0)

    def observe_grab(self, entity_id: str, release: ArrRelease, *, watermark: int) -> ArrGrabObservation:
        """Correlate exactly one later Arr grab and queue entry without guessing."""
        public_id = _public_id(int(entity_id)) if entity_id.isdecimal() else None
        if public_id != entity_id or not isinstance(release, ArrRelease) or isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            return ArrGrabObservation("unknown")
        title = release.resource.get("title")
        if not isinstance(title, str) or not title:
            return ArrGrabObservation("unknown")
        history = self._paged("history")
        if history is None:
            return ArrGrabObservation("unknown")
        matches: list[str] = []
        for record in history:
            record_id, download_id = record.get("id"), record.get("downloadId")
            if record.get("eventType") != "grabbed" or _public_id(record.get(self.release_entity_key)) != entity_id or record.get("sourceTitle") != title:
                continue
            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= watermark or not isinstance(download_id, str) or not download_id:
                return ArrGrabObservation("unknown")
            matches.append(download_id)
        if not matches:
            return ArrGrabObservation("absent")
        if len(matches) != 1:
            return ArrGrabObservation("ambiguous")
        queue = self._paged("queue")
        if queue is None:
            return ArrGrabObservation("unknown")
        download_id = matches[0]
        queue_matches = [
            record for record in queue
            if _public_id(record.get(self.release_entity_key)) == entity_id
            and record.get("title") == title
            and record.get("downloadId") == download_id
            and record.get("protocol") == "torrent"
            and not isinstance(record.get("size"), bool)
            and isinstance(record.get("size"), (int, float))
            and record.get("size") == release.expected_bytes
        ]
        if len(queue_matches) == 1:
            torrent_hash = download_id.lower()
            if len(torrent_hash) != 40 or any(character not in "0123456789abcdef" for character in torrent_hash):
                return ArrGrabObservation("unknown")
            return ArrGrabObservation("observed", download_id, torrent_hash)
        if not queue_matches:
            return ArrGrabObservation("absent")
        return ArrGrabObservation("ambiguous")

    def _stopped_qbittorrent_client_name(self, category: str, download_client_id: int) -> str | None:
        if not self.category_field_name or not isinstance(category, str) or not category or isinstance(download_client_id, bool) or not isinstance(download_client_id, int) or download_client_id <= 0:
            return None
        request = Request(f"{self._base_url}/api/v3/downloadclient", headers={"X-Api-Key": self._resolve(self._api_key)})
        try:
            status, body = request_bytes(request, timeout=self._timeout)
            clients = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if status != 200 or not isinstance(clients, list):
            return None
        selected_name: str | None = None
        seen_ids: set[int] = set()
        for client in clients:
            if not isinstance(client, dict):
                return None
            client_id = client.get("id")
            if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id <= 0 or client_id in seen_ids:
                return None
            seen_ids.add(client_id)
            if client.get("enable") is not True or client.get("protocol") != "torrent" or client.get("implementation") != "QBittorrent":
                continue
            fields = client.get("fields")
            if not isinstance(fields, list):
                return None
            values: dict[str, object] = {}
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] in values:
                    return None
                values[field["name"]] = field.get("value")
            if values.get(self.category_field_name) != category:
                continue
            if client_id != download_client_id or values.get("initialState") != 2 or selected_name is not None:
                return None
            name = client.get("name")
            if not isinstance(name, str) or not name:
                return None
            selected_name = name
        if selected_name is None:
            return None
        if sum(client.get("enable") is True and client.get("name") == selected_name for client in clients if isinstance(client, dict)) != 1:
            return None
        return selected_name

    def external_grabs_after(self, watermark: int, *, category: str, download_client_id: int) -> ArrExternalObservation | None:
        """Observe all exact external torrent grabs after a persisted watermark.

        Queue exposes a download-client *name*, not its configuration identity.
        The name is therefore accepted only after a unique public
        ``downloadclient`` lookup of the configured positive client id.
        """
        if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            return None
        client_name = self._stopped_qbittorrent_client_name(category, download_client_id)
        if client_name is None:
            return None
        history = self._paged("history")
        queue = self._paged("queue")
        if history is None or queue is None:
            return None
        history_ids = [record.get("id") for record in history]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in history_ids):
            return None
        later = sorted((record for record in history if record["id"] > watermark), key=lambda record: record["id"])
        if not later:
            return ArrExternalObservation(watermark, ())
        grabs: list[ArrExternalGrab] = []
        seen_history: set[int] = set()
        seen_downloads: set[str] = set()
        for record in later:
            history_id = record["id"]
            if history_id in seen_history:
                return None
            seen_history.add(history_id)
            if record.get("eventType") != "grabbed":
                continue
            entity_id = _public_id(record.get(self.release_entity_key))
            download_id = record.get("downloadId")
            if entity_id is None or not isinstance(download_id, str) or len(download_id) != 40 or any(character not in "0123456789abcdefABCDEF" for character in download_id):
                return None
            torrent_hash = download_id.lower()
            if torrent_hash in seen_downloads:
                return None
            queue_matches = [
                item for item in queue
                if _public_id(item.get(self.release_entity_key)) == entity_id
                and item.get("downloadId") == download_id
                and item.get("downloadClient") == client_name
                and item.get("protocol") == "torrent"
                and isinstance(item.get("size"), int)
                and not isinstance(item.get("size"), bool)
                and item["size"] > 0
            ]
            if len(queue_matches) != 1:
                return None
            seen_downloads.add(torrent_hash)
            grabs.append(ArrExternalGrab(entity_id, download_id, torrent_hash, queue_matches[0]["size"], history_id))
        return ArrExternalObservation(max(record["id"] for record in later), tuple(grabs))

    def stopped_qbittorrent_client(self, category: str, download_client_id: int) -> bool:
        """Require the configured enabled Arr qBittorrent client to add stopped.

        A distinct enabled qBittorrent client with the same source category is
        ambiguous; unrelated clients remain outside this source's authority.
        """
        if not self.category_field_name or not isinstance(category, str) or not category or isinstance(download_client_id, bool) or not isinstance(download_client_id, int) or download_client_id <= 0:
            return False
        request = Request(f"{self._base_url}/api/v3/downloadclient", headers={"X-Api-Key": self._resolve(self._api_key)})
        try:
            status, body = request_bytes(request, timeout=self._timeout)
            clients = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if status != 200 or not isinstance(clients, list):
            return False
        matched_configured_client = False
        for client in clients:
            if not isinstance(client, dict):
                return False
            if client.get("enable") is not True:
                continue
            if client.get("protocol") != "torrent" or client.get("implementation") != "QBittorrent":
                continue
            fields = client.get("fields")
            if not isinstance(fields, list):
                return False
            values: dict[str, object] = {}
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] in values:
                    return False
                values[field["name"]] = field.get("value")
            if values.get(self.category_field_name) != category:
                continue
            client_id = client.get("id")
            if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id <= 0:
                return False
            if client_id != download_client_id:
                return False
            if values.get("initialState") != 2 or matched_configured_client:
                return False
            matched_configured_client = True
        return matched_configured_client

    def _matched_import(self, download_id: str, media_id: str) -> tuple[str, str] | None:
        if not download_id or not media_id or self._staging_root is None:
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
