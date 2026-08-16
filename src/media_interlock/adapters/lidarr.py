"""Strict Lidarr v1 acquisition boundary for managed music candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..config import MusicFencePolicy, ReconciliationPolicy, SecretReference
from ._http import request_bytes
from .arr import ArrGrabObservation, _public_id, _utc_timestamp


@dataclass(frozen=True)
class LidarrAlbum:
    album_id: str
    released_at: int
    monitored: bool = True


@dataclass(frozen=True)
class LidarrRelease:
    """An unchanged public release resource plus the policy evidence."""

    resource: dict[str, object]
    selector_fingerprint: str
    expected_bytes: int
    album_id: str
    reported_seeders: int | None
    indexer: str | None
    canonical_hash: str | None
    custom_format_score: int
    custom_format_names: tuple[str, ...]


class LidarrAdapter:
    """Use only Lidarr's documented v1 album APIs."""

    api_version = "v1"
    source_name = "lidarr"
    category_field_name = "musicCategory"

    def __init__(
        self,
        base_url: str,
        api_key: SecretReference,
        *,
        secret_resolver: Callable[[SecretReference], str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds

    def _api(self, endpoint: str) -> str:
        return f"{self._base_url}/api/{self.api_version}/{endpoint.lstrip('/')}"

    def _get(self, endpoint: str) -> Any:
        request = Request(self._api(endpoint), headers={"X-Api-Key": self._resolve(self._api_key)})
        status, body = request_bytes(request, timeout=self._timeout)
        if status != 200:
            raise RuntimeError("unexpected Lidarr status")
        return json.loads(body.decode("utf-8"))

    def _paged(self, endpoint: str) -> list[dict[str, object]] | None:
        records: list[dict[str, object]] = []
        expected_total: int | None = None
        # A personal library can exceed one thousand albums.  Keep the request
        # size stable for Lidarr, but derive completion from its declared
        # total instead of silently turning a large wanted list into an
        # unavailable source.  The finite ceiling still bounds a malformed
        # server response to 100,000 records.
        for page in range(1, 1_001):
            try:
                response = self._get(f"{endpoint}?{urlencode({'page': page, 'pageSize': 100})}")
            except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
                return None
            page_records = response.get("records") if isinstance(response, dict) else None
            total = response.get("totalRecords") if isinstance(response, dict) else None
            if (
                not isinstance(page_records, list)
                or not all(isinstance(record, dict) for record in page_records)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total < len(records)
            ):
                return None
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                return None
            records.extend(page_records)
            if len(records) == total:
                return records
            if len(records) > total or not page_records:
                return None
        return None

    def missing_albums(self) -> tuple[LidarrAlbum, ...] | None:
        records = self._paged("wanted/missing")
        if records is None:
            return None
        albums: list[LidarrAlbum] = []
        for record in records:
            album_id = _public_id(record.get("id"))
            monitored = record.get("monitored")
            released_at = _utc_timestamp(record.get("releaseDate"))
            if album_id is None or not isinstance(monitored, bool) or released_at is None:
                return None
            albums.append(LidarrAlbum(album_id, released_at, monitored))
        if len({album.album_id for album in albums}) != len(albums):
            return None
        return tuple(albums)

    def missing_monitored_albums(self) -> tuple[LidarrAlbum, ...] | None:
        albums = self.missing_albums()
        return None if albums is None else tuple(album for album in albums if album.monitored)

    @staticmethod
    def _materialize_release(resource: object, album_id: str) -> LidarrRelease | None:
        if not isinstance(resource, dict) or resource.get("protocol") != "torrent":
            return None
        resource_album_id = _public_id(resource.get("albumId"))
        if resource_album_id != album_id:
            return None
        guid, title, size = resource.get("guid"), resource.get("title"), resource.get("size")
        locator = resource.get("downloadUrl") or resource.get("magnetUrl")
        seeders, indexer = resource.get("seeders"), resource.get("indexer")
        score, formats = resource.get("customFormatScore", 0), resource.get("customFormats", [])
        if (
            not isinstance(guid, str)
            or not guid
            or not isinstance(title, str)
            or not title
            or not isinstance(locator, str)
            or not locator
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or seeders is not None and (isinstance(seeders, bool) or not isinstance(seeders, int) or seeders < 0)
            or indexer is not None and (not isinstance(indexer, str) or not indexer)
            or isinstance(score, bool)
            or not isinstance(score, int)
            or not isinstance(formats, list)
        ):
            return None
        names: list[str] = []
        for format_ in formats:
            if not isinstance(format_, dict) or not isinstance(format_.get("name"), str) or not format_["name"]:
                return None
            names.append(format_["name"])
        info_hash = resource.get("infoHash")
        canonical_hash = (
            info_hash.lower()
            if isinstance(info_hash, str)
            and len(info_hash) == 40
            and all(character in "0123456789abcdefABCDEF" for character in info_hash)
            else None
        )
        try:
            encoded = json.dumps(resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return LidarrRelease(
            resource,
            hashlib.sha256(encoded).hexdigest(),
            size,
            album_id,
            seeders,
            indexer,
            canonical_hash,
            score,
            tuple(names),
        )

    @classmethod
    def release_from_record(cls, resource: dict[str, object], album_id: str) -> LidarrRelease | None:
        return cls._materialize_release(resource, album_id)

    def album_releases(self, album_id: str) -> tuple[LidarrRelease, ...] | None:
        public_id = _public_id(int(album_id)) if isinstance(album_id, str) and album_id.isdecimal() else None
        if public_id != album_id:
            return None
        try:
            resources = self._get(f"release?{urlencode({'albumId': album_id})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(resources, list):
            return None
        releases: list[LidarrRelease] = []
        for resource in resources:
            if not isinstance(resource, dict):
                return None
            if resource.get("protocol") != "torrent":
                continue
            release = self._materialize_release(resource, album_id)
            if release is None:
                return None
            releases.append(release)
        return tuple(releases)

    @staticmethod
    def first_approved_release(
        releases: tuple[LidarrRelease, ...] | None,
        policy: ReconciliationPolicy,
        health_policy: MusicFencePolicy,
        *,
        current_score: int,
    ) -> LidarrRelease | None:
        if releases is None or isinstance(current_score, bool) or not isinstance(current_score, int):
            return None
        for release in releases:
            resource = release.resource
            if resource.get("approved") is not True or resource.get("downloadAllowed") is not True:
                continue
            formats = set(release.custom_format_names)
            if release.custom_format_score < policy.minimum_candidate_score:
                continue
            if release.custom_format_score - current_score < policy.minimum_score_gain:
                continue
            if not set(policy.required_candidate_formats).issubset(formats):
                continue
            if set(policy.forbidden_candidate_formats) & formats:
                continue
            if release.reported_seeders is not None:
                if release.reported_seeders < health_policy.minimum_reported_seeders:
                    continue
            elif (
                health_policy.unknown_seeders_policy != "probe_only"
                or release.indexer not in health_policy.probe_only_indexers
            ):
                continue
            return release
        return None

    def grab_release(self, release: LidarrRelease) -> bool:
        if not isinstance(release, LidarrRelease):
            return False
        try:
            body = json.dumps(release.resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if hashlib.sha256(body).hexdigest() != release.selector_fingerprint:
            return False
        request = Request(
            self._api("release"),
            data=body,
            headers={"X-Api-Key": self._resolve(self._api_key), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            status, _ = request_bytes(request, timeout=self._timeout)
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return status == 200

    def history_watermark(self) -> int | None:
        history = self._paged("history")
        if history is None:
            return None
        identifiers = [record.get("id") for record in history]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in identifiers):
            return None
        return max(identifiers, default=0)

    def observe_grab(self, album_id: str, release: LidarrRelease, *, watermark: int) -> ArrGrabObservation:
        if (
            not isinstance(release, LidarrRelease)
            or release.album_id != album_id
            or isinstance(watermark, bool)
            or not isinstance(watermark, int)
            or watermark < 0
        ):
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
            if (
                record.get("eventType") != "grabbed"
                or _public_id(record.get("albumId")) != album_id
                or record.get("sourceTitle") != title
            ):
                continue
            if (
                isinstance(record_id, bool)
                or not isinstance(record_id, int)
                or record_id <= watermark
                or not isinstance(download_id, str)
                or not download_id
            ):
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
            record
            for record in queue
            if _public_id(record.get("albumId")) == album_id
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
            if release.canonical_hash is not None and torrent_hash != release.canonical_hash:
                return ArrGrabObservation("unknown")
            return ArrGrabObservation("observed", download_id, torrent_hash)
        return ArrGrabObservation("absent" if not queue_matches else "ambiguous")

    def stopped_qbittorrent_client_name(self, category: str, download_client_id: int) -> str | None:
        if (
            not isinstance(category, str)
            or not category
            or isinstance(download_client_id, bool)
            or not isinstance(download_client_id, int)
            or download_client_id <= 0
        ):
            return None
        try:
            clients = self._get("downloadclient")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(clients, list):
            return None
        selected_name: str | None = None
        seen_ids: set[int] = set()
        for client in clients:
            if not isinstance(client, dict):
                return None
            client_id = client.get("id")
            if (
                isinstance(client_id, bool)
                or not isinstance(client_id, int)
                or client_id <= 0
                or client_id in seen_ids
            ):
                return None
            seen_ids.add(client_id)
            if (
                client.get("enable") is not True
                or client.get("protocol") != "torrent"
                or client.get("implementation") != "QBittorrent"
            ):
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
        return selected_name

    def stopped_qbittorrent_client(self, category: str, download_client_id: int) -> bool:
        return self.stopped_qbittorrent_client_name(category, download_client_id) is not None
