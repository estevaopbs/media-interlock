"""Fail-closed qBittorrent WebUI adapter for Fence-owned transfers."""

from __future__ import annotations

import http.cookiejar
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ..config import SecretReference
from ..fence.model import QbittorrentActivityObservation, QbittorrentHealthObservation, QbittorrentObservation
from ._http import MAX_RESPONSE_BYTES, _NoRedirect


def _torrent_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _fence_tags(value: object) -> set[str] | None:
    if not isinstance(value, str):
        return None
    tags = {tag.strip() for tag in value.split(",") if tag.strip()}
    return {tag for tag in tags if tag.startswith("fence:")}


class QbittorrentAdapter:
    """Controls only tagged work in one configured category and staging root."""

    def __init__(self, base_url: str, username: SecretReference | None, password: SecretReference | None, *, api_key: SecretReference | None = None, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._api_key = api_key
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds
        self._opener = build_opener(_NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._authenticated = False

    def _headers(self, *, content_type: bool = False) -> dict[str, str]:
        headers = {"Referer": self._base_url}
        if content_type:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._resolve(self._api_key)}"
        return headers

    def _invalidate_authentication(self) -> None:
        self._authenticated = False
        self._opener = build_opener(_NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    @staticmethod
    def _read_bounded(response: Any) -> bytes:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("qBittorrent response exceeds bound")
        return body

    def _post(self, path: str, fields: dict[str, str]) -> bytes:
        request = Request(f"{self._base_url}{path}", data=urlencode(fields).encode("utf-8"), headers=self._headers(content_type=True), method="POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RuntimeError("unexpected qBittorrent status")
                return self._read_bounded(response)
        except HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                self._invalidate_authentication()
            raise

    def _get_json(self, path: str) -> Any:
        request = Request(f"{self._base_url}{path}", headers=self._headers())
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RuntimeError("unexpected qBittorrent status")
                return json.loads(self._read_bounded(response).decode("utf-8"))
        except HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                self._invalidate_authentication()
            raise

    def _get_text(self, path: str) -> str:
        request = Request(f"{self._base_url}{path}", headers=self._headers())
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RuntimeError("unexpected qBittorrent status")
                return self._read_bounded(response).decode("utf-8").strip()
        except HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                self._invalidate_authentication()
            raise

    def _login(self) -> bool:
        if self._api_key is not None:
            try:
                return bool(self._resolve(self._api_key))
            except (OSError, RuntimeError, ValueError):
                return False
        if self._authenticated:
            return True
        if self._username is None or self._password is None:
            return False
        try:
            result = self._post("/api/v2/auth/login", {"username": self._resolve(self._username), "password": self._resolve(self._password)})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        self._authenticated = result == b"Ok."
        return self._authenticated

    def ready(self) -> bool:
        for attempt in range(2):
            if not self._login():
                return False
            try:
                version = self._get_text("/api/v2/app/webapiVersion")
                application_version = self._get_text("/api/v2/app/version")
                version_parts = tuple(int(part) for part in version.split("."))
                match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", application_version)
            except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                if attempt == 0 and not self._authenticated:
                    continue
                return False
            return match is not None and int(match.group(1)) == 5 and version_parts >= (2, 11)
        return False

    def observe_existing_stopped(self, torrent_hash: str, category: str, *, save_path: Path, _reservation_id: str | None = None) -> QbittorrentObservation:
        """Observe an Arr-added torrent before Fence mutates its tag or state."""
        expected_save_path = str(save_path) if isinstance(save_path, Path) else ""
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(category, str) or not category or not expected_save_path:
            return QbittorrentObservation("unknown")
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash, 'category': category})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return QbittorrentObservation("unknown")
        if not isinstance(torrents, list):
            return QbittorrentObservation("unknown")
        if not torrents:
            return QbittorrentObservation("absent")
        if len(torrents) != 1 or not isinstance(torrents[0], dict):
            return QbittorrentObservation("ambiguous")
        torrent = torrents[0]
        size, state, remaining = torrent.get("size"), torrent.get("state"), torrent.get("amount_left")
        owner_tags = _fence_tags(torrent.get("tags"))
        expected_owner_tags = set() if _reservation_id is None else {_reservation_id}
        if torrent.get("hash") != torrent_hash or torrent.get("category") != category or torrent.get("save_path") != expected_save_path or owner_tags != expected_owner_tags or isinstance(size, bool) or not isinstance(size, int) or size < 0 or (remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int) or not 0 <= remaining <= size)) or not isinstance(state, str):
            return QbittorrentObservation("unknown")
        if not (state.startswith("paused") or state.startswith("stopped")):
            return QbittorrentObservation("unknown")
        if size == 0:
            magnet_uri = torrent.get("magnet_uri")
            return QbittorrentObservation("metadata_pending") if isinstance(magnet_uri, str) and magnet_uri.startswith("magnet:?") else QbittorrentObservation("unknown")
        return QbittorrentObservation("observed", size, remaining)

    def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool:
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(reservation_id, str) or not reservation_id:
            return False
        try:
            self._post("/api/v2/torrents/addTags", {"hashes": torrent_hash, "tags": reservation_id})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str, *, save_path: Path) -> QbittorrentObservation:
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(category, str) or not category or not isinstance(reservation_id, str) or not reservation_id:
            return QbittorrentObservation("unknown")
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash, 'category': category, 'tag': reservation_id})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return QbittorrentObservation("unknown")
        if not isinstance(torrents, list):
            return QbittorrentObservation("unknown")
        if not torrents:
            return QbittorrentObservation("absent")
        if len(torrents) != 1 or not isinstance(torrents[0], dict):
            return QbittorrentObservation("ambiguous")
        torrent = torrents[0]
        if _fence_tags(torrent.get("tags")) != {reservation_id}:
            return QbittorrentObservation("unknown")
        return self.observe_existing_stopped(torrent_hash, category, save_path=save_path, _reservation_id=reservation_id)

    def resume(self, torrent_hash: str) -> bool:
        if not self._login() or not _torrent_hash(torrent_hash):
            return False
        try:
            self._post("/api/v2/torrents/start", {"hashes": torrent_hash})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def pause(self, torrent_hash: str) -> bool:
        if not self._login() or not _torrent_hash(torrent_hash):
            return False
        try:
            self._post("/api/v2/torrents/stop", {"hashes": torrent_hash})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def observe_active(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation:
        expected_save_path = str(save_path) if isinstance(save_path, Path) else ""
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(category, str) or not category or not expected_save_path:
            return QbittorrentActivityObservation("unknown")
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return QbittorrentActivityObservation("unknown")
        if not isinstance(torrents, list):
            return QbittorrentActivityObservation("unknown")
        if not torrents:
            return QbittorrentActivityObservation("absent")
        if len(torrents) != 1 or not isinstance(torrents[0], dict):
            return QbittorrentActivityObservation("ambiguous")
        torrent = torrents[0]
        if torrent.get("hash") != torrent_hash or torrent.get("category") != category or torrent.get("save_path") != expected_save_path or _fence_tags(torrent.get("tags")) != {reservation_id}:
            return QbittorrentActivityObservation("unknown")
        state, size = torrent.get("state"), torrent.get("size")
        if not isinstance(state, str) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return QbittorrentActivityObservation("unknown")
        if state in {"downloading", "stalledDL", "queuedDL", "forcedDL", "metaDL", "forcedMetaDL", "uploading", "stalledUP", "queuedUP", "forcedUP"}:
            return QbittorrentActivityObservation("observed", True, size or None)
        if state.startswith("paused") or state.startswith("stopped"):
            return QbittorrentActivityObservation("observed", False, size or None)
        return QbittorrentActivityObservation("unknown")

    def observe_candidate_health(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentHealthObservation:
        expected_save_path = str(save_path) if isinstance(save_path, Path) else ""
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(reservation_id, str) or not reservation_id or not isinstance(category, str) or not category or not expected_save_path:
            return QbittorrentHealthObservation("unknown")
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return QbittorrentHealthObservation("unknown")
        if not isinstance(torrents, list):
            return QbittorrentHealthObservation("unknown")
        if not torrents:
            return QbittorrentHealthObservation("absent")
        if len(torrents) != 1 or not isinstance(torrents[0], dict):
            return QbittorrentHealthObservation("ambiguous")
        torrent = torrents[0]
        if torrent.get("hash") != torrent_hash or torrent.get("category") != category or torrent.get("save_path") != expected_save_path or _fence_tags(torrent.get("tags")) != {reservation_id}:
            return QbittorrentHealthObservation("unknown")
        size, total_size, downloaded = torrent.get("size"), torrent.get("total_size"), torrent.get("downloaded")
        availability, seeds, leeches = torrent.get("availability"), torrent.get("num_seeds"), torrent.get("num_leechs")
        if any(isinstance(value, bool) for value in (size, total_size, downloaded, availability, seeds, leeches)) or not isinstance(size, int) or not isinstance(total_size, int) or not isinstance(downloaded, int) or downloaded < 0 or not isinstance(availability, (int, float)) or availability < 0 or not isinstance(seeds, int) or not isinstance(leeches, int) or seeds < 0 or leeches < 0:
            return QbittorrentHealthObservation("unknown")
        return QbittorrentHealthObservation("observed", metadata_known=size > 0 and total_size > 0, downloaded_bytes=downloaded, availability=float(availability), peers=seeds + leeches)

    def delete_owned_incomplete(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path, delete_files: bool) -> bool:
        """Delete one exact Fence-owned transfer; never operate on a path."""
        if not isinstance(delete_files, bool):
            return False
        observed = self.observe_candidate_health(torrent_hash, reservation_id, category, save_path=save_path)
        if observed.kind != "observed":
            return False
        try:
            self._post("/api/v2/torrents/delete", {"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def terminal_observed(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation:
        """Accept only an exact, fully downloaded fenced torrent as terminal."""
        expected_save_path = str(save_path) if isinstance(save_path, Path) else ""
        if not self._login() or not _torrent_hash(torrent_hash) or not isinstance(category, str) or not category or not expected_save_path:
            return QbittorrentActivityObservation("unknown")
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return QbittorrentActivityObservation("unknown")
        if not isinstance(torrents, list):
            return QbittorrentActivityObservation("unknown")
        if not torrents:
            return QbittorrentActivityObservation("absent")
        if len(torrents) != 1 or not isinstance(torrents[0], dict):
            return QbittorrentActivityObservation("ambiguous")
        torrent = torrents[0]
        if torrent.get("hash") != torrent_hash or torrent.get("category") != category or torrent.get("save_path") != expected_save_path or _fence_tags(torrent.get("tags")) != {reservation_id}:
            return QbittorrentActivityObservation("unknown")
        progress, state = torrent.get("progress"), torrent.get("state")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or progress != 1 or not isinstance(state, str):
            return QbittorrentActivityObservation("unknown")
        return QbittorrentActivityObservation("observed", state in {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP"})
