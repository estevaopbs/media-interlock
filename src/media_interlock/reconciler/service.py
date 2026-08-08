"""One-shot durable Arr release handoff without local ranking or blind replay."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..adapters.arr import ArrGrabObservation, ArrRelease
from ..fence.model import AdmissionDecision, PreAdmissionIntent
from .model import GrabIntent, ReconciliationState, SearchIntent


class ReconciliationStore(Protocol):
    def save(self, state: ReconciliationState) -> None: ...


class ArrReleaseControl(Protocol):
    def stopped_qbittorrent_client(self, category: str) -> bool: ...
    def history_watermark(self) -> int | None: ...
    def first_approved_release(self, entity_id: str) -> ArrRelease | None: ...
    def grab_release(self, release: ArrRelease) -> bool: ...
    def observe_grab(self, entity_id: str, release: ArrRelease, *, watermark: int) -> ArrGrabObservation: ...


class FencePreAdmission(Protocol):
    def pre_admit(self, intent: PreAdmissionIntent) -> AdmissionDecision: ...
    def bind_grab(self, operation_id: str, download_id: str, torrent_hash: str) -> bool: ...


class ReconcilerService:
    def __init__(self, state: ReconciliationState, store: ReconciliationStore, adapters: Mapping[str, ArrReleaseControl], fence: FencePreAdmission, categories: Mapping[str, str]) -> None:
        self._state = state
        self._store = store
        self._adapters = adapters
        self._fence = fence
        self._categories = categories

    def _persist(self) -> None:
        self._store.save(self._state)

    def recover(self, *, now: int) -> list[str]:
        """Resume durable pre-POST work; observe possible POST effects first."""
        results: list[str] = []
        for intent in self._state.intents():
            if self._state.observed(intent.operation_id):
                continue
            adapter = self._adapters.get(intent.source)
            category = self._categories.get(intent.source)
            if adapter is None or category is None:
                results.append("unavailable")
                continue
            try:
                grab = self._state.grab_intent(intent.operation_id)
            except KeyError:
                try:
                    if not adapter.stopped_qbittorrent_client(category):
                        results.append("inhibited")
                        continue
                    watermark = adapter.history_watermark()
                    release = adapter.first_approved_release(intent.entity_id)
                except Exception:
                    results.append("unavailable")
                    continue
                if watermark is None or release is None:
                    results.append("inhibited")
                    continue
                try:
                    grab = GrabIntent(intent.operation_id, intent.source, intent.entity_id, release.selector_fingerprint, release.expected_bytes, watermark, release.resource)
                except ValueError:
                    results.append("inhibited")
                    continue
                self._state.record_grab_intent(grab)
                self._persist()
            if not self._state.grab_attempted(intent.operation_id):
                try:
                    if not adapter.stopped_qbittorrent_client(category):
                        results.append("inhibited")
                        continue
                    admission = self._fence.pre_admit(PreAdmissionIntent(intent.operation_id, intent.source, intent.entity_id, grab.selector_fingerprint, grab.expected_bytes, str(grab.watermark)))
                except Exception:
                    results.append("unavailable")
                    continue
                if not admission.admitted:
                    results.append("inhibited")
                    continue
                self._state.mark_grab_attempted(intent.operation_id)
                self._persist()
                try:
                    adapter.grab_release(ArrRelease(dict(grab.release_resource), grab.selector_fingerprint, grab.expected_bytes))
                except Exception:
                    pass
            release = ArrRelease(dict(grab.release_resource), grab.selector_fingerprint, grab.expected_bytes)
            try:
                observation = adapter.observe_grab(intent.entity_id, release, watermark=grab.watermark)
            except Exception:
                results.append("unavailable")
                continue
            if observation.kind != "observed" or observation.download_id is None or observation.torrent_hash is None:
                results.append("pending")
                continue
            try:
                bound = self._fence.bind_grab(intent.operation_id, observation.download_id, observation.torrent_hash)
            except Exception:
                results.append("unavailable")
                continue
            if not bound:
                results.append("pending")
                continue
            self._state.mark_observed(intent.operation_id, completed=True, now=now)
            self._persist()
            results.append("bound")
        return results

    def execute(self, intent: SearchIntent, *, now: int) -> str:
        """Perform at most one durable release handoff; pending is never success."""
        try:
            existing = self._state.intent(intent.operation_id)
        except KeyError:
            self._state.record_intent(intent, now=now)
            self._persist()
        else:
            if existing != intent:
                return "inhibited"
        adapter = self._adapters.get(intent.source)
        category = self._categories.get(intent.source)
        if adapter is None or category is None:
            return "inhibited"
        try:
            if not adapter.stopped_qbittorrent_client(category):
                return "inhibited"
        except Exception:
            return "unavailable"
        try:
            grab = self._state.grab_intent(intent.operation_id)
        except KeyError:
            try:
                watermark = adapter.history_watermark()
                release = adapter.first_approved_release(intent.entity_id)
            except Exception:
                return "unavailable"
            if watermark is None or release is None:
                return "inhibited"
            try:
                grab = GrabIntent(intent.operation_id, intent.source, intent.entity_id, release.selector_fingerprint, release.expected_bytes, watermark, release.resource)
            except ValueError:
                return "inhibited"
            self._state.record_grab_intent(grab)
            self._persist()
        release = ArrRelease(dict(grab.release_resource), grab.selector_fingerprint, grab.expected_bytes)
        if not self._state.grab_attempted(intent.operation_id):
            try:
                admission = self._fence.pre_admit(PreAdmissionIntent(intent.operation_id, intent.source, intent.entity_id, grab.selector_fingerprint, grab.expected_bytes, str(grab.watermark)))
            except Exception:
                return "unavailable"
            if not admission.admitted:
                return "inhibited"
            self._state.mark_grab_attempted(intent.operation_id)
            self._persist()
            try:
                adapter.grab_release(release)
            except Exception:
                pass
        try:
            observation = adapter.observe_grab(intent.entity_id, release, watermark=grab.watermark)
        except Exception:
            return "unavailable"
        if observation.kind != "observed" or observation.download_id is None or observation.torrent_hash is None:
            return "pending"
        try:
            bound = self._fence.bind_grab(intent.operation_id, observation.download_id, observation.torrent_hash)
        except Exception:
            return "unavailable"
        if not bound:
            return "pending"
        self._state.mark_observed(intent.operation_id, completed=True, now=now)
        self._persist()
        return "bound"
