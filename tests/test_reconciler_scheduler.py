from __future__ import annotations

import unittest
import hashlib
import json

import _source_tree  # noqa: F401

from media_interlock.config import ReconciliationPolicy
from media_interlock.adapters.arr import ArrRelease, ArrReleaseSearch
from media_interlock.reconciler.model import ReconciliationState, SearchIntent
from media_interlock.reconciler.scheduler import (
    ScheduleState,
    UpgradeScheduler,
    UpgradeEntity,
    cooldown_for_completed_searches,
)


DAY = 86_400


def policy(**overrides: object) -> ReconciliationPolicy:
    values: dict[str, object] = {
        "minimum_age_days": 0,
        "terminal_horizon_days": 182,
        "cooldown_seconds": 6 * 3_600,
        "cooldown_step_days": 7,
        "cooldown_multiplier": 2.0,
        "maximum_cooldown_seconds": 0,
        "final_search": True,
        "max_attempts": 64,
        "max_searches_per_run": 10,
        "max_searches_per_hour": 4,
        "max_searches_per_day": 20,
        "max_grabs_per_run": 1,
        "minimum_candidate_score": 0,
        "minimum_score_gain": 0,
        "required_candidate_formats": (),
        "forbidden_candidate_formats": (),
    }
    values.update(overrides)
    return ReconciliationPolicy(**values)


