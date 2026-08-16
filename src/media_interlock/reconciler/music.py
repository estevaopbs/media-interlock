"""Independent, durable scheduling state for Lidarr candidate cycles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol
import uuid

from ..adapters.lidarr import LidarrAlbum, LidarrRelease
from ..config import MusicFencePolicy, ReconciliationPolicy
from .model import SearchIntent
from .scheduler import DAY, SchedulerRunResult, cooldown_for_completed_searches


@dataclass
class _AlbumCycle:
    released_at: int
    last_completed_at: int | None = None
    completed_cycles: int = 0
    terminal: bool = False
    cycle_candidates_used: int = 0
    active_operation_id: str | None = None
    active_selector_fingerprint: str | None = None
    active_canonical_hash: str | None = None
    rejected_selectors: set[str] | None = None
    rejected_hashes: set[str] | None = None
    cycle_candidate_limit: int | None = None
    applied_invalidation_ids: set[str] | None = None
    active_release_resource: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.rejected_selectors is None:
            self.rejected_selectors = set()
        if self.rejected_hashes is None:
            self.rejected_hashes = set()
        if self.applied_invalidation_ids is None:
            self.applied_invalidation_ids = set()


class MusicScheduleState:
    """Keep music cooldowns separate from movie and episode checkpoints."""

    def __init__(self, albums: dict[str, _AlbumCycle] | None = None, budget_events: tuple[int, ...] = ()) -> None:
        self._albums = {} if albums is None else albums
        self._budget_events = list(budget_events)

    @staticmethod
    def _valid_now(now: int) -> None:
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("music schedule clock is invalid")

    @staticmethod
    def _valid_album(album: LidarrAlbum) -> None:
        if (
            not isinstance(album, LidarrAlbum)
            or not album.album_id.isdecimal()
            or int(album.album_id) <= 0
            or isinstance(album.released_at, bool)
            or not isinstance(album.released_at, int)
            or album.released_at < 0
        ):
            raise ValueError("music album identity is invalid")

    def _cycle(self, album: LidarrAlbum) -> _AlbumCycle:
        self._valid_album(album)
        current = self._albums.get(album.album_id)
        if current is None:
            current = _AlbumCycle(album.released_at)
            self._albums[album.album_id] = current
        elif current.released_at != album.released_at:
            raise ValueError("music album release time drifted")
        return current

    def due(
        self,
        album: LidarrAlbum,
        policy: ReconciliationPolicy,
        *,
        now: int,
        max_candidates: int,
    ) -> bool:
        self._valid_now(now)
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates <= 0:
            raise ValueError("music candidate limit is invalid")
        cycle = self._cycle(album)
        if cycle.active_operation_id is not None or cycle.terminal:
            return False
        if cycle.cycle_candidates_used and cycle.cycle_candidates_used < max_candidates:
            return True
        minimum_at = album.released_at + policy.minimum_age_days * DAY
        if now < minimum_at:
            return False
        terminal_at = album.released_at + policy.terminal_horizon_days * DAY
        if now >= terminal_at:
            return policy.final_search and not cycle.terminal
        if cycle.completed_cycles >= policy.max_attempts:
            return False
        if cycle.last_completed_at is None:
            return True
        return now >= cycle.last_completed_at + cooldown_for_completed_searches(policy, cycle.completed_cycles)

    def complete_cycle(self, album: LidarrAlbum, policy: ReconciliationPolicy, *, now: int) -> None:
        self._valid_now(now)
        cycle = self._cycle(album)
        cycle.last_completed_at = now
        cycle.completed_cycles += 1
        cycle.terminal = now >= album.released_at + policy.terminal_horizon_days * DAY
        cycle.cycle_candidates_used = 0
        cycle.cycle_candidate_limit = None

    def was_rejected(self, album_id: str, selector_fingerprint: str, canonical_hash: str | None) -> bool:
        cycle = self._albums.get(album_id)
        if cycle is None:
            return False
        return selector_fingerprint in cycle.rejected_selectors or canonical_hash is not None and canonical_hash in cycle.rejected_hashes

    def start_candidate(
        self,
        album: LidarrAlbum,
        release: LidarrRelease,
        *,
        operation_id: str,
        now: int,
        max_candidates: int,
    ) -> None:
        self._valid_now(now)
        if not isinstance(release, LidarrRelease) or release.album_id != album.album_id:
            raise ValueError("music candidate does not match album")
        try:
            parsed = uuid.UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("music operation id is invalid") from exc
        if (
            str(parsed) != operation_id
            or isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or max_candidates <= 0
        ):
            raise ValueError("music candidate state is invalid")
        try:
            encoded = json.dumps(release.resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            sealed_resource = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("music candidate state is invalid") from exc
        if not isinstance(sealed_resource, dict) or hashlib.sha256(encoded).hexdigest() != release.selector_fingerprint:
            raise ValueError("music candidate state is invalid")
        cycle = self._cycle(album)
        if (
            cycle.active_operation_id is not None
            or cycle.cycle_candidates_used >= max_candidates
            or self.was_rejected(album.album_id, release.selector_fingerprint, release.canonical_hash)
        ):
            raise ValueError("music candidate cannot be started")
        if cycle.cycle_candidate_limit is None:
            cycle.cycle_candidate_limit = max_candidates
        elif cycle.cycle_candidate_limit != max_candidates:
            raise ValueError("music candidate limit changed during an active cycle")
        cycle.cycle_candidates_used += 1
        cycle.active_operation_id = operation_id
        cycle.active_selector_fingerprint = release.selector_fingerprint
        cycle.active_canonical_hash = release.canonical_hash
        cycle.active_release_resource = sealed_resource

    def active_candidates(self) -> tuple[tuple[LidarrAlbum, str, str, dict[str, object]], ...]:
        """Return sealed candidates that must be recovered before fresh searches."""
        result: list[tuple[LidarrAlbum, str, str, dict[str, object]]] = []
        for album_id, cycle in sorted(self._albums.items(), key=lambda item: int(item[0])):
            if cycle.active_operation_id is None:
                continue
            if cycle.active_selector_fingerprint is None or cycle.active_release_resource is None:
                raise ValueError("active music candidate lacks a sealed release")
            result.append((
                LidarrAlbum(album_id, cycle.released_at),
                cycle.active_operation_id,
                cycle.active_selector_fingerprint,
                dict(cycle.active_release_resource),
            ))
        return tuple(result)

    def confirm_imported(self, album_id: str) -> bool:
        """Converge only when Lidarr no longer reports the album missing."""
        cycle = self._albums.get(album_id)
        if cycle is None or cycle.active_operation_id is None:
            return False
        cycle.active_operation_id = None
        cycle.active_selector_fingerprint = None
        cycle.active_canonical_hash = None
        cycle.active_release_resource = None
        cycle.cycle_candidates_used = 0
        cycle.cycle_candidate_limit = None
        return True

    def invalidate(
        self,
        album_id: str,
        selector_fingerprint: str,
        canonical_hash: str | None,
        reason: str,
        *,
        now: int,
    ) -> bool:
        self._valid_now(now)
        if (
            not isinstance(album_id, str)
            or not album_id.isdecimal()
            or int(album_id) <= 0
            or not isinstance(selector_fingerprint, str)
            or len(selector_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in selector_fingerprint)
            or canonical_hash is not None and (not isinstance(canonical_hash, str) or len(canonical_hash) != 40 or any(character not in "0123456789abcdef" for character in canonical_hash))
            or reason not in {"metadata-timeout", "no-peer-progress", "no-progress"}
        ):
            raise ValueError("music invalidation is invalid")
        cycle = self._albums.get(album_id)
        if cycle is None or cycle.active_selector_fingerprint != selector_fingerprint or cycle.active_canonical_hash != canonical_hash:
            return False
        cycle.rejected_selectors.add(selector_fingerprint)
        if canonical_hash is not None:
            cycle.rejected_hashes.add(canonical_hash)
        cycle.active_operation_id = None
        cycle.active_selector_fingerprint = None
        cycle.active_canonical_hash = None
        cycle.active_release_resource = None
        if cycle.cycle_candidate_limit is not None and cycle.cycle_candidates_used >= cycle.cycle_candidate_limit:
            cycle.last_completed_at = now
            cycle.completed_cycles += 1
            cycle.cycle_candidates_used = 0
            cycle.cycle_candidate_limit = None
        return True

    def apply_fence_invalidation(
        self,
        *,
        operation_id: str,
        album_id: str,
        selector_fingerprint: str,
        canonical_hash: str,
        invalidation_id: str,
        reason: str,
        now: int,
    ) -> bool:
        """Apply one durable Fence verdict before its acknowledgement."""
        self._valid_now(now)
        try:
            operation = uuid.UUID(operation_id)
            invalidation = uuid.UUID(invalidation_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("music invalidation is invalid") from exc
        if (
            str(operation) != operation_id or str(invalidation) != invalidation_id
            or not isinstance(album_id, str) or not album_id.isdecimal() or int(album_id) <= 0
            or not isinstance(selector_fingerprint, str) or len(selector_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in selector_fingerprint)
            or not isinstance(canonical_hash, str) or len(canonical_hash) != 40 or any(char not in "0123456789abcdef" for char in canonical_hash)
            or reason not in {"metadata-timeout", "no-peer-progress", "no-progress"}
        ):
            raise ValueError("music invalidation is invalid")
        cycle = self._albums.get(album_id)
        if cycle is None:
            return False
        if invalidation_id in cycle.applied_invalidation_ids:
            return True
        if cycle.active_operation_id != operation_id or cycle.active_selector_fingerprint != selector_fingerprint:
            return False
        if cycle.active_canonical_hash is not None and cycle.active_canonical_hash != canonical_hash:
            return False
        cycle.rejected_selectors.add(selector_fingerprint)
        if cycle.active_canonical_hash is not None:
            cycle.rejected_hashes.add(cycle.active_canonical_hash)
        cycle.rejected_hashes.add(canonical_hash)
        cycle.active_operation_id = None
        cycle.active_selector_fingerprint = None
        cycle.active_canonical_hash = None
        cycle.active_release_resource = None
        cycle.applied_invalidation_ids.add(invalidation_id)
        if cycle.cycle_candidate_limit is not None and cycle.cycle_candidates_used >= cycle.cycle_candidate_limit:
            cycle.last_completed_at = now
            cycle.completed_cycles += 1
            cycle.cycle_candidates_used = 0
            cycle.cycle_candidate_limit = None
        return True

    def record_budget_event(self, now: int) -> None:
        self._valid_now(now)
        self._budget_events.append(now)
        self._budget_events = [event for event in self._budget_events if event > now - DAY]

    def within_budget(self, policy: ReconciliationPolicy, *, now: int) -> bool:
        self._valid_now(now)
        return (
            sum(event > now - 3_600 for event in self._budget_events) < policy.max_searches_per_hour
            and sum(event > now - DAY for event in self._budget_events) < policy.max_searches_per_day
        )

    def record(self) -> dict[str, object]:
        return {
            "albums": [
                {
                    "album_id": album_id,
                    "released_at": cycle.released_at,
                    "last_completed_at": cycle.last_completed_at,
                    "completed_cycles": cycle.completed_cycles,
                    "terminal": cycle.terminal,
                    "cycle_candidates_used": cycle.cycle_candidates_used,
                    "active_operation_id": cycle.active_operation_id,
                    "active_selector_fingerprint": cycle.active_selector_fingerprint,
                    "active_canonical_hash": cycle.active_canonical_hash,
                    "active_release_resource": cycle.active_release_resource,
                    "rejected_selectors": sorted(cycle.rejected_selectors),
                    "rejected_hashes": sorted(cycle.rejected_hashes),
                    "cycle_candidate_limit": cycle.cycle_candidate_limit,
                    "applied_invalidation_ids": sorted(cycle.applied_invalidation_ids),
                }
                for album_id, cycle in sorted(self._albums.items(), key=lambda item: int(item[0]))
            ],
            "budget_events": list(self._budget_events),
        }

    @classmethod
    def from_record(cls, record: object) -> MusicScheduleState:
        if not isinstance(record, dict) or set(record) != {"albums", "budget_events"} or not isinstance(record["albums"], list) or not isinstance(record["budget_events"], list):
            raise ValueError("music schedule state is invalid")
        albums: dict[str, _AlbumCycle] = {}
        expected = {
            "album_id", "released_at", "last_completed_at", "completed_cycles", "terminal",
            "cycle_candidates_used", "active_operation_id", "active_selector_fingerprint",
            "active_canonical_hash", "active_release_resource", "rejected_selectors",
            "rejected_hashes", "cycle_candidate_limit", "applied_invalidation_ids",
        }
        for raw in record["albums"]:
            if not isinstance(raw, dict) or set(raw) != expected:
                raise ValueError("music schedule record is invalid")
            album_id = raw["album_id"]
            released_at = raw["released_at"]
            last_completed_at = raw["last_completed_at"]
            if (
                not isinstance(album_id, str) or not album_id.isdecimal() or int(album_id) <= 0
                or isinstance(released_at, bool) or not isinstance(released_at, int) or released_at < 0
                or last_completed_at is not None and (isinstance(last_completed_at, bool) or not isinstance(last_completed_at, int) or last_completed_at < released_at)
            ):
                raise ValueError("music schedule record is invalid")
            candidates, completed, limit = raw["cycle_candidates_used"], raw["completed_cycles"], raw["cycle_candidate_limit"]
            if (
                isinstance(candidates, bool) or not isinstance(candidates, int) or candidates < 0
                or isinstance(completed, bool) or not isinstance(completed, int) or completed < 0
                or not isinstance(raw["terminal"], bool)
                or limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or candidates > limit)
            ):
                raise ValueError("music schedule record is invalid")
            operation, selector, canonical_hash = raw["active_operation_id"], raw["active_selector_fingerprint"], raw["active_canonical_hash"]
            if (operation is None) != (selector is None) or canonical_hash is not None and (not isinstance(canonical_hash, str) or len(canonical_hash) != 40 or any(char not in "0123456789abcdef" for char in canonical_hash)):
                raise ValueError("music schedule record is invalid")
            if operation is not None:
                try:
                    parsed = uuid.UUID(operation)
                except (TypeError, ValueError) as exc:
                    raise ValueError("music schedule record is invalid") from exc
                if str(parsed) != operation or not isinstance(selector, str) or len(selector) != 64 or any(char not in "0123456789abcdef" for char in selector):
                    raise ValueError("music schedule record is invalid")
            active_resource = raw["active_release_resource"]
            if operation is None:
                if active_resource is not None:
                    raise ValueError("music schedule record is invalid")
            else:
                try:
                    encoded = json.dumps(active_resource, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                except (TypeError, ValueError) as exc:
                    raise ValueError("music schedule record is invalid") from exc
                if not isinstance(active_resource, dict) or hashlib.sha256(encoded).hexdigest() != selector:
                    raise ValueError("music schedule record is invalid")
            selectors, hashes, invalidations = raw["rejected_selectors"], raw["rejected_hashes"], raw["applied_invalidation_ids"]
            try:
                valid_invalidations = isinstance(invalidations, list) and len(set(invalidations)) == len(invalidations) and all(isinstance(value, str) and str(uuid.UUID(value)) == value for value in invalidations)
            except (TypeError, ValueError):
                valid_invalidations = False
            if (
                not isinstance(selectors, list) or not isinstance(hashes, list)
                or any(not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in selectors)
                or any(not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value) for value in hashes)
                or len(set(selectors)) != len(selectors) or len(set(hashes)) != len(hashes)
                or not valid_invalidations or album_id in albums
            ):
                raise ValueError("music schedule record is invalid")
            albums[album_id] = _AlbumCycle(
                released_at, last_completed_at, completed, raw["terminal"], candidates, operation, selector,
                canonical_hash, set(selectors), set(hashes), limit, set(invalidations),
                None if active_resource is None else dict(active_resource),
            )
        events = record["budget_events"]
        if any(isinstance(event, bool) or not isinstance(event, int) or event < 0 for event in events):
            raise ValueError("music schedule budget is invalid")
        return cls(albums, tuple(events))


class MusicSchedulerAdapter(Protocol):
    def missing_albums(self) -> tuple[LidarrAlbum, ...] | None: ...
    def album_releases(self, album_id: str) -> tuple[LidarrRelease, ...] | None: ...
    def first_approved_release(self, candidates: tuple[LidarrRelease, ...], policy: ReconciliationPolicy, health_policy: MusicFencePolicy, *, current_score: int) -> LidarrRelease | None: ...
    def release_from_record(self, resource: dict[str, object], album_id: str) -> LidarrRelease | None: ...


class MusicSelectedReleaseExecutor(Protocol):
    def execute_selected(self, intent: SearchIntent, release: LidarrRelease, *, now: int) -> str: ...


class MusicScheduler:
    """Schedule bounded Lidarr candidate cycles without video state reuse."""

    def __init__(
        self,
        state: MusicScheduleState,
        save: Callable[[MusicScheduleState], None],
        adapter: MusicSchedulerAdapter,
        policy: ReconciliationPolicy,
        health_policy: MusicFencePolicy,
        executor: MusicSelectedReleaseExecutor,
        on_imported: Callable[[str], bool] | None = None,
    ) -> None:
        self._state = state
        self._save = save
        self._adapter = adapter
        self._policy = policy
        self._health_policy = health_policy
        self._executor = executor
        self._on_imported = on_imported

    def apply_fence_invalidation(
        self,
        *,
        operation_id: str,
        album_id: str,
        selector_fingerprint: str,
        canonical_hash: str,
        invalidation_id: str,
        reason: str,
        now: int,
    ) -> bool:
        applied = self._state.apply_fence_invalidation(
            operation_id=operation_id, album_id=album_id, selector_fingerprint=selector_fingerprint,
            canonical_hash=canonical_hash, invalidation_id=invalidation_id, reason=reason, now=now,
        )
        if applied:
            self._save(self._state)
        return applied

    def run(self, *, now: int) -> SchedulerRunResult:
        searched = grabbed = no_candidate = unavailable = pending = 0
        grabs = 0
        active_candidates = self._state.active_candidates()
        missing_albums = self._adapter.missing_albums()
        if missing_albums is None:
            recovery_candidates = active_candidates
        else:
            missing_ids = {album.album_id for album in missing_albums}
            recovery_candidates = tuple(candidate for candidate in active_candidates if candidate[0].album_id in missing_ids)
            for album, operation_id, _, _ in active_candidates:
                if album.album_id not in missing_ids and (self._on_imported is None or self._on_imported(operation_id)) and self._state.confirm_imported(album.album_id):
                    self._save(self._state)
        for album, operation_id, selector_fingerprint, resource in recovery_candidates:
            grabs += 1
            try:
                release = self._adapter.release_from_record(resource, album.album_id)
            except Exception:
                release = None
            if release is None or release.selector_fingerprint != selector_fingerprint:
                unavailable += 1
                continue
            execution = self._executor.execute_selected(
                SearchIntent(operation_id, "lidarr", album.album_id, False, f"music-v1:{selector_fingerprint}"),
                release,
                now=now,
            )
            if execution == "bound":
                grabbed += 1
            elif execution == "pending":
                pending += 1
            else:
                unavailable += 1
        if missing_albums is None:
            return SchedulerRunResult(searched, grabbed, no_candidate, unavailable + 1, pending)
        for album in sorted((item for item in missing_albums if item.monitored), key=lambda item: (item.released_at, int(item.album_id))):
            if not self._state.due(album, self._policy, now=now, max_candidates=self._health_policy.max_candidates_per_cycle):
                continue
            if (
                searched >= self._policy.max_searches_per_run
                or grabs >= self._policy.max_grabs_per_run
                or not self._state.within_budget(self._policy, now=now)
            ):
                break
            try:
                releases = self._adapter.album_releases(album.album_id)
            except Exception:
                releases = None
            searched += 1
            self._state.record_budget_event(now)
            self._save(self._state)
            if releases is None:
                unavailable += 1
                continue
            allowed = tuple(
                release
                for release in releases
                if not self._state.was_rejected(album.album_id, release.selector_fingerprint, release.canonical_hash)
            )
            candidate = self._adapter.first_approved_release(allowed, self._policy, self._health_policy, current_score=0)
            if candidate is None:
                self._state.complete_cycle(album, self._policy, now=now)
                self._save(self._state)
                no_candidate += 1
                continue
            operation_id = str(uuid.uuid4())
            self._state.start_candidate(album, candidate, operation_id=operation_id, now=now, max_candidates=self._health_policy.max_candidates_per_cycle)
            self._save(self._state)
            execution = self._executor.execute_selected(
                SearchIntent(operation_id, "lidarr", album.album_id, False, f"music-v1:{candidate.selector_fingerprint}"),
                candidate,
                now=now,
            )
            grabs += 1
            if execution == "bound":
                grabbed += 1
            elif execution == "pending":
                pending += 1
            else:
                unavailable += 1
        return SchedulerRunResult(searched, grabbed, no_candidate, unavailable, pending)
