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
from ._http import MAX_RESPONSE_BYTES, _NoRedirect


def _torrent_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


class QbittorrentAdapter:
    """Controls only tagged work in one configured category and staging root."""

    def __init__(self, base_url: str, username: SecretReference, password: SecretReference, *, staging_root: Path, category: str, secret_resolver: Callable[[SecretReference], str] | None = None, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._staging_root = str(staging_root)
        self._category = category
        self._resolve = secret_resolver or (lambda reference: reference.resolve())
        self._timeout = timeout_seconds
        self._opener = build_opener(_NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._authenticated = False

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
        request = Request(f"{self._base_url}{path}", data=urlencode(fields).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": self._base_url}, method="POST")
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
        request = Request(f"{self._base_url}{path}", headers={"Referer": self._base_url})
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
        request = Request(f"{self._base_url}{path}", headers={"Referer": self._base_url})
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
        if self._authenticated:
            return True
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
                preferences = self._get_json("/api/v2/app/preferences")
                version_parts = tuple(int(part) for part in version.split("."))
                match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", application_version)
            except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                if attempt == 0 and not self._authenticated:
                    continue
                return False
            return match is not None and int(match.group(1)) == 5 and version_parts >= (2, 11) and isinstance(preferences, dict) and preferences.get("start_paused_enabled") is True
        return False

    def _tagged(self, reservation_id: str) -> dict[str, Any] | None:
        if not self._login() or not reservation_id:
            return None
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'tag': reservation_id, 'category': self._category})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(torrents, list) or len(torrents) != 1 or not isinstance(torrents[0], dict):
            return None
        torrent = torrents[0]
        tags, category, save_path, torrent_hash = (torrent.get(key) for key in ("tags", "category", "save_path", "hash"))
        if not all(isinstance(value, str) and value for value in (tags, category, save_path)) or not _torrent_hash(torrent_hash):
            return None
        if reservation_id not in {tag.strip() for tag in tags.split(",")} or category != self._category or save_path != self._staging_root:
            return None
        return torrent

    def observe_stopped(self, reservation_id: str) -> tuple[str, int] | None:
        torrent = self._tagged(reservation_id)
        if torrent is None:
            return None
        state = torrent.get("state")
        size = torrent.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return None
        return (torrent["hash"], size) if isinstance(state, str) and (state.startswith("paused") or state.startswith("stopped")) else None

    def add_stopped(self, source: str, reservation_id: str) -> tuple[str, int] | None:
        if not source or not reservation_id or not self.ready():
            return None
        try:
            self._post("/api/v2/torrents/add", {"urls": source, "paused": "true", "tags": reservation_id, "category": self._category, "savepath": self._staging_root})
        except (HTTPError, URLError, OSError, RuntimeError):
            return None
        return self.observe_stopped(reservation_id)

    def resume(self, torrent_hash: str) -> bool:
        if not self._login() or not _torrent_hash(torrent_hash):
            return False
        try:
            self._post("/api/v2/torrents/start", {"hashes": torrent_hash})
        except (HTTPError, URLError, OSError, RuntimeError):
            return False
        return True

    def observe_active(self, torrent_hash: str, reservation_id: str) -> bool | None:
        if not self._login() or not _torrent_hash(torrent_hash):
            return None
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(torrents, list) or len(torrents) != 1 or not isinstance(torrents[0], dict):
            return None
        torrent = torrents[0]
        tags = torrent.get("tags")
        if torrent.get("hash") != torrent_hash or torrent.get("category") != self._category or torrent.get("save_path") != self._staging_root or not isinstance(tags, str) or reservation_id not in {tag.strip() for tag in tags.split(",")}:
            return None
        state = torrent.get("state")
        if not isinstance(state, str):
            return None
        if state in {"downloading", "stalledDL", "queuedDL", "forcedDL", "metaDL", "forcedMetaDL", "uploading", "stalledUP", "queuedUP", "forcedUP"}:
            return True
        if state.startswith("paused") or state.startswith("stopped"):
            return False
        return None

    def terminal_observed(self, torrent_hash: str, reservation_id: str) -> bool | None:
        """Accept only an exact, fully downloaded fenced torrent as terminal."""
        if not self._login() or not _torrent_hash(torrent_hash):
            return None
        try:
            torrents = self._get_json(f"/api/v2/torrents/info?{urlencode({'hashes': torrent_hash})}")
        except (HTTPError, URLError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(torrents, list) or len(torrents) != 1 or not isinstance(torrents[0], dict):
            return None
        torrent = torrents[0]
        tags = torrent.get("tags")
        if torrent.get("hash") != torrent_hash or torrent.get("category") != self._category or torrent.get("save_path") != self._staging_root or not isinstance(tags, str) or reservation_id not in {tag.strip() for tag in tags.split(",")}:
            return None
        progress, state = torrent.get("progress"), torrent.get("state")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or progress != 1 or not isinstance(state, str):
            return None
        return state in {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP"}
