from __future__ import annotations

import unittest
import uuid
import tempfile
import hashlib
import json
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.reconciler.model import AttemptPolicy, GrabIntent, ReconciliationState, SearchIntent
from media_interlock.reconciler.store import ReconcilerStore


class ReconciliationModelTests(unittest.TestCase):
    def test_search_intent_binds_one_public_entity_and_opaque_checkpoint(self) -> None:
        operation_id = str(uuid.uuid4())
        intent = SearchIntent(
            operation_id=operation_id,
            source="radarr",
            entity_id="42",
            force=False,
            checkpoint="2026-08-08T12:00:00Z",
        )

        state = ReconciliationState()
        self.assertEqual(intent, state.record_intent(intent))
        self.assertEqual(intent, state.record_intent(intent))
        self.assertEqual(intent, state.intent(operation_id))

    def test_search_intent_rejects_paths_nonpublic_ids_and_conflicting_replay(self) -> None:
        operation_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            SearchIntent(operation_id, "radarr", "../../movie", False, "checkpoint")
        with self.assertRaises(ValueError):
            SearchIntent(operation_id, "unknown", "42", False, "checkpoint")

        state = ReconciliationState()
        state.record_intent(SearchIntent(operation_id, "sonarr", "42", False, "checkpoint"))
        with self.assertRaises(ValueError):
            state.record_intent(SearchIntent(operation_id, "sonarr", "43", False, "checkpoint"))

    def test_attempt_suppression_force_and_uncertain_effect_are_distinct(self) -> None:
        state = ReconciliationState()
        policy = AttemptPolicy(cooldown_seconds=60, max_attempts=2)
        first = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "checkpoint-a")

        self.assertTrue(state.eligible(first.source, first.entity_id, policy, now=100))
        state.record_intent(first, now=100)
        self.assertFalse(state.eligible(first.source, first.entity_id, policy, now=200))
        self.assertFalse(state.eligible(first.source, first.entity_id, policy, now=200, force=True))

        state.mark_observed(first.operation_id, completed=True, now=200)
        self.assertFalse(state.eligible(first.source, first.entity_id, policy, now=201))
        self.assertTrue(state.eligible(first.source, first.entity_id, policy, now=201, force=True))

    def test_durable_records_preserve_uncertain_intent_across_restart(self) -> None:
        intent = SearchIntent(str(uuid.uuid4()), "sonarr", "42", False, "checkpoint-a")
        state = ReconciliationState()
        state.record_intent(intent, now=100)

        restored = ReconciliationState.from_records(state.records())
        self.assertEqual(intent, restored.intent(intent.operation_id))
        self.assertFalse(restored.eligible("sonarr", "42", AttemptPolicy(0, 1), now=999, force=True))

    def test_noncompleted_observation_does_not_become_completed_after_restart(self) -> None:
        intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "checkpoint-a")
        state = ReconciliationState()
        state.record_intent(intent, now=100)
        state.mark_observed(intent.operation_id, completed=False, now=200)

        restored = ReconciliationState.from_records(state.records())
        self.assertTrue(restored.eligible("radarr", "42", AttemptPolicy(0, 1), now=201))

    def test_private_store_restores_uncertain_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReconcilerStore.open(Path(directory) / "reconciler")
            intent = SearchIntent(str(uuid.uuid4()), "radarr", "42", False, "checkpoint-a")
            state = ReconciliationState()
            state.record_intent(intent, now=100)
            store.save(state)
            store.close()

            restarted = ReconcilerStore.open(Path(directory) / "reconciler")
            self.addCleanup(restarted.close)
            self.assertEqual(intent, restarted.load().intent(intent.operation_id))

    def test_grab_intent_is_durable_before_release_effect_and_exact_on_restart(self) -> None:
        operation_id = str(uuid.uuid4())
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        fingerprint = hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        search = SearchIntent(operation_id, "radarr", "42", False, "checkpoint-a")
        grab = GrabIntent(operation_id, "radarr", "42", fingerprint, 400, 7, resource)
        state = ReconciliationState()
        state.record_intent(search, now=100)

        self.assertEqual(grab, state.record_grab_intent(grab))
        restored = ReconciliationState.from_records(state.records())

        self.assertEqual(grab, restored.grab_intent(operation_id))

    def test_grab_effect_is_marked_before_post_and_persists_across_restart(self) -> None:
        operation_id = str(uuid.uuid4())
        resource = {"approved": True, "protocol": "torrent", "guid": "release-42", "title": "fixture.movie.2026", "size": 400, "downloadUrl": "https://indexer.invalid/release"}
        fingerprint = hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        state = ReconciliationState()
        state.record_intent(SearchIntent(operation_id, "radarr", "42", False, "checkpoint-a"))
        state.record_grab_intent(GrabIntent(operation_id, "radarr", "42", fingerprint, 400, 7, resource))

        state.mark_grab_attempted(operation_id)

        self.assertTrue(ReconciliationState.from_records(state.records()).grab_attempted(operation_id))
