"""Pure, fail-closed reconciliation intent state."""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Iterable, Mapping


_SOURCES = frozenset({"radarr", "sonarr"})


@dataclass(frozen=True)
class AttemptPolicy:
    cooldown_seconds: int
    max_attempts: int

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.cooldown_seconds, self.max_attempts)) or self.cooldown_seconds < 0 or self.max_attempts <= 0:
            raise ValueError("reconciliation policy is invalid")


@dataclass(frozen=True)
class SearchIntent:
    operation_id: str
    source: str
    entity_id: str
    force: bool
    checkpoint: str

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.operation_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("operation_id must be a canonical UUID") from exc
        if str(parsed) != self.operation_id:
            raise ValueError("operation_id must be a canonical UUID")
        if self.source not in _SOURCES:
            raise ValueError("source must be radarr or sonarr")
        if not self.entity_id.isdecimal() or str(int(self.entity_id)) != self.entity_id or int(self.entity_id) <= 0:
            raise ValueError("entity_id must be a positive public integer")
        if not isinstance(self.force, bool):
            raise ValueError("force must be boolean")
        if not isinstance(self.checkpoint, str) or not self.checkpoint or len(self.checkpoint) > 256 or any(character in self.checkpoint for character in "\x00/\\"):
            raise ValueError("checkpoint must be a bounded opaque value")


class ReconciliationState:
    def __init__(self) -> None:
        self._intents: dict[str, SearchIntent] = {}
        self._uncertain: set[tuple[str, str]] = set()
        self._observed_attempts: dict[tuple[str, str], list[int]] = {}
        self._intent_times: dict[str, int] = {}
        self._observed_times: dict[str, int] = {}

    def record_intent(self, intent: SearchIntent, *, now: int = 0) -> SearchIntent:
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("attempt timestamp is invalid")
        previous = self._intents.get(intent.operation_id)
        if previous is not None:
            if previous != intent:
                raise ValueError("operation_id conflicts with durable intent")
            return previous
        self._intents[intent.operation_id] = intent
        self._uncertain.add((intent.source, intent.entity_id))
        self._intent_times[intent.operation_id] = now
        return intent

    def intent(self, operation_id: str) -> SearchIntent:
        return self._intents[operation_id]

    def eligible(self, source: str, entity_id: str, policy: AttemptPolicy, *, now: int, force: bool = False) -> bool:
        key = (source, entity_id)
        if key in self._uncertain:
            return False
        attempts = self._observed_attempts.get(key, [])
        if force:
            return True
        if len(attempts) >= policy.max_attempts:
            return False
        return not attempts or now - attempts[-1] >= policy.cooldown_seconds

    def mark_observed(self, operation_id: str, *, completed: bool, now: int) -> None:
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("attempt timestamp is invalid")
        intent = self.intent(operation_id)
        key = (intent.source, intent.entity_id)
        self._uncertain.discard(key)
        if completed:
            self._observed_attempts.setdefault(key, []).append(now)
        self._observed_times[operation_id] = now

    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "operation_id": intent.operation_id,
                "source": intent.source,
                "entity_id": intent.entity_id,
                "force": intent.force,
                "checkpoint": intent.checkpoint,
                "intent_at": self._intent_times[intent.operation_id],
                "observed_at": self._observed_times.get(intent.operation_id),
            }
            for intent in self._intents.values()
        )

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, object]]) -> ReconciliationState:
        state = cls()
        expected = {"operation_id", "source", "entity_id", "force", "checkpoint", "intent_at", "observed_at"}
        for record in records:
            if set(record) != expected:
                raise ValueError("durable reconciliation record has unknown fields")
            try:
                intent = SearchIntent(
                    record["operation_id"], record["source"], record["entity_id"], record["force"], record["checkpoint"]
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("durable reconciliation record is invalid") from exc
            intent_at = record["intent_at"]
            observed_at = record["observed_at"]
            if isinstance(intent_at, bool) or not isinstance(intent_at, int) or intent_at < 0 or (observed_at is not None and (isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < intent_at)):
                raise ValueError("durable reconciliation record is invalid")
            state.record_intent(intent, now=intent_at)
            if observed_at is not None:
                state.mark_observed(intent.operation_id, completed=True, now=observed_at)
        return state
