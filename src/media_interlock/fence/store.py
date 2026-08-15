"""Fence-private durable reservation state."""

from __future__ import annotations

import json
from pathlib import Path

from .._infra.state import SqliteStore
from ..contracts import ContractError
from .model import FencePolicy, FenceState


class FenceStore:
    _KEY = "fence.reservations.v2"
    _LEGACY_KEY = "fence.reservations.v1"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @classmethod
    def open(cls, state_dir: Path) -> "FenceStore":
        return cls(SqliteStore.open(state_dir, "fence"))

    @classmethod
    def from_store(cls, store: SqliteStore) -> "FenceStore":
        return cls(store)

    @property
    def store(self) -> SqliteStore:
        return self._store

    def load(self, policy: FencePolicy) -> FenceState:
        raw = self._store.get(self._KEY)
        if raw is None:
            if self._store.get(self._LEGACY_KEY) is not None:
                raise ContractError("durable Fence v1 state requires an explicit migration")
            return FenceState(policy)
        try:
            snapshot = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("durable Fence state is not valid JSON") from exc
        allowed = {"reservations", "watermarks", "quiescing", "post_pnr_adoptions", "post_pnr_historical_adoptions", "post_pnr_historical_activations"}
        required = {"reservations", "watermarks", "quiescing"}
        if not isinstance(snapshot, dict) or not required <= set(snapshot) <= allowed or not isinstance(snapshot["reservations"], list) or not isinstance(snapshot["watermarks"], dict) or not isinstance(snapshot["quiescing"], bool) or ("post_pnr_adoptions" in snapshot and not isinstance(snapshot["post_pnr_adoptions"], list)) or ("post_pnr_historical_adoptions" in snapshot and not isinstance(snapshot["post_pnr_historical_adoptions"], list)) or ("post_pnr_historical_activations" in snapshot and not isinstance(snapshot["post_pnr_historical_activations"], list)):
            raise ContractError("durable Fence state is not a v2 snapshot")
        return FenceState.from_snapshot(policy, snapshot["reservations"], snapshot["watermarks"], quiescing=snapshot["quiescing"], post_pnr_adoptions=snapshot.get("post_pnr_adoptions", []), post_pnr_historical_adoptions=snapshot.get("post_pnr_historical_adoptions", []), post_pnr_historical_activations=snapshot.get("post_pnr_historical_activations", []))

    def save(self, state: FenceState) -> None:
        snapshot = {"reservations": state.records(), "watermarks": state.watermarks(), "quiescing": state.quiescing, "post_pnr_adoptions": state.post_pnr_records(), "post_pnr_historical_adoptions": state.post_pnr_historical_records(), "post_pnr_historical_activations": state.post_pnr_historical_activation_records()}
        self._store.put(self._KEY, json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False))

    def close(self) -> None:
        self._store.close()