class ReconcilerSchedulerTests(unittest.TestCase):
    def test_cooldown_scales_after_successful_searches_and_can_be_capped(self) -> None:
        configured = policy()

        self.assertEqual(6 * 3_600, cooldown_for_completed_searches(configured, 1))
        self.assertEqual(12 * 3_600, cooldown_for_completed_searches(configured, 2))
        self.assertEqual(24 * 3_600, cooldown_for_completed_searches(configured, 3))
        self.assertEqual(
            18 * 3_600,
            cooldown_for_completed_searches(
                policy(maximum_cooldown_seconds=18 * 3_600),
                4,
            ),
        )
        self.assertGreater(
            cooldown_for_completed_searches(policy(cooldown_multiplier=100.0), 10),
            182 * DAY,
        )

    def test_new_entity_is_due_once_and_then_obeys_exponential_cooldown(self) -> None:
        state = ScheduleState()
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)
        configured = policy()

        self.assertEqual(14 * DAY, state.next_search_at(entity, configured, 14 * DAY))
        state.record_completed(entity, configured, now=14 * DAY, terminal=False)

        self.assertEqual(14 * DAY + 6 * 3_600, state.next_search_at(entity, configured, 14 * DAY))
        self.assertFalse(state.due(entity, configured, 14 * DAY + 5 * 3_600))
        self.assertTrue(state.due(entity, configured, 14 * DAY + 6 * 3_600))

    def test_elapsed_age_does_not_stretch_the_first_cooldown(self) -> None:
        state = ScheduleState()
        configured = policy()
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)

        state.record_completed(entity, configured, now=90 * DAY, terminal=False)

        self.assertEqual(90 * DAY + 6 * 3_600, state.next_search_at(entity, configured, 90 * DAY))

    def test_transient_release_failure_has_independent_retry_and_no_attempt(self) -> None:
        state = ScheduleState()
        configured = policy(transient_retry_seconds=600, transient_retry_multiplier=2.0)
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)

        state.record_transient_failure(entity, configured, now=100)

        self.assertEqual(700, state.next_search_at(entity, configured, 100))
        self.assertTrue(state.due(entity, configured, 700))
        self.assertEqual([], state.record()["checkpoints"])

    def test_policy_revision_makes_old_schedule_due_under_current_policy(self) -> None:
        state = ScheduleState()
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)
        state.record_completed(entity, policy(schedule_policy_revision="old"), now=100, terminal=False)

        self.assertTrue(state.due(entity, policy(schedule_policy_revision="current"), 101))

    def test_terminal_search_runs_once_and_new_generation_gets_one_final_search(self) -> None:
        state = ScheduleState()
        configured = policy(terminal_horizon_days=26 * 7)
        old = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)
        terminal_now = 26 * 7 * DAY

        self.assertTrue(state.due(old, configured, terminal_now))
        state.record_completed(old, configured, now=terminal_now, terminal=True)
        self.assertIsNone(state.next_search_at(old, configured, terminal_now + DAY))

        replacement = UpgradeEntity("sonarr", "42", 0, "file-8", 8_000)
        self.assertTrue(state.due(replacement, configured, terminal_now + DAY))
        state.record_completed(replacement, configured, now=terminal_now + DAY, terminal=True)
        self.assertIsNone(state.next_search_at(replacement, configured, terminal_now + 2 * DAY))

    def test_final_search_is_independent_of_intermediate_attempt_limit(self) -> None:
        state = ScheduleState()
        configured = policy(max_attempts=1, terminal_horizon_days=14)
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)
        state.record_completed(entity, configured, now=DAY, terminal=False)

        self.assertIsNone(state.next_search_at(entity, configured, 2 * DAY))
        self.assertTrue(state.due(entity, configured, 14 * DAY))

    def test_minimum_age_attempt_limit_and_rolling_budgets_are_parameterized(self) -> None:
        state = ScheduleState()
        entity = UpgradeEntity("radarr", "9", 100, "movie-file-3", 1_000)
        configured = policy(minimum_age_days=7, max_attempts=2)

        self.assertEqual(100 + 7 * DAY, state.next_search_at(entity, configured, 100))
        state.record_completed(entity, configured, now=100 + 7 * DAY, terminal=False)
        state.record_completed(entity, configured, now=100 + 8 * DAY, terminal=False)
        self.assertIsNone(state.next_search_at(entity, configured, 100 + 9 * DAY))

        for offset in range(4):
            state.record_budget_event(1_000 + offset)
        self.assertFalse(state.within_budget(policy(), now=1_100))
        self.assertTrue(state.within_budget(policy(), now=1_000 + 3_601))

    def test_state_round_trip_preserves_checkpoints_and_budget_events(self) -> None:
        state = ScheduleState()
        entity = UpgradeEntity("sonarr", "42", 0, "file-7", 4_000)
        state.record_completed(entity, policy(), now=123, terminal=False)
        state.record_budget_event(123)

        restored = ScheduleState.from_record(state.record())

        self.assertEqual(state.record(), restored.record())

    def test_run_due_records_empty_search_then_grabs_one_candidate_and_stops(self) -> None:
        entities = (
            UpgradeEntity("sonarr", "1", 10, "file-1", 0),
            UpgradeEntity("sonarr", "2", 20, "file-2", 0),
            UpgradeEntity("sonarr", "3", 30, "file-3", 0),
        )
        resource = {
            "approved": True,
            "protocol": "torrent",
            "guid": "release-2",
            "title": "fixture.release.2",
            "size": 400,
            "downloadUrl": "https://indexer.invalid/release-2",
        }
        release = ArrRelease(
            resource,
            hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            400,
        )
        searched: list[str] = []

        class Adapter:
            def upgrade_entities(self) -> tuple[UpgradeEntity, ...]:
                return entities

            def approved_release(self, entity_id: str, _: ReconciliationPolicy, *, current_score: int) -> ArrReleaseSearch:
                searched.append(entity_id)
                return ArrReleaseSearch.completed(release if entity_id == "2" else None)

        class Executor:
            def recover(self, *, now: int) -> list[str]:
                return []

            def execute_selected(self, intent: object, selected: ArrRelease, *, now: int) -> str:
                self.intent = intent
                self.release = selected
                return "bound"

        schedule = ScheduleState()
        saves: list[dict[str, object]] = []
        result = UpgradeScheduler(
            schedule,
            ReconciliationState(),
            lambda state: saves.append(state.record()),
            {"sonarr": Adapter()},
            {"sonarr": policy(max_grabs_per_run=1)},
            Executor(),
        ).run(now=DAY)

        self.assertEqual(["1", "2"], searched)
        self.assertEqual(2, result.searched)
        self.assertEqual(1, result.no_candidate)
        self.assertEqual(1, result.grabbed)
        self.assertFalse(schedule.due(entities[0], policy(), DAY))
        self.assertFalse(schedule.due(entities[1], policy(), DAY))
        self.assertTrue(schedule.due(entities[2], policy(), DAY))
        self.assertTrue(saves)

    def test_adapter_failure_uses_technical_retry_without_consuming_search_budget(self) -> None:
        entity = UpgradeEntity("sonarr", "1", 10, "file-1", 0)

        class Adapter:
            def upgrade_entities(self) -> tuple[UpgradeEntity, ...]:
                return (entity,)

            def approved_release(self, entity_id: str, _: ReconciliationPolicy, *, current_score: int) -> ArrReleaseSearch:
                return ArrReleaseSearch.transient_failure("upstream response was invalid")

        class Executor:
            def recover(self, *, now: int) -> list[str]: return []
            def execute_selected(self, *_: object, **__: object) -> str: raise AssertionError

        schedule = ScheduleState()
        result = UpgradeScheduler(
            schedule,
            ReconciliationState(),
            lambda _: None,
            {"sonarr": Adapter()},
            {"sonarr": policy()},
            Executor(),
        ).run(now=DAY)

        self.assertEqual(1, result.unavailable)
        self.assertFalse(schedule.due(entity, policy(), DAY))
        self.assertEqual([], schedule.record()["budget_events"])

    def test_uncertain_grab_stops_more_candidates_in_the_same_run(self) -> None:
        entities = (
            UpgradeEntity("sonarr", "1", 10, "file-1", 0),
            UpgradeEntity("sonarr", "2", 20, "file-2", 0),
        )
        resource = {
            "approved": True,
            "protocol": "torrent",
            "guid": "release",
            "title": "fixture.release",
            "size": 400,
            "downloadUrl": "https://indexer.invalid/release",
        }
        release = ArrRelease(
            resource,
            hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            400,
        )
        searched: list[str] = []

        class Adapter:
            def upgrade_entities(self) -> tuple[UpgradeEntity, ...]:
                return entities

            def approved_release(self, entity_id: str, _: ReconciliationPolicy, *, current_score: int) -> ArrReleaseSearch:
                searched.append(entity_id)
                return ArrReleaseSearch.completed(release)

        class Executor:
            def recover(self, *, now: int) -> list[str]: return []
            def execute_selected(self, *_: object, **__: object) -> str: return "unavailable"

        result = UpgradeScheduler(
            ScheduleState(),
            ReconciliationState(),
            lambda _: None,
            {"sonarr": Adapter()},
            {"sonarr": policy(max_grabs_per_run=1)},
            Executor(),
        ).run(now=DAY)

        self.assertEqual(["1"], searched)
        self.assertEqual(1, result.searched)
        self.assertEqual(1, result.unavailable)

    def test_pending_auto_intent_suppresses_duplicate_search_after_restart(self) -> None:
        entity = UpgradeEntity("sonarr", "1", 10, "file-1", 0)
        reconciliation = ReconciliationState()
        reconciliation.record_intent(
            SearchIntent(
                "2da90747-f4d4-4605-8c81-afcd699133a2",
                "sonarr",
                "1",
                False,
                "auto-v1:file-1:0",
            ),
            now=100,
        )

        class Adapter:
            def upgrade_entities(self) -> tuple[UpgradeEntity, ...]: return (entity,)
            def approved_release(self, *_: object, **__: object) -> ArrReleaseSearch: raise AssertionError

        class Executor:
            def recover(self, *, now: int) -> list[str]: return ["pending"]
            def execute_selected(self, *_: object, **__: object) -> str: raise AssertionError

        result = UpgradeScheduler(
            ScheduleState(),
            reconciliation,
            lambda _: None,
            {"sonarr": Adapter()},
            {"sonarr": policy()},
            Executor(),
        ).run(now=DAY)

        self.assertEqual(0, result.searched)

    def test_sonarr_searches_are_fair_between_series(self) -> None:
        entities = (
            UpgradeEntity("sonarr", "1", 0, "file-1", 0, "10"),
            UpgradeEntity("sonarr", "2", 0, "file-2", 0, "10"),
            UpgradeEntity("sonarr", "3", 0, "file-3", 0, "20"),
        )
        searched: list[str] = []

        class Adapter:
            def upgrade_entities(self) -> tuple[UpgradeEntity, ...]: return entities
            def approved_release(self, entity_id: str, *_: object, **__: object) -> ArrReleaseSearch:
                searched.append(entity_id)
                return ArrReleaseSearch.completed()

        class Executor:
            def recover(self, *, now: int) -> list[str]: return []
            def execute_selected(self, *_: object, **__: object) -> str: raise AssertionError

        UpgradeScheduler(
            ScheduleState(),
            ReconciliationState(),
            lambda _: None,
            {"sonarr": Adapter()},
            {"sonarr": policy(max_searches_per_run=2)},
            Executor(),
        ).run(now=DAY)

        self.assertEqual(["1", "3"], searched)


if __name__ == "__main__":
    unittest.main()
