"""Shared strict Arr v3 imported-download correlation."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..config import ReconciliationPolicy, SecretReference
from ..reconciler.scheduler import UpgradeEntity
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
class ArrReleaseSearch:
    """A completed interactive search, including a valid empty result."""

    outcome: str
    release: ArrRelease | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "transient_failure"}:
            raise ValueError("release search outcome is invalid")
        if self.outcome == "completed" and self.reason is not None:
            raise ValueError("completed release search cannot carry a failure reason")
        if self.outcome == "transient_failure" and (not isinstance(self.reason, str) or not self.reason):
            raise ValueError("transient release failure requires a reason")

    @property
    def available(self) -> bool:
        """Compatibility spelling for a completed, validated Arr response."""
        return self.outcome == "completed"

    @classmethod
    def completed(cls, release: ArrRelease | None = None) -> "ArrReleaseSearch":
        return cls("completed", release)

    @classmethod
    def transient_failure(cls, reason: str) -> "ArrReleaseSearch":
        return cls("transient_failure", None, reason)


@dataclass(frozen=True)
class ArrGrabObservation:
    """One bounded public Arr observation; never an inference from absence."""

    kind: str
    download_id: str | None = None
    torrent_hash: str | None = None
    history_id: int | None = None


@dataclass(frozen=True)
class ArrExternalGrab:
    """A public Arr grab that can be reconciled without a release request."""

    entity_id: str
    download_id: str
    torrent_hash: str
    expected_bytes: int
    history_id: int


@dataclass(frozen=True)
class ArrHistoricalExternalGrab:
    """Exact historical Arr evidence for one explicitly authorized hash."""

    entity_ids: tuple[str, ...]
    download_id: str
    torrent_hash: str
    history_ids: tuple[int, ...]
    queue_expected_bytes: int | None


@dataclass(frozen=True)
class ArrExternalObservation:
    """One bounded observer pass, including its safe causal watermark."""

    watermark: int
    grabs: tuple[ArrExternalGrab, ...]


@dataclass(frozen=True)
class ArrImportedEvent:
    """One exact Arr import eligible for Publisher reconciliation."""

    history_id: int
    download_id: str
    media_id: str
    relative_path: str


def _utc_timestamp(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    timestamp = int(parsed.timestamp())
    return timestamp if timestamp >= 0 else None


def _profile_needs_upgrade(profile: object, file: object) -> bool | None:
    if not isinstance(profile, dict) or not isinstance(file, dict) or profile.get("upgradeAllowed") is not True:
        return False
    current_score = file.get("customFormatScore", 0)
    cutoff_score = profile.get("cutoffFormatScore", 0)
    minimum_gain = profile.get("minUpgradeFormatScore", 1)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (current_score, cutoff_score, minimum_gain)):
        return None
    if current_score < cutoff_score and cutoff_score - current_score >= minimum_gain:
        return True
    quality = file.get("quality")
    current_quality = quality.get("quality") if isinstance(quality, dict) else None
    current_id = _public_id(current_quality.get("id")) if isinstance(current_quality, dict) else None
    cutoff_id = _public_id(profile.get("cutoff"))
    items = profile.get("items")
    if current_id is None or cutoff_id is None or not isinstance(items, list):
        return None
    ranks: dict[str, int] = {}
    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            return None
        quality_item = item.get("quality")
        grouped = item.get("items")
        if isinstance(quality_item, dict):
            quality_id = _public_id(quality_item.get("id"))
            if quality_id is None:
                return None
            ranks[quality_id] = rank
        elif isinstance(grouped, list):
            group_id = _public_id(item.get("id"))
            if group_id is not None:
                ranks[group_id] = rank
            for member in grouped:
                member_quality = member.get("quality") if isinstance(member, dict) else None
                member_id = _public_id(member_quality.get("id")) if isinstance(member_quality, dict) else None
                if member_id is None:
                    return None
                ranks[member_id] = rank
        else:
            return None
    if current_id not in ranks or cutoff_id not in ranks:
        return None
    return ranks[current_id] < ranks[cutoff_id]


class ArrHistoryAdapter:
    media_keys: tuple[str, ...] = ()
    source_name = ""
    item_type = ""
    release_entity_key = ""
    category_field_name = ""
    def __init__(self, base_url: str, api_key: SecretReference, *, staging_root: Path | None, arr_import_path_prefix: str | None = None, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._staging_root = staging_root
        self._arr_import_path_prefix = _canonical_arr_path(arr_import_path_prefix)
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

    def _release_resources(
        self,
        entity_id: str,
        *,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> tuple[bool, list[dict[str, object]]]:
        public_id = _public_id(int(entity_id)) if entity_id.isdecimal() else None
        if public_id != entity_id or not self.release_entity_key:
            return False, []
        request = Request(f"{self._base_url}/api/v3/release?{urlencode({self.release_entity_key: entity_id})}", headers={"X-Api-Key": self._resolve(self._api_key)})
        try:
            status, body = request_bytes(
                request,
                timeout=self._timeout if timeout_seconds is None else timeout_seconds,
                max_response_bytes=1_048_576 if max_response_bytes is None else max_response_bytes,
            )
            releases = json.loads(body.decode("utf-8"))
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return False, []
        if status != 200 or not isinstance(releases, list) or not all(isinstance(resource, dict) for resource in releases):
            return False, []
        return True, releases

    @staticmethod
    def _materialize_release(resource: dict[str, object]) -> ArrRelease | None:
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

    def first_approved_release(self, entity_id: str) -> ArrRelease | None:
        available, releases = self._release_resources(entity_id)
        if not available:
            return None
        for resource in releases:
            if resource.get("approved") is not True:
                continue
            return self._materialize_release(resource)
        return None

    def approved_release(
        self,
        entity_id: str,
        policy: ReconciliationPolicy,
        *,
        current_score: int,
    ) -> ArrReleaseSearch:
        available, releases = self._release_resources(
            entity_id,
            timeout_seconds=policy.release_timeout_seconds,
            max_response_bytes=policy.max_release_response_bytes,
        )
        if not available:
            return ArrReleaseSearch.transient_failure("release response was unavailable or invalid")
        for resource in releases:
            if resource.get("approved") is not True:
                continue
            raw_score = resource.get("customFormatScore", 0)
            raw_formats = resource.get("customFormats", [])
            if isinstance(raw_score, bool) or not isinstance(raw_score, int) or not isinstance(raw_formats, list):
                return ArrReleaseSearch.transient_failure("release custom format metadata was invalid")
            names: list[str] = []
            for item in raw_formats:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    return ArrReleaseSearch.transient_failure("release custom format metadata was invalid")
                names.append(item["name"])
            selected = set(names)
            if raw_score < policy.minimum_candidate_score or raw_score - current_score < policy.minimum_score_gain:
                continue
            if not set(policy.required_candidate_formats).issubset(selected):
                continue
            if set(policy.forbidden_candidate_formats) & selected:
                continue
            release = self._materialize_release(resource)
            if release is None:
                return ArrReleaseSearch.transient_failure("approved release metadata was invalid")
            return ArrReleaseSearch.completed(release)
        return ArrReleaseSearch.completed()

    def _cutoff_entity(self, record: dict[str, object]) -> UpgradeEntity | None:
        raise NotImplementedError

    def cutoff_entities(self) -> tuple[UpgradeEntity, ...] | None:
        records = self._paged("wanted/cutoff")
        if records is None:
            return None
        entities: list[UpgradeEntity] = []
        for record in records:
            entity = self._cutoff_entity(record)
            if entity is None:
                return None
            entities.append(entity)
        if len({entity.entity_id for entity in entities}) != len(entities):
            return None
        return tuple(entities)

    def upgrade_entities(self) -> tuple[UpgradeEntity, ...] | None:
        raise NotImplementedError

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

    def mark_history_failed(self, history_id: int) -> bool:
        if isinstance(history_id, bool) or not isinstance(history_id, int) or history_id <= 0:
            return False
        request = Request(f"{self._base_url}/api/v3/history/failed/{history_id}", data=b"", headers={"X-Api-Key": self._resolve(self._api_key)}, method="POST")
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
        matches: list[tuple[str, int]] = []
        for record in history:
            record_id, download_id = record.get("id"), record.get("downloadId")
            if record.get("eventType") != "grabbed" or _public_id(record.get(self.release_entity_key)) != entity_id or record.get("sourceTitle") != title:
                continue
            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= watermark or not isinstance(download_id, str) or not download_id:
                return ArrGrabObservation("unknown")
            matches.append((download_id, record_id))
        if not matches:
            return ArrGrabObservation("absent")
        if len(matches) != 1:
            return ArrGrabObservation("ambiguous")
        queue = self._paged("queue")
        if queue is None:
            return ArrGrabObservation("unknown")
        download_id, history_id = matches[0]
        queue_matches = [
            record for record in queue
            if _public_id(record.get(self.release_entity_key)) == entity_id
            and record.get("title") == title
            and record.get("downloadId") == download_id
            and record.get("protocol") == "torrent"
            and not isinstance(record.get("size"), bool)
            and isinstance(record.get("size"), (int, float))
            and record.get("size") in {0, release.expected_bytes}
        ]
        if len(queue_matches) == 1:
            torrent_hash = download_id.lower()
            if len(torrent_hash) != 40 or any(character not in "0123456789abcdef" for character in torrent_hash):
                return ArrGrabObservation("unknown")
            return ArrGrabObservation("observed", download_id, torrent_hash, history_id)
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
                and item["size"] >= 0
            ]
            if len(queue_matches) != 1:
                return None
            expected_bytes = queue_matches[0]["size"]
            if expected_bytes == 0:
                expected_bytes = _history_release_size(record)
                if expected_bytes is None:
                    return None
            seen_downloads.add(torrent_hash)
            grabs.append(ArrExternalGrab(entity_id, download_id, torrent_hash, expected_bytes, history_id))
        return ArrExternalObservation(max(record["id"] for record in later), tuple(grabs))

    def sealed_external_grab(self, entity_id: str, torrent_hash: str, *, category: str, download_client_id: int) -> ArrExternalGrab | None:
        """Re-observe one deployment-authorized existing Arr grab exactly once."""
        public_id = _public_id(int(entity_id)) if isinstance(entity_id, str) and entity_id.isdecimal() else None
        if public_id != entity_id or not isinstance(torrent_hash, str) or len(torrent_hash) != 40 or any(character not in "0123456789abcdef" for character in torrent_hash):
            return None
        client_name = self._stopped_qbittorrent_client_name(category, download_client_id)
        if client_name is None:
            return None
        history = self._paged("history")
        queue = self._paged("queue")
        if history is None or queue is None:
            return None
        matching_history = [
            record for record in history
            if record.get("eventType") == "grabbed"
            and _public_id(record.get(self.release_entity_key)) == entity_id
            and record.get("downloadId") == torrent_hash.upper()
            and isinstance(record.get("id"), int) and not isinstance(record.get("id"), bool) and record["id"] > 0
        ]
        if len(matching_history) != 1:
            return None
        matching_queue = [
            record for record in queue
            if _public_id(record.get(self.release_entity_key)) == entity_id
            and record.get("downloadId") == torrent_hash.upper()
            and record.get("downloadClient") == client_name
            and record.get("protocol") == "torrent"
            and isinstance(record.get("size"), int) and not isinstance(record.get("size"), bool) and record["size"] > 0
        ]
        if len(matching_queue) != 1:
            return None
        return ArrExternalGrab(entity_id, torrent_hash.upper(), torrent_hash, matching_queue[0]["size"], matching_history[0]["id"])

    def sealed_historical_external_grab(self, entity_ids: tuple[str, ...], torrent_hash: str, *, category: str, download_client_id: int) -> ArrHistoricalExternalGrab | None:
        """Seal historical evidence without extending v1 Queue-required semantics.

        This is intentionally separate from :meth:`sealed_external_grab`: the
        latter remains the v1 singleton, Queue-required operation.  A historical
        pack is valid only when every public History record for the hash forms
        the requested canonical entity set.
        """
        if (
            not isinstance(entity_ids, tuple) or not 1 <= len(entity_ids) <= 128
            or any(not isinstance(item, str) or not item.isdecimal() or str(int(item)) != item for item in entity_ids)
            or entity_ids != tuple(sorted(entity_ids, key=int)) or len(set(entity_ids)) != len(entity_ids)
            or (self.source_name == "radarr" and len(entity_ids) != 1)
            or not isinstance(torrent_hash, str) or len(torrent_hash) != 40
            or any(character not in "0123456789abcdef" for character in torrent_hash)
        ):
            return None
        client_name = self._stopped_qbittorrent_client_name(category, download_client_id)
        if client_name is None:
            return None
        history = self._paged("history")
        queue = self._paged("queue")
        if history is None or queue is None:
            return None
        download_id = torrent_hash.upper()

        def matches_hash(value: object) -> bool:
            return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value) and value.lower() == torrent_hash

        matching_history = [
            record for record in history
            if record.get("eventType") == "grabbed" and matches_hash(record.get("downloadId"))
        ]
        observed_ids = [_public_id(record.get(self.release_entity_key)) for record in matching_history]
        history_ids = [record.get("id") for record in matching_history]
        if (
            len(matching_history) != len(entity_ids)
            or tuple(sorted(observed_ids, key=lambda item: -1 if item is None else int(item))) != entity_ids
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in history_ids)
            or len(set(history_ids)) != len(history_ids)
        ):
            return None
        # Queue is optional only for this explicit historical operation.  When
        # it still exists, every row linked either by the requested entities or
        # the hash must be the same complete evidence; a stale or competing
        # row for one requested episode is an ambiguity, not an ignorable row.
        matching_queue = [
            record for record in queue
            if matches_hash(record.get("downloadId")) or _public_id(record.get(self.release_entity_key)) in entity_ids
        ]
        if not matching_queue:
            return ArrHistoricalExternalGrab(entity_ids, download_id, torrent_hash, tuple(history_ids), None)
        queue_ids = [_public_id(record.get(self.release_entity_key)) for record in matching_queue]
        sizes = [record.get("size") for record in matching_queue]
        if (
            len(matching_queue) != len(entity_ids)
            or tuple(sorted(queue_ids, key=lambda item: -1 if item is None else int(item))) != entity_ids
            or any(not matches_hash(record.get("downloadId")) or record.get("downloadClient") != client_name or record.get("protocol") != "torrent" for record in matching_queue)
            or any(isinstance(size, bool) or not isinstance(size, int) or size <= 0 for size in sizes)
            or len(set(sizes)) != 1
        ):
            return None
        return ArrHistoricalExternalGrab(entity_ids, download_id, torrent_hash, tuple(history_ids), sizes[0])

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
        if not download_id or not media_id or self._staging_root is None or self._arr_import_path_prefix is None:
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
            imported_path = _canonical_arr_path(imported)
            if imported_path is None:
                return None
            try:
                relative = imported_path.relative_to(self._arr_import_path_prefix)
            except ValueError:
                return None
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                return None
            candidate = self._staging_root.joinpath(*relative.parts)
            try:
                candidate.relative_to(self._staging_root)
            except ValueError:
                return None
            entity_id = self._entity_id(record)
            if entity_id is None:
                return None
            candidates.append((str(relative), entity_id))
        return candidates[0] if len(candidates) == 1 else None

    def imported_after(self, cursor: int, *, maximum: int) -> tuple[int, tuple[ArrImportedEvent, ...]] | None:
        """Read bounded, validated Arr imports after one durable cursor.

        The cursor advances only across a syntactically complete history page.
        A path outside the configured staging root is ignored but still
        consumed: it is not an authority to enumerate or publish files.
        """
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            return None
        history = self._paged("history")
        if history is None:
            return None
        records = sorted(history, key=lambda record: record.get("id", -1))
        ids = [record.get("id") for record in records]
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids) or len(set(ids)) != len(ids):
            return None
        events: list[ArrImportedEvent] = []
        next_cursor = cursor
        for record in records:
            history_id = record["id"]
            if history_id <= cursor:
                continue
            if record.get("eventType") != "downloadFolderImported":
                next_cursor = history_id
                continue
            download_id = record.get("downloadId")
            media_id = _public_id(record.get(self.release_entity_key))
            data = record.get("data")
            imported = data.get("importedPath") if isinstance(data, dict) else None
            if not isinstance(download_id, str) or not download_id or media_id is None or not isinstance(imported, str):
                return None
            imported_path = _canonical_arr_path(imported)
            if imported_path is None or self._arr_import_path_prefix is None:
                return None
            try:
                relative = imported_path.relative_to(self._arr_import_path_prefix)
            except ValueError:
                next_cursor = history_id
                continue
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                return None
            events.append(ArrImportedEvent(history_id, download_id, media_id, str(relative)))
            next_cursor = history_id
            if len(events) >= maximum:
                break
        return next_cursor, tuple(events)

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


def _history_release_size(record: object) -> int | None:
    data = record.get("data") if isinstance(record, dict) else None
    value = data.get("size") if isinstance(data, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _canonical_arr_path(value: object) -> PurePosixPath | None:
    """Validate one canonical absolute Arr-visible path without filesystem IO."""
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        return None
    path = PurePosixPath(value)
    return path if path.is_absolute() else None
