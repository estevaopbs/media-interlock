"""Publisher-private durable publication state."""

from __future__ import annotations

import json
from pathlib import Path

from .._infra.state import SqliteStore
from ..contracts import ContractError
from .model import PublisherState


class PublisherStore:
    _KEY = "publisher.publications.v3"
    _LEGACY_KEYS = ("publisher.publications.v1", "publisher.publications.v2")

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @classmethod
    def open(cls, state_dir: Path) -> "PublisherStore":
        return cls(SqliteStore.open(state_dir, "publisher"))

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

    def close(self) -> None:
        self._store.close()
