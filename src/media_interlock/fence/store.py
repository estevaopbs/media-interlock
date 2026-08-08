"""Fence-private durable reservation state."""

from __future__ import annotations

import json
from pathlib import Path

from .._infra.state import SqliteStore
from ..contracts import ContractError
from .model import FencePolicy, FenceState


class FenceStore:
    _KEY = "fence.reservations.v1"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @classmethod
    def open(cls, state_dir: Path) -> "FenceStore":
        return cls(SqliteStore.open(state_dir, "fence"))

    def load(self, policy: FencePolicy) -> FenceState:
        raw = self._store.get(self._KEY)
        if raw is None:
            return FenceState(policy)
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("durable Fence state is not valid JSON") from exc
        if not isinstance(records, list):
            raise ContractError("durable Fence state is not a reservation list")
        return FenceState.from_records(policy, records)

    def save(self, state: FenceState) -> None:
        self._store.put(self._KEY, json.dumps(state.records(), sort_keys=True, separators=(",", ":"), allow_nan=False))

    def close(self) -> None:
        self._store.close()
