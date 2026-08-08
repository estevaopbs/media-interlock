"""Pure, fail-closed reconciliation intent state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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


@dataclass(frozen=True)
class GrabIntent:
    """The immutable Arr decision recorded before Fence or Arr effects."""

    operation_id: str
    source: str
    entity_id: str
    selector_fingerprint: str
    expected_bytes: int
    watermark: int
    release_resource: Mapping[str, object]

    def __post_init__(self) -> None:
        SearchIntent(self.operation_id, self.source, self.entity_id, False, "grab")
        if not isinstance(self.selector_fingerprint, str) or len(self.selector_fingerprint) != 64 or any(character not in "0123456789abcdef" for character in self.selector_fingerprint):
            raise ValueError("release selector fingerprint is invalid")
        if isinstance(self.expected_bytes, bool) or not isinstance(self.expected_bytes, int) or self.expected_bytes <= 0:
            raise ValueError("expected release size is invalid")
        if isinstance(self.watermark, bool) or not isinstance(self.watermark, int) or self.watermark < 0:
            raise ValueError("history watermark is invalid")
        if not isinstance(self.release_resource, Mapping):
            raise ValueError("release resource is invalid")
        try:
            normalized = json.loads(json.dumps(dict(self.release_resource), sort_keys=True, separators=(",", ":"), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("release resource is invalid") from exc
        if not isinstance(normalized, dict) or normalized.get("approved") is not True or normalized.get("protocol") != "torrent" or not isinstance(normalized.get("guid"), str) or not normalized["guid"] or not isinstance(normalized.get("title"), str) or not normalized["title"] or isinstance(normalized.get("size"), bool) or normalized.get("size") != self.expected_bytes:
            raise ValueError("release resource is invalid")
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(encoded).hexdigest() != self.selector_fingerprint:
            raise ValueError("release selector fingerprint is invalid")
        object.__setattr__(self, "release_resource", normalized)


class ReconciliationState:
    def __init__(self) -> None:
        self._intents: dict[str, SearchIntent] = {}
        self._grab_intents: dict[str, GrabIntent] = {}
        self._grab_attempted: set[str] = set()
        self._uncertain: set[tuple[str, str]] = set()
        self._observed_attempts: dict[tuple[str, str], list[int]] = {}
        self._intent_times: dict[str, int] = {}
        self._observed_times: dict[str, int] = {}
        self._completed: dict[str, bool] = {}

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

    def record_grab_intent(self, intent: GrabIntent) -> GrabIntent:
        search = self.intent(intent.operation_id)
        if (search.source, search.entity_id) != (intent.source, intent.entity_id):
            raise ValueError("grab intent conflicts with search intent")
        previous = self._grab_intents.get(intent.operation_id)
        if previous is not None:
            if previous != intent:
                raise ValueError("operation_id conflicts with durable grab intent")
            return previous
        self._grab_intents[intent.operation_id] = intent
        return intent

    def grab_intent(self, operation_id: str) -> GrabIntent:
        return self._grab_intents[operation_id]

    def mark_grab_attempted(self, operation_id: str) -> None:
        if operation_id not in self._grab_intents:
            raise ValueError("grab effect has no durable intent")
        self._grab_attempted.add(operation_id)

    def grab_attempted(self, operation_id: str) -> bool:
        return operation_id in self._grab_attempted

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
        self._completed[operation_id] = completed

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
                "completed": self._completed.get(intent.operation_id),
                "grab": None if intent.operation_id not in self._grab_intents else {
                    "selector_fingerprint": self._grab_intents[intent.operation_id].selector_fingerprint,
                    "expected_bytes": self._grab_intents[intent.operation_id].expected_bytes,
                    "watermark": self._grab_intents[intent.operation_id].watermark,
                    "release_resource": dict(self._grab_intents[intent.operation_id].release_resource),
                },
                "grab_attempted": intent.operation_id in self._grab_attempted,
            }
            for intent in self._intents.values()
        )

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, object]]) -> ReconciliationState:
        state = cls()
        expected = {"operation_id", "source", "entity_id", "force", "checkpoint", "intent_at", "observed_at", "completed", "grab", "grab_attempted"}
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
            completed = record["completed"]
            if isinstance(intent_at, bool) or not isinstance(intent_at, int) or intent_at < 0 or (observed_at is not None and (isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < intent_at)) or (observed_at is None and completed is not None) or (observed_at is not None and not isinstance(completed, bool)):
                raise ValueError("durable reconciliation record is invalid")
            state.record_intent(intent, now=intent_at)
            grab = record["grab"]
            grab_attempted = record["grab_attempted"]
            if not isinstance(grab_attempted, bool) or (grab is None and grab_attempted):
                raise ValueError("durable reconciliation record is invalid")
            if grab is not None:
                if not isinstance(grab, Mapping) or set(grab) != {"selector_fingerprint", "expected_bytes", "watermark", "release_resource"}:
                    raise ValueError("durable reconciliation record is invalid")
                try:
                    state.record_grab_intent(GrabIntent(intent.operation_id, intent.source, intent.entity_id, grab["selector_fingerprint"], grab["expected_bytes"], grab["watermark"], grab["release_resource"]))
                except (TypeError, ValueError) as exc:
                    raise ValueError("durable reconciliation record is invalid") from exc
                if grab_attempted:
                    state.mark_grab_attempted(intent.operation_id)
            if observed_at is not None:
                state.mark_observed(intent.operation_id, completed=completed, now=observed_at)
        return state
