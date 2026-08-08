"""Reconciler-private durable intent state."""

from __future__ import annotations

import json
from pathlib import Path

from .._infra.state import SqliteStore
from .model import ReconciliationState


class ReconcilerStore:
    _KEY = "reconciler.intents.v1"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @classmethod
    def open(cls, state_dir: Path) -> ReconcilerStore:
        return cls(SqliteStore.open(state_dir, "reconciler"))

    def load(self) -> ReconciliationState:
        raw = self._store.get(self._KEY)
        if raw is None:
            return ReconciliationState()
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("durable Reconciler state is not valid JSON") from exc
        if not isinstance(records, list):
            raise ValueError("durable Reconciler state is not a record list")
        return ReconciliationState.from_records(records)

    def save(self, state: ReconciliationState) -> None:
        self._store.put(self._KEY, json.dumps(state.records(), sort_keys=True, separators=(",", ":"), allow_nan=False))

    def close(self) -> None:
        self._store.close()
