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
    scope_id: str | None = None

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
        if self.scope_id is not None and (not isinstance(self.scope_id, str) or not self.scope_id):
            raise ValueError("upgrade entity scope is invalid")

    @property
    def scope_key(self) -> tuple[str, str]:
        """A fairness scope; a Sonarr series, or one Radarr movie by default."""
        return self.source, self.scope_id or self.entity_id


@dataclass(frozen=True)
class ScheduleCheckpoint:
    generation: str
    last_completed_at: int
    attempts: int
    terminal: bool
    next_search_at: int
    policy_revision: str


@dataclass(frozen=True)
class TransientRetry:
    generation: str
    failures: int
    retry_at: int
    policy_revision: str


def cooldown_for_completed_searches(policy: ReconciliationPolicy, completed_searches: int) -> int:
    """Return the configured cooldown after a validated search count."""
    if isinstance(completed_searches, bool) or not isinstance(completed_searches, int) or completed_searches <= 0:
        raise ValueError("completed search count is invalid")
    if policy.cooldown_seconds == 0:
        return 0
    exponent = completed_searches - 1
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
        transient_retries: Mapping[tuple[str, str], TransientRetry] | None = None,
        fairness_cursor: int = 0,
    ) -> None:
        self._checkpoints = dict(checkpoints or {})
        self._budget_events = list(budget_events)
        self._recorded_operations = set(recorded_operations)
        self._transient_retries = dict(transient_retries or {})
        self._fairness_cursor = fairness_cursor

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
        retry = self._transient_retries.get(key)
        same_retry = retry is not None and retry.generation == entity.generation and retry.policy_revision == policy.schedule_policy_revision
        terminal_at = entity.released_at + policy.terminal_horizon_days * DAY
        if now >= terminal_at:
            if not policy.final_search:
                return None
            if same_generation and checkpoint.terminal:
                return None
            return now
        if same_generation and checkpoint.attempts >= policy.max_attempts:
            return None
        if same_retry:
            assert retry is not None
            return retry.retry_at
        if not same_generation or checkpoint.policy_revision != policy.schedule_policy_revision:
            return now
        assert checkpoint is not None
        return checkpoint.next_search_at

    def due(self, entity: UpgradeEntity, policy: ReconciliationPolicy, now: int) -> bool:
        due_at = self.next_search_at(entity, policy, now)
        return due_at is not None and due_at <= now

    def record_completed(
        self,
        entity: UpgradeEntity,
        policy: ReconciliationPolicy,
        *,
        now: int,
        terminal: bool,
        operation_id: str | None = None,
    ) -> None:
        key = (entity.source, entity.entity_id)
        previous = self._checkpoints.get(key)
        attempts = previous.attempts + 1 if previous is not None and previous.generation == entity.generation else 1
        self._checkpoints[key] = ScheduleCheckpoint(
            entity.generation,
            now,
            attempts,
            terminal,
            now + cooldown_for_completed_searches(policy, attempts),
            policy.schedule_policy_revision,
        )
        self._transient_retries.pop(key, None)
        if operation_id is not None:
            self._recorded_operations.add(operation_id)

    def record_transient_failure(self, entity: UpgradeEntity, policy: ReconciliationPolicy, *, now: int) -> None:
        key = (entity.source, entity.entity_id)
        previous = self._transient_retries.get(key)
        failures = previous.failures + 1 if previous is not None and previous.generation == entity.generation and previous.policy_revision == policy.schedule_policy_revision else 1
        try:
            delay = math.ceil(policy.transient_retry_seconds * (policy.transient_retry_multiplier ** (failures - 1)))
        except OverflowError:
            delay = policy.maximum_transient_retry_seconds
        self._transient_retries[key] = TransientRetry(
            entity.generation,
            failures,
            now + min(delay, policy.maximum_transient_retry_seconds),
            policy.schedule_policy_revision,
        )

    def record_fairness_choice(self, entity: UpgradeEntity) -> None:
        # Persisting an opaque cursor keeps a restart from permanently favoring
        # the first series in a lexicographic inventory.
        self._fairness_cursor = (self._fairness_cursor + 1) % 2_147_483_647

    def fair_order(self, entities: list[UpgradeEntity]) -> list[UpgradeEntity]:
        """Round-robin due work across series/movie scopes for this run."""
        scopes: dict[tuple[str, str], list[UpgradeEntity]] = {}
        for entity in entities:
            scopes.setdefault(entity.scope_key, []).append(entity)
        keys = list(scopes)
        if not keys:
            return []
        start = self._fairness_cursor % len(keys)
        ordered_keys = keys[start:] + keys[:start]
        ordered: list[UpgradeEntity] = []
        while any(scopes[key] for key in ordered_keys):
            for key in ordered_keys:
                if scopes[key]:
                    ordered.append(scopes[key].pop(0))
        return ordered

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
                    "next_search_at": checkpoint.next_search_at,
                    "policy_revision": checkpoint.policy_revision,
                }
                for (source, entity_id), checkpoint in sorted(self._checkpoints.items())
            ],
            "budget_events": list(self._budget_events),
            "recorded_operations": sorted(self._recorded_operations),
            "transient_retries": [
                {
                    "source": source,
                    "entity_id": entity_id,
                    "generation": retry.generation,
                    "failures": retry.failures,
                    "retry_at": retry.retry_at,
                    "policy_revision": retry.policy_revision,
                }
                for (source, entity_id), retry in sorted(self._transient_retries.items())
            ],
            "fairness_cursor": self._fairness_cursor,
        }

    @classmethod
    def from_record(cls, record: object) -> "ScheduleState":
        if not isinstance(record, dict) or set(record) not in (
            {"checkpoints", "budget_events", "recorded_operations"},
            {"checkpoints", "budget_events", "recorded_operations", "transient_retries", "fairness_cursor"},
        ):
            raise ValueError("schedule state is invalid")
        raw_checkpoints, raw_events, raw_operations = record["checkpoints"], record["budget_events"], record["recorded_operations"]
        raw_retries = record.get("transient_retries", [])
        fairness_cursor = record.get("fairness_cursor", 0)
        if not isinstance(raw_checkpoints, list) or not isinstance(raw_events, list) or not isinstance(raw_operations, list) or not isinstance(raw_retries, list) or isinstance(fairness_cursor, bool) or not isinstance(fairness_cursor, int) or fairness_cursor < 0:
            raise ValueError("schedule state is invalid")
        checkpoints: dict[tuple[str, str], ScheduleCheckpoint] = {}
        for item in raw_checkpoints:
            if not isinstance(item, dict) or set(item) not in (
                {"source", "entity_id", "generation", "last_completed_at", "attempts", "terminal"},
                {"source", "entity_id", "generation", "last_completed_at", "attempts", "terminal", "next_search_at", "policy_revision"},
            ):
                raise ValueError("schedule checkpoint is invalid")
            entity = UpgradeEntity(item["source"], item["entity_id"], 0, item["generation"], 0)
            last, attempts, terminal = item["last_completed_at"], item["attempts"], item["terminal"]
            if isinstance(last, bool) or not isinstance(last, int) or last < 0 or isinstance(attempts, bool) or not isinstance(attempts, int) or attempts <= 0 or not isinstance(terminal, bool):
                raise ValueError("schedule checkpoint is invalid")
            key = (entity.source, entity.entity_id)
            if key in checkpoints:
                raise ValueError("schedule checkpoint is duplicated")
            next_search_at = item.get("next_search_at", last)
            revision = item.get("policy_revision", "legacy")
            if isinstance(next_search_at, bool) or not isinstance(next_search_at, int) or next_search_at < last or not isinstance(revision, str) or not revision:
                raise ValueError("schedule checkpoint is invalid")
            checkpoints[key] = ScheduleCheckpoint(entity.generation, last, attempts, terminal, next_search_at, revision)
        if any(isinstance(event, bool) or not isinstance(event, int) or event < 0 for event in raw_events):
            raise ValueError("schedule budget event is invalid")
        if any(not isinstance(operation, str) or not operation for operation in raw_operations) or len(set(raw_operations)) != len(raw_operations):
            raise ValueError("schedule operation registry is invalid")
        retries: dict[tuple[str, str], TransientRetry] = {}
        for item in raw_retries:
            if not isinstance(item, dict) or set(item) != {"source", "entity_id", "generation", "failures", "retry_at", "policy_revision"}:
                raise ValueError("schedule retry is invalid")
            entity = UpgradeEntity(item["source"], item["entity_id"], 0, item["generation"], 0)
            failures, retry_at, revision = item["failures"], item["retry_at"], item["policy_revision"]
            if isinstance(failures, bool) or not isinstance(failures, int) or failures <= 0 or isinstance(retry_at, bool) or not isinstance(retry_at, int) or retry_at < 0 or not isinstance(revision, str) or not revision:
                raise ValueError("schedule retry is invalid")
            key = (entity.source, entity.entity_id)
            if key in retries:
                raise ValueError("schedule retry is duplicated")
            retries[key] = TransientRetry(entity.generation, failures, retry_at, revision)
        return cls(checkpoints, tuple(raw_events), tuple(raw_operations), retries, fairness_cursor)


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
            self._schedule.record_completed(
                entity,
                self._policies[entity.source],
                now=now,
                terminal=terminal,
                operation_id=intent.operation_id,
            )
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
        due = self._schedule.fair_order(due)
        searched = grabbed = no_candidate = pending = 0
        per_source: dict[str, int] = {source: 0 for source in self._policies}
        per_source_grabs: dict[str, int] = {source: 0 for source in self._policies}
        for entity in due:
            policy = self._policies[entity.source]
            if per_source[entity.source] >= policy.max_searches_per_run or not self._schedule.within_budget(policy, now=now):
                continue
            if per_source_grabs[entity.source] >= policy.max_grabs_per_run:
                continue
            self._schedule.record_fairness_choice(entity)
            result = self._adapters[entity.source].approved_release(entity.entity_id, policy, current_score=entity.current_score)
            if not result.available:
                unavailable += 1
                self._schedule.record_transient_failure(entity, policy, now=now)
                self._save_schedule(self._schedule)
                continue
            searched += 1
            per_source[entity.source] += 1
            self._schedule.record_budget_event(now)
            self._save_schedule(self._schedule)
            terminal = now >= entity.released_at + policy.terminal_horizon_days * DAY
            if result.release is None:
                self._schedule.record_completed(entity, policy, now=now, terminal=terminal)
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
                self._schedule.record_completed(entity, policy, now=now, terminal=terminal, operation_id=intent.operation_id)
                self._save_schedule(self._schedule)
                grabbed += 1
            elif execution == "pending":
                pending += 1
            else:
                unavailable += 1
        return SchedulerRunResult(searched, grabbed, no_candidate, unavailable, pending)
