"""Publisher-private durable publication state."""

from __future__ import annotations

import json
from pathlib import Path

from .._infra.state import SqliteStore
from ..contracts import ContractError
from .model import PublisherState


class PublisherStore:
    _KEY = "publisher.publications.v3"
    _IMPORT_CURSORS_KEY = "publisher.import-cursors.v1"
    _LEGACY_KEYS = ("publisher.publications.v1", "publisher.publications.v2")

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @classmethod
    def open(cls, state_dir: Path) -> "PublisherStore":
        return cls(SqliteStore.open(state_dir, "publisher"))

    @classmethod
    def from_store(cls, store: SqliteStore) -> "PublisherStore":
        return cls(store)

    @property
    def store(self) -> SqliteStore:
        return self._store

    def load(self) -> PublisherState:
        raw = self._store.get(self._KEY)
        if raw is None:
            if any(self._store.get(key) is not None for key in self._LEGACY_KEYS):
                raise ContractError("durable Publisher state requires an explicit migration")
            return PublisherState()
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("durable Publisher state is not valid JSON") from exc
        if not isinstance(records, list):
            raise ContractError("durable Publisher state is not a publication list")
        return PublisherState.from_records(records)

    def save(self, state: PublisherState) -> None:
        self._store.put(self._KEY, json.dumps(state.records(), sort_keys=True, separators=(",", ":"), allow_nan=False))

    def load_import_cursors(self) -> dict[str, int]:
        raw = self._store.get(self._IMPORT_CURSORS_KEY)
        if raw is None:
            return {}
        try:
            cursors = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("durable Publisher import cursors are not valid JSON") from exc
        if not isinstance(cursors, dict) or any(
            name not in {"radarr", "sonarr"}
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for name, value in cursors.items()
        ):
            raise ContractError("durable Publisher import cursors are invalid")
        return {str(name): int(value) for name, value in cursors.items()}

    def save_import_cursor(self, source: str, cursor: int) -> None:
        if source not in {"radarr", "sonarr"} or isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ContractError("Publisher import cursor is invalid")
        cursors = self.load_import_cursors()
        previous = cursors.get(source, 0)
        if cursor < previous:
            raise ContractError("Publisher import cursor cannot move backwards")
        cursors[source] = cursor
        self._store.put(self._IMPORT_CURSORS_KEY, json.dumps(cursors, sort_keys=True, separators=(",", ":")))

    def close(self) -> None:
        self._store.close()
