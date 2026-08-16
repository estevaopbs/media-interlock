from __future__ import annotations

import unittest
import hashlib
import json

import _source_tree  # noqa: F401

from media_interlock.adapters.lidarr import LidarrAlbum, LidarrAdapter, LidarrRelease
from media_interlock.config import MusicFencePolicy, ReconciliationPolicy
from media_interlock.reconciler.music import MusicScheduleState, MusicScheduler


class MusicScheduleStateTests(unittest.TestCase):
    @staticmethod
    def policy(**overrides: object) -> ReconciliationPolicy:
        values: dict[str, object] = {
            "minimum_age_days": 0,
            "terminal_horizon_days": 365,
            "cooldown_seconds": 86_400,
            "cooldown_step_days": 7,
            "cooldown_multiplier": 2.0,
            "maximum_cooldown_seconds": 0,
            "final_search": False,
            "max_attempts": 8,
            "max_searches_per_run": 6,
            "max_searches_per_hour": 10,
            "max_searches_per_day": 20,
            "max_grabs_per_run": 1,
            "minimum_candidate_score": 0,
            "minimum_score_gain": 0,
            "required_candidate_formats": ("Lossless",),
            "forbidden_candidate_formats": (),
        }
        values.update(overrides)
        return ReconciliationPolicy(**values)

    @staticmethod
    def release(guid: str, info_hash: str) -> LidarrRelease:
        resource: dict[str, object] = {
            "approved": True,
            "downloadAllowed": True,
            "protocol": "torrent",
            "guid": guid,
            "title": guid,
            "size": 400,
            "magnetUrl": f"magnet:?xt=urn:btih:{info_hash}",
            "albumId": 42,
            "seeders": None if guid == "unknown" else 2,
            "indexer": "reliable",
            "infoHash": info_hash,
            "customFormatScore": 1,
            "customFormats": [{"name": "Lossless"}],
        }
        return LidarrRelease(
            resource,
            hashlib.sha256(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            400,
            "42",
            resource["seeders"],
            "reliable",
            info_hash,
            1,
            ("Lossless",),
        )

    def test_music_cooldown_is_independent_from_video_schedule_state(self) -> None:
        policy = self.policy(required_candidate_formats=())
        music = MusicScheduleState()
        album = LidarrAlbum("42", 0)

        self.assertTrue(music.due(album, policy, now=86_400, max_candidates=3))
        music.complete_cycle(album, policy, now=86_400)
        self.assertFalse(music.due(album, policy, now=86_400, max_candidates=3))
        self.assertTrue(music.due(album, policy, now=172_800, max_candidates=3))

    def test_invalid_candidate_is_rejected_before_the_next_native_release_is_selected(self) -> None:
        unknown = self.release("unknown", "a" * 40)
        second = self.release("second", "b" * 40)
        third = self.release("third", "c" * 40)
        selected: list[str] = []

        class Adapter:
            def missing_albums(self) -> tuple[LidarrAlbum, ...]:
                return (LidarrAlbum("42", 0),)

            def album_releases(self, _: str) -> tuple[LidarrRelease, ...]:
                return unknown, second, third

            def first_approved_release(self, releases: tuple[LidarrRelease, ...], policy: ReconciliationPolicy, health: MusicFencePolicy, *, current_score: int) -> LidarrRelease | None:
                return LidarrAdapter.first_approved_release(releases, policy, health, current_score=current_score)

        class Executor:
            def execute_selected(self, _intent: object, candidate: LidarrRelease, *, now: int) -> str:
                selected.append(str(candidate.resource["guid"]))
                return "bound"

        state = MusicScheduleState()
        scheduler = MusicScheduler(
            state,
            lambda _: None,
            Adapter(),
            self.policy(),
            MusicFencePolicy(
                minimum_reported_seeders=1,
                unknown_seeders_policy="reject",
                probe_only_indexers=(),
                metadata_probe_seconds=900,
                no_progress_seconds=3600,
                max_candidates_per_cycle=3,
                delete_invalid_payload=True,
            ),
            Executor(),
        )

        self.assertEqual(1, scheduler.run(now=86_400).grabbed)
        self.assertEqual(["second"], selected)
        self.assertTrue(state.invalidate("42", second.selector_fingerprint, second.canonical_hash, "metadata-timeout", now=86_401))
        self.assertEqual(1, scheduler.run(now=86_401).grabbed)
        self.assertEqual(["second", "third"], selected)

    def test_active_candidate_is_sealed_and_survives_a_schedule_restart(self) -> None:
        state = MusicScheduleState()
        album = LidarrAlbum("42", 0)
        release = self.release("selected", "d" * 40)
        operation_id = str(__import__("uuid").uuid4())

        state.start_candidate(album, release, operation_id=operation_id, now=100, max_candidates=3)
        restored = MusicScheduleState.from_record(state.record())

        self.assertEqual(
            ((album, operation_id, release.selector_fingerprint, release.resource),),
            restored.active_candidates(),
        )

    def test_fence_invalidation_is_applied_once_before_another_candidate_can_run(self) -> None:
        import uuid
        state = MusicScheduleState()
        album = LidarrAlbum("42", 0)
        release = self.release("selected", "d" * 40)
        operation_id = str(uuid.uuid4())
        invalidation_id = str(uuid.uuid4())
        state.start_candidate(album, release, operation_id=operation_id, now=100, max_candidates=3)

        self.assertTrue(state.apply_fence_invalidation(
            operation_id=operation_id, album_id="42", selector_fingerprint=release.selector_fingerprint,
            canonical_hash="d" * 40, invalidation_id=invalidation_id, reason="metadata-timeout", now=200,
        ))
        self.assertTrue(state.apply_fence_invalidation(
            operation_id=operation_id, album_id="42", selector_fingerprint=release.selector_fingerprint,
            canonical_hash="d" * 40, invalidation_id=invalidation_id, reason="metadata-timeout", now=201,
        ))
        self.assertTrue(state.was_rejected("42", release.selector_fingerprint, "d" * 40))
        self.assertEqual((), state.active_candidates())
