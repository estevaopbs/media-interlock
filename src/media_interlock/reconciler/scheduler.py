"""Pure configurable scheduling for automatic Arr upgrade searches."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Callable, Mapping, Protocol
import uuid

from ..config import ReconciliationPolicy
from .model import ReconciliationState, SearchIntent

if TYPE_CHECKING:
    from ..adapters.arr import ArrRelease, ArrReleaseSearch


DAY = 86_400


@dataclass(frozen=True)
class UpgradeEntity:
    source: str
    entity_id: str
    released_at: int
    generation: str
    current_score: int

    def __post_init__(self) -> None:
        if self.source not in {"radarr", "sonarr"}:
            raise ValueError("upgrade entity source is invalid")
        if not self.entity_id.isdecimal() or int(self.entity_id) <= 0:
            raise ValueError("upgrade entity id is invalid")
        if isinstance(self.released_at, bool) or not isinstance(self.released_at, int) or self.released_at < 0:
            raise ValueError("upgrade entity release time is invalid")
        if not isinstance(self.generation, str) or not self.generation or len(self.generation) > 128:
            raise ValueError("upgrade entity generation is invalid")
        if isinstance(self.current_score, bool) or not isinstance(self.current_score, int):
            raise ValueError("upgrade entity score is invalid")


@dataclass(frozen=True)
class ScheduleCheckpoint:
    generation: str
    last_completed_at: int
    attempts: int
    terminal: bool


def cooldown_for_age(policy: ReconciliationPolicy, age_seconds: int) -> int:
    """Return the configured geometric cooldown for one entity age."""
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, int) or age_seconds < 0:
        raise ValueError("entity age is invalid")
    if policy.cooldown_seconds == 0:
        return 0
    step_seconds = policy.cooldown_step_days * DAY
    exponent = age_seconds // step_seconds
    try:
        cooldown = math.ceil(policy.cooldown_seconds * (policy.cooldown_multiplier ** exponent))
    except OverflowError:
        # A cooldown beyond the terminal horizon is observationally equivalent
        # to any larger value and avoids unbounded numeric growth.
        cooldown = policy.terminal_horizon_days * DAY + 1
    if policy.maximum_cooldown_seconds:
        cooldown = min(cooldown, policy.maximum_cooldown_seconds)
    return cooldown


class ScheduleState:
    def __init__(
        self,
        checkpoints: Mapping[tuple[str, str], ScheduleCheckpoint] | None = None,
        budget_events: tuple[int, ...] = (),
        recorded_operations: tuple[str, ...] = (),
    ) -> None:
        self._checkpoints = dict(checkpoints or {})
        self._budget_events = list(budget_events)
        self._recorded_operations = set(recorded_operations)

    def next_search_at(
        self,
        entity: UpgradeEntity,
        policy: ReconciliationPolicy,
        now: int,
    ) -> int | None:
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("schedule clock is invalid")
        minimum_at = entity.released_at + policy.minimum_age_days * DAY
        if now < minimum_at:
            return minimum_at
        key = (entity.source, entity.entity_id)
        checkpoint = self._checkpoints.get(key)
        same_generation = checkpoint is not None and checkpoint.generation == entity.generation
        terminal_at = entity.released_at + policy.terminal_horizon_days * DAY
        if now >= terminal_at:
            if not policy.final_search:
                return None
            if same_generation and checkpoint.terminal:
                return None
            return now
        if same_generation and checkpoint.attempts >= policy.max_attempts:
            return None
        if not same_generation:
            return now
        assert checkpoint is not None
        age_seconds = max(0, now - entity.released_at)
        return checkpoint.last_completed_at + cooldown_for_age(policy, age_seconds)

    def due(self, entity: UpgradeEntity, policy: ReconciliationPolicy, now: int) -> bool:
        due_at = self.next_search_at(entity, policy, now)
        return due_at is not None and due_at <= now

    def record_completed(self, entity: UpgradeEntity, *, now: int, terminal: bool, operation_id: str | None = None) -> None:
        key = (entity.source, entity.entity_id)
        previous = self._checkpoints.get(key)
        attempts = previous.attempts + 1 if previous is not None and previous.generation == entity.generation else 1
        self._checkpoints[key] = ScheduleCheckpoint(entity.generation, now, attempts, terminal)
        if operation_id is not None:
            self._recorded_operations.add(operation_id)

    def operation_recorded(self, operation_id: str) -> bool:
        return operation_id in self._recorded_operations

    def record_budget_event(self, now: int) -> None:
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("budget event time is invalid")
        self._budget_events.append(now)
        cutoff = now - DAY
        self._budget_events = [event for event in self._budget_events if event > cutoff]

    def within_budget(self, policy: ReconciliationPolicy, *, now: int) -> bool:
        hour = sum(event > now - 3_600 for event in self._budget_events)
        day = sum(event > now - DAY for event in self._budget_events)
        return hour < policy.max_searches_per_hour and day < policy.max_searches_per_day

    def record(self) -> dict[str, object]:
        return {
            "checkpoints": [
                {
                    "source": source,
                    "entity_id": entity_id,
                    "generation": checkpoint.generation,
                    "last_completed_at": checkpoint.last_completed_at,
                    "attempts": checkpoint.attempts,
                    "terminal": checkpoint.terminal,
                }
                for (source, entity_id), checkpoint in sorted(self._checkpoints.items())
            ],
            "budget_events": list(self._budget_events),
            "recorded_operations": sorted(self._recorded_operations),
        }

    @classmethod
    def from_record(cls, record: object) -> "ScheduleState":
        if not isinstance(record, dict) or set(record) != {"checkpoints", "budget_events", "recorded_operations"}:
            raise ValueError("schedule state is invalid")
        raw_checkpoints, raw_events, raw_operations = record["checkpoints"], record["budget_events"], record["recorded_operations"]
        if not isinstance(raw_checkpoints, list) or not isinstance(raw_events, list) or not isinstance(raw_operations, list):
            raise ValueError("schedule state is invalid")
        checkpoints: dict[tuple[str, str], ScheduleCheckpoint] = {}
        for item in raw_checkpoints:
            if not isinstance(item, dict) or set(item) != {"source", "entity_id", "generation", "last_completed_at", "attempts", "terminal"}:
                raise ValueError("schedule checkpoint is invalid")
            entity = UpgradeEntity(item["source"], item["entity_id"], 0, item["generation"], 0)
            last, attempts, terminal = item["last_completed_at"], item["attempts"], item["terminal"]
            if isinstance(last, bool) or not isinstance(last, int) or last < 0 or isinstance(attempts, bool) or not isinstance(attempts, int) or attempts <= 0 or not isinstance(terminal, bool):
                raise ValueError("schedule checkpoint is invalid")
            key = (entity.source, entity.entity_id)
            if key in checkpoints:
                raise ValueError("schedule checkpoint is duplicated")
            checkpoints[key] = ScheduleCheckpoint(entity.generation, last, attempts, terminal)
        if any(isinstance(event, bool) or not isinstance(event, int) or event < 0 for event in raw_events):
            raise ValueError("schedule budget event is invalid")
        if any(not isinstance(operation, str) or not operation for operation in raw_operations) or len(set(raw_operations)) != len(raw_operations):
            raise ValueError("schedule operation registry is invalid")
        return cls(checkpoints, tuple(raw_events), tuple(raw_operations))


class SchedulerAdapter(Protocol):
    def upgrade_entities(self) -> tuple[UpgradeEntity, ...] | None: ...
    def approved_release(self, entity_id: str, policy: ReconciliationPolicy, *, current_score: int) -> "ArrReleaseSearch": ...


class SelectedReleaseExecutor(Protocol):
    def recover(self, *, now: int) -> list[str]: ...
    def execute_selected(self, intent: SearchIntent, release: "ArrRelease", *, now: int) -> str: ...


@dataclass(frozen=True)
class SchedulerRunResult:
    searched: int
    grabbed: int
    no_candidate: int
    unavailable: int
    pending: int


class UpgradeScheduler:
    """Inventory due Arr entities and execute bounded upgrade searches."""

    _CHECKPOINT_PREFIX = "auto-v1:"

    def __init__(
        self,
        schedule: ScheduleState,
        reconciliation: ReconciliationState,
        save_schedule: Callable[[ScheduleState], None],
        adapters: Mapping[str, SchedulerAdapter],
        policies: Mapping[str, ReconciliationPolicy],
        executor: SelectedReleaseExecutor,
    ) -> None:
        self._schedule = schedule
        self._reconciliation = reconciliation
        self._save_schedule = save_schedule
        self._adapters = adapters
        self._policies = policies
        self._executor = executor

    @classmethod
    def _checkpoint(cls, entity: UpgradeEntity, terminal: bool) -> str:
        return f"{cls._CHECKPOINT_PREFIX}{entity.generation}:{int(terminal)}"

    @classmethod
    def _parse_checkpoint(cls, intent: SearchIntent, released_at: int, current_score: int) -> tuple[UpgradeEntity, bool] | None:
        if not intent.checkpoint.startswith(cls._CHECKPOINT_PREFIX):
            return None
        parts = intent.checkpoint.removeprefix(cls._CHECKPOINT_PREFIX).split(":")
        if len(parts) != 2 or not parts[0] or parts[1] not in {"0", "1"}:
            return None
        return UpgradeEntity(intent.source, intent.entity_id, released_at, parts[0], current_score), parts[1] == "1"

    def _sync_completed(self, entities: Mapping[tuple[str, str], UpgradeEntity], now: int) -> None:
        for intent in self._reconciliation.intents():
            if not self._reconciliation.observed(intent.operation_id) or self._schedule.operation_recorded(intent.operation_id):
                continue
            current = entities.get((intent.source, intent.entity_id))
            parsed = self._parse_checkpoint(
                intent,
                current.released_at if current is not None else 0,
                current.current_score if current is not None else 0,
            )
            if parsed is None:
                continue
            entity, terminal = parsed
            self._schedule.record_completed(entity, now=now, terminal=terminal, operation_id=intent.operation_id)
            self._save_schedule(self._schedule)

    def run(self, *, now: int) -> SchedulerRunResult:
        self._executor.recover(now=now)
        inventories: dict[str, tuple[UpgradeEntity, ...]] = {}
        unavailable = 0
        for source, adapter in self._adapters.items():
            entities = adapter.upgrade_entities()
            if entities is None:
                unavailable += 1
                continue
            inventories[source] = entities
        current = {(entity.source, entity.entity_id): entity for entities in inventories.values() for entity in entities}
        self._sync_completed(current, now)
        pending_entities = {
            (intent.source, intent.entity_id)
            for intent in self._reconciliation.intents()
            if intent.checkpoint.startswith(self._CHECKPOINT_PREFIX)
            and not self._reconciliation.observed(intent.operation_id)
        }
        due = sorted(
            (
                entity
                for source, entities in inventories.items()
                for entity in entities
                if (entity.source, entity.entity_id) not in pending_entities
                if self._schedule.due(entity, self._policies[source], now)
            ),
            key=lambda entity: (
                self._schedule.next_search_at(entity, self._policies[entity.source], now) or now,
                entity.released_at,
                entity.source,
                int(entity.entity_id),
            ),
        )
        searched = grabbed = no_candidate = pending = 0
        per_source: dict[str, int] = {source: 0 for source in self._policies}
        per_source_grabs: dict[str, int] = {source: 0 for source in self._policies}
        for entity in due:
            policy = self._policies[entity.source]
            if per_source[entity.source] >= policy.max_searches_per_run or not self._schedule.within_budget(policy, now=now):
                continue
            if per_source_grabs[entity.source] >= policy.max_grabs_per_run:
                continue
            result = self._adapters[entity.source].approved_release(entity.entity_id, policy, current_score=entity.current_score)
            searched += 1
            per_source[entity.source] += 1
            self._schedule.record_budget_event(now)
            self._save_schedule(self._schedule)
            if not result.available:
                unavailable += 1
                continue
            terminal = now >= entity.released_at + policy.terminal_horizon_days * DAY
            if result.release is None:
                self._schedule.record_completed(entity, now=now, terminal=terminal)
                self._save_schedule(self._schedule)
                no_candidate += 1
                continue
            intent = SearchIntent(str(uuid.uuid4()), entity.source, entity.entity_id, False, self._checkpoint(entity, terminal))
            # Selection may cross the external-effect boundary even when its
            # observation is unavailable. Conservatively consume the grab
            # budget before execution so one run cannot initiate another grab.
            per_source_grabs[entity.source] += 1
            execution = self._executor.execute_selected(intent, result.release, now=now)
            if execution == "bound":
                self._schedule.record_completed(entity, now=now, terminal=terminal, operation_id=intent.operation_id)
                self._save_schedule(self._schedule)
                grabbed += 1
            elif execution == "pending":
                pending += 1
            else:
                unavailable += 1
        return SchedulerRunResult(searched, grabbed, no_candidate, unavailable, pending)
