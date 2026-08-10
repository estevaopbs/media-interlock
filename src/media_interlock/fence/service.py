"""Fence orchestration preserving durable intent-before-effect ordering."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Protocol
import uuid

from ..adapters.arr import ArrExternalGrab
from ..contracts import Envelope, post_pnr_adoption_receipt, post_pnr_historical_adoption_receipt
from .headroom import PhysicalHeadroom
from .model import AdmissionDecision, ExternalAdoptionIntent, FenceState, PostPnrAdoptionIntent, PostPnrHistoricalAdoptionIntent, PreAdmissionIntent, QbittorrentActivityObservation, QbittorrentObservation, ReservationState


class ReservationStore(Protocol):
    def save(self, state: FenceState) -> None: ...


class QbittorrentControl(Protocol):
    def ready(self) -> bool: ...

    def observe_existing_stopped(self, torrent_hash: str, category: str, *, save_path: Path) -> QbittorrentObservation: ...

    def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool: ...

    def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str, *, save_path: Path) -> QbittorrentObservation: ...

    def resume(self, torrent_hash: str) -> bool: ...

    def pause(self, torrent_hash: str) -> bool: ...

    def observe_active(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation: ...

    def terminal_observed(self, torrent_hash: str, reservation_id: str, category: str, *, save_path: Path) -> QbittorrentActivityObservation: ...


class ProwlarrReadiness(Protocol):
    def ready(self) -> bool: ...


class MutationLease(Protocol):
    def acquire(self): ...


class ArrExternalObserver(Protocol):
    def external_grabs_after(self, watermark: int, *, category: str, download_client_id: int): ...

    def sealed_external_grab(self, entity_id: str, torrent_hash: str, *, category: str, download_client_id: int): ...

    def sealed_historical_external_grab(self, entity_ids: tuple[str, ...], torrent_hash: str, *, category: str, download_client_id: int): ...


@dataclass(frozen=True)
class FenceSource:
    category: str
    qbittorrent_save_path: Path
    download_client_id: int = 1
    download_pool: str | None = None
    staging_pool: str | None = None
    canonical_pool: str | None = None


class FenceService:
    def __init__(self, state: FenceState, store: ReservationStore, qbittorrent: QbittorrentControl, prowlarr: ProwlarrReadiness | None, *, sources: Mapping[str, FenceSource], observers: Mapping[str, ArrExternalObserver] | None = None, headroom: PhysicalHeadroom | None = None, lease: MutationLease | None = None, resume_ready: Callable[[], bool] | None = None) -> None:
        self._state = state
        self._store = store
        self._qbittorrent = qbittorrent
        self._prowlarr = prowlarr
        self._sources = dict(sources)
        self._observers = {} if observers is None else dict(observers)
        self._headroom = headroom
        self._lease = lease
        self._resume_ready = resume_ready

    def _hold_mutation_lease(self):
        return nullcontext() if self._lease is None else self._lease.acquire()

    def _persist(self, candidate: FenceState) -> None:
        self._store.save(candidate)
        self._state.replace_with(candidate)

    def _physical_ready(self, state: FenceState) -> bool:
        if self._headroom is None:
            return True
        pools: dict[str, tuple[str, str, str]] = {}
        for name, source in self._sources.items():
            if not isinstance(source.download_pool, str) or not isinstance(source.staging_pool, str) or not isinstance(source.canonical_pool, str):
                return False
            pools[name] = (source.download_pool, source.staging_pool, source.canonical_pool)
        return self._headroom.allows(state.records(), pools)

    def _ready_for_resume(self, state: FenceState) -> bool:
        if not state.within_capacity or not self._physical_ready(state):
            return False
        if self._resume_ready is None:
            return True
        try:
            return self._resume_ready()
        except Exception:
            return False

    def pre_admit(self, intent: PreAdmissionIntent, *, publisher_ready: bool) -> AdmissionDecision:
        """Reserve capacity before Arr is allowed to issue the exact release grab."""
        try:
            qbittorrent_ready = self._qbittorrent.ready()
            prowlarr_ready = self._prowlarr is None or self._prowlarr.ready()
        except Exception:
            qbittorrent_ready = False
            prowlarr_ready = False
        candidate = self._state.clone()
        decision = candidate.pre_admit(intent, qbittorrent_ready=qbittorrent_ready, prowlarr_ready=prowlarr_ready, publisher_ready=publisher_ready)
        if decision.admitted and decision.reason == "admitted" and not self._physical_ready(candidate):
            return AdmissionDecision(False, "physical_headroom")
        if decision.admitted and decision.reason == "admitted":
            self._persist(candidate)
        return decision

    @staticmethod
    def _external_fingerprint(source: str, grab: ArrExternalGrab) -> str:
        payload = {
            "download_id": grab.download_id,
            "entity_id": grab.entity_id,
            "expected_bytes": grab.expected_bytes,
            "history_id": grab.history_id,
            "source": source,
            "torrent_hash": grab.torrent_hash,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def _adopt_external_grab(self, source_name: str, grab: ArrExternalGrab, *, publisher_ready: bool) -> bool:
        fingerprint = self._external_fingerprint(source_name, grab)
        operation_id = self._state.operation_for_observation(fingerprint) or str(uuid.uuid4())
        try:
            qbittorrent_ready = self._qbittorrent.ready()
        except Exception:
            qbittorrent_ready = False
        candidate = self._state.clone()
        decision = candidate.adopt_external(ExternalAdoptionIntent(operation_id, source_name, grab.entity_id, grab.download_id, grab.torrent_hash, grab.expected_bytes, grab.history_id, fingerprint), qbittorrent_ready=qbittorrent_ready, publisher_ready=publisher_ready)
        if decision.admitted and decision.reason == "admitted" and not self._physical_ready(candidate):
            return False
        if not decision.admitted:
            return False
        if decision.reason == "admitted":
            self._persist(candidate)
        return self.bind_grab(operation_id, grab.download_id, grab.torrent_hash)

    def poll_external(self, *, publisher_ready: bool) -> bool:
        """Adopt only post-baseline public Arr observations, one source at a time."""
        for source_name, source in self._sources.items():
            observer = self._observers.get(source_name)
            if observer is None:
                continue
            watermark = self._state.watermark(source_name)
            try:
                observation = observer.external_grabs_after(0 if watermark is None else watermark, category=source.category, download_client_id=source.download_client_id)
            except Exception:
                return False
            if observation is None:
                return False
            if watermark is None:
                candidate = self._state.clone()
                try:
                    candidate.record_watermark(source_name, observation.watermark)
                except Exception:
                    return False
                self._persist(candidate)
                continue
            if observation.watermark < watermark:
                return False
            for grab in observation.grabs:
                if not self._adopt_external_grab(source_name, grab, publisher_ready=publisher_ready):
                    return False
            candidate = self._state.clone()
            try:
                candidate.record_watermark(source_name, observation.watermark)
            except Exception:
                return False
            self._persist(candidate)
        return True

    def post_pnr_adopt(self, *, operation_id: str, source: str, download_client_id: int, entity_id: str, torrent_hash: str, category: str, save_path: str) -> AdmissionDecision:
        """Claim exactly one pre-existing Arr eligibility; this call is the post-PNR authority boundary."""
        profile = self._sources.get(source)
        observer = self._observers.get(source)
        if profile is None or observer is None or profile.download_client_id != download_client_id or profile.category != category or str(profile.qbittorrent_save_path) != save_path:
            return AdmissionDecision(False, "identity_drift")
        existing = self._state.post_pnr_adoption(operation_id)
        if existing is not None:
            requested = (source, download_client_id, entity_id, torrent_hash, category, save_path)
            immutable = (existing.source, existing.download_client_id, existing.entity_id, existing.torrent_hash, existing.category, existing.save_path)
            if requested != immutable:
                return AdmissionDecision(False, "conflict")
            return self._claim_post_pnr(operation_id)
        try:
            grab = observer.sealed_external_grab(entity_id, torrent_hash, category=category, download_client_id=download_client_id)
            qbittorrent_ready = self._qbittorrent.ready()
        except Exception:
            return AdmissionDecision(False, "unavailable")
        if grab is None or grab.entity_id != entity_id or grab.torrent_hash != torrent_hash:
            return AdmissionDecision(False, "identity_ambiguous")
        intent = PostPnrAdoptionIntent(operation_id, source, download_client_id, entity_id, torrent_hash, category, save_path, grab.expected_bytes, grab.history_id)
        candidate = self._state.clone()
        decision = candidate.adopt_post_pnr(intent, qbittorrent_ready=qbittorrent_ready)
        if not decision.admitted:
            return decision
        if decision.reason == "admitted":
            if not self._physical_ready(candidate):
                return AdmissionDecision(False, "physical_headroom")
            self._persist(candidate)
        return self._claim_post_pnr(operation_id)

    def post_pnr_historical_adopt(self, *, operation_id: str, source: str, download_client_id: int, entity_ids: tuple[str, ...], torrent_hash: str, category: str, save_path: str) -> AdmissionDecision:
        """Claim a pre-watermark singleton or Sonarr pack by exact public evidence."""
        profile = self._sources.get(source)
        observer = self._observers.get(source)
        if profile is None or observer is None or profile.download_client_id != download_client_id or profile.category != category or str(profile.qbittorrent_save_path) != save_path:
            return AdmissionDecision(False, "identity_drift")
        existing = self._state.post_pnr_historical_adoption(operation_id)
        if existing is not None:
            requested = (source, download_client_id, entity_ids, torrent_hash, category, save_path)
            immutable = (existing.source, existing.download_client_id, existing.entity_ids, existing.torrent_hash, existing.category, existing.save_path)
            if requested != immutable:
                return AdmissionDecision(False, "conflict")
            return self._claim_post_pnr(operation_id)
        try:
            qbittorrent_ready = self._qbittorrent.ready()
            stopped = self._qbittorrent.observe_existing_stopped(torrent_hash, category, save_path=profile.qbittorrent_save_path)
            grab = observer.sealed_historical_external_grab(entity_ids, torrent_hash, category=category, download_client_id=download_client_id)
        except Exception:
            return AdmissionDecision(False, "unavailable")
        if stopped.kind != "observed" or stopped.observed_bytes is None or grab is None:
            return AdmissionDecision(False, "identity_ambiguous")
        if grab.entity_ids != entity_ids or grab.torrent_hash != torrent_hash or grab.queue_expected_bytes not in {None, stopped.observed_bytes}:
            return AdmissionDecision(False, "identity_ambiguous")
        intent = PostPnrHistoricalAdoptionIntent(operation_id, source, download_client_id, entity_ids, torrent_hash, category, save_path, stopped.observed_bytes, grab.history_ids)
        candidate = self._state.clone()
        decision = candidate.adopt_post_pnr_historical(intent, qbittorrent_ready=qbittorrent_ready)
        if not decision.admitted:
            return decision
        if decision.reason == "admitted":
            if not self._physical_ready(candidate):
                return AdmissionDecision(False, "physical_headroom")
            self._persist(candidate)
        return self._claim_post_pnr(operation_id)

    def _claim_post_pnr(self, operation_id: str) -> AdmissionDecision:
        try:
            reservation = self._state.reservation(operation_id)
            intent = self._state.post_pnr_adoption(operation_id)
            source = self._sources[reservation.source]
        except (KeyError, AttributeError):
            return AdmissionDecision(False, "unavailable")
        if intent is None and self._state.post_pnr_historical_adoption(operation_id) is None:
            return AdmissionDecision(False, "unavailable")
        if reservation.state is ReservationState.QBITTORRENT_STOPPED:
            return AdmissionDecision(True, "adopted")
        if reservation.state is ReservationState.TAG_INTENT_RECORDED:
            return AdmissionDecision(self._recover_post_pnr_tag(operation_id), "adopted" if self._state.reservation(operation_id).state is ReservationState.QBITTORRENT_STOPPED else "pending")
        if reservation.state is not ReservationState.GRAB_BOUND:
            return AdmissionDecision(False, "conflict")
        candidate = self._state.clone()
        try:
            candidate.request_tag(operation_id)
            self._persist(candidate)
            with self._hold_mutation_lease():
                stopped = self._qbittorrent.observe_existing_stopped(reservation.torrent_hash, source.category, save_path=source.qbittorrent_save_path)
                if stopped.kind != "observed" or (self._state.post_pnr_historical_adoption(operation_id) is not None and stopped.observed_bytes != reservation.requested_bytes):
                    return AdmissionDecision(False, "pending")
                tagged = self._qbittorrent.apply_reservation_tag(reservation.torrent_hash, reservation.reservation_id)
                readback = self._qbittorrent.observe_tagged_stopped(reservation.torrent_hash, source.category, reservation.reservation_id, save_path=source.qbittorrent_save_path) if tagged else None
                if readback is None or readback.kind != "observed" or readback.observed_bytes is None or (self._state.post_pnr_historical_adoption(operation_id) is not None and readback.observed_bytes != reservation.requested_bytes):
                    return AdmissionDecision(False, "pending")
                candidate = self._state.clone()
                candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=readback.observed_bytes, remaining_download_bytes=readback.remaining_bytes)
                self._persist(candidate)
        except Exception:
            return AdmissionDecision(False, "pending")
        return AdmissionDecision(True, "adopted")

    def _recover_post_pnr_tag(self, operation_id: str) -> bool:
        try:
            reservation = self._state.reservation(operation_id)
            source = self._sources[reservation.source]
            if not self._state.is_post_pnr_adoption(operation_id) or reservation.state is not ReservationState.TAG_INTENT_RECORDED or reservation.torrent_hash is None:
                return False
            with self._hold_mutation_lease():
                observed = self._qbittorrent.observe_tagged_stopped(reservation.torrent_hash, source.category, reservation.reservation_id, save_path=source.qbittorrent_save_path)
                if observed.kind != "observed" or observed.observed_bytes is None or (self._state.post_pnr_historical_adoption(operation_id) is not None and observed.observed_bytes != reservation.requested_bytes):
                    return False
                candidate = self._state.clone()
                candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=observed.observed_bytes, remaining_download_bytes=observed.remaining_bytes)
                self._persist(candidate)
        except Exception:
            return False
        return True

    def post_pnr_receipt(self, operation_id: str) -> Envelope | None:
        try:
            intent = self._state.post_pnr_adoption(operation_id)
            reservation = self._state.reservation(operation_id)
        except KeyError:
            return None
        if intent is None or reservation.state is not ReservationState.QBITTORRENT_STOPPED:
            return None
        return post_pnr_adoption_receipt(operation_id, source=intent.source, download_client_id=intent.download_client_id, entity_id=intent.entity_id, torrent_hash=intent.torrent_hash, category=intent.category, save_path=intent.save_path, fence_reservation_id=reservation.reservation_id)

    def post_pnr_historical_receipt(self, operation_id: str) -> Envelope | None:
        try:
            intent = self._state.post_pnr_historical_adoption(operation_id)
            reservation = self._state.reservation(operation_id)
        except KeyError:
            return None
        if intent is None or reservation.state is not ReservationState.QBITTORRENT_STOPPED:
            return None
        return post_pnr_historical_adoption_receipt(operation_id, source=intent.source, download_client_id=intent.download_client_id, entity_ids=intent.entity_ids, torrent_hash=intent.torrent_hash, category=intent.category, save_path=intent.save_path, fence_reservation_id=reservation.reservation_id)

    def quiesce(self, *, enabled: bool) -> bool:
        """Durably pause or reopen only hashes already owned by this ledger."""
        if enabled:
            if not self._state.quiescing:
                candidate = self._state.clone()
                candidate.begin_quiescence()
                self._persist(candidate)
            success = True
            for record in self._state.records():
                if record["state"] != ReservationState.QBITTORRENT_ACTIVE.value:
                    continue
                operation_id = record["operation_id"]
                assert isinstance(operation_id, str)
                candidate = self._state.clone()
                try:
                    candidate.request_pause(operation_id)
                except Exception:
                    success = False
                    continue
                self._persist(candidate)
                success = self._recover_pause(operation_id) and success
            return success
        if self._state.quiescing:
            candidate = self._state.clone()
            candidate.end_quiescence()
            self._persist(candidate)
        success = True
        for record in self._state.records():
            if record["state"] != ReservationState.QBITTORRENT_PAUSED.value:
                continue
            operation_id, reservation_id, torrent_hash, source = record["operation_id"], record["reservation_id"], record["torrent_hash"], record["source"]
            if not isinstance(operation_id, str) or not isinstance(reservation_id, str) or not isinstance(source, str) or not self._ready_for_resume(self._state):
                success = False
                continue
            candidate = self._state.clone()
            try:
                candidate.request_resume(operation_id)
            except Exception:
                success = False
                continue
            self._persist(candidate)
            self._recover_resume(operation_id, reservation_id, torrent_hash, source)
            try:
                success = self._state.reservation(operation_id).state is ReservationState.QBITTORRENT_ACTIVE and success
            except KeyError:
                success = False
        return success

    def _recover_pause(self, operation_id: str) -> bool:
        try:
            reservation = self._state.reservation(operation_id)
            source = self._sources[reservation.source]
            if reservation.state is not ReservationState.PAUSE_INTENT_RECORDED or reservation.torrent_hash is None:
                return False
            with self._hold_mutation_lease():
                observed = self._qbittorrent.observe_active(reservation.torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)
                paused = observed.kind == "observed" and observed.active is False
                if observed.kind == "observed" and observed.active is True:
                    paused = self._qbittorrent.pause(reservation.torrent_hash) and (after := self._qbittorrent.observe_active(reservation.torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)).kind == "observed" and after.active is False
        except Exception:
            return False
        if not paused:
            return False
        candidate = self._state.clone()
        try:
            candidate.mark_qbittorrent_paused(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        return True

    def bind_grab(self, operation_id: str, download_id: str, torrent_hash: str) -> bool:
        """Bind the Arr identity while operating qBittorrent by its canonical hash."""
        try:
            reservation = self._state.reservation(operation_id)
            source = self._sources[reservation.source]
        except (KeyError, AttributeError):
            return False
        if reservation.download_id is not None:
            if reservation.download_id != download_id or reservation.torrent_hash != torrent_hash:
                return False
            if reservation.state is ReservationState.PRE_ADMITTED:
                pass
            elif reservation.state is ReservationState.GRAB_BOUND:
                # Tag intent was not yet durable, so re-drive only the observed
                # stopped proof and the subsequent intent-before-tag sequence.
                pass
            else:
                if reservation.state in {ReservationState.TAG_INTENT_RECORDED, ReservationState.QBITTORRENT_STOPPED, ReservationState.RESUME_INTENT_RECORDED}:
                    self.recover()
                return reservation.state in {
                    ReservationState.TAG_INTENT_RECORDED,
                    ReservationState.QBITTORRENT_STOPPED,
                    ReservationState.RESUME_INTENT_RECORDED,
                    ReservationState.QBITTORRENT_ACTIVE,
                    ReservationState.TERMINAL,
                    ReservationState.RELEASED,
                }
        try:
            observed_bytes = self._qbittorrent.observe_existing_stopped(torrent_hash, source.category, save_path=source.qbittorrent_save_path)
        except Exception:
            return False
        if observed_bytes.kind != "observed" or observed_bytes.observed_bytes is None:
            return False
        candidate = self._state.clone()
        try:
            candidate.bind_observed_grab(operation_id, download_id=download_id, torrent_hash=torrent_hash)
        except Exception:
            return False
        self._persist(candidate)
        candidate = self._state.clone()
        try:
            candidate.request_tag(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        try:
            with self._hold_mutation_lease():
                # The first stopped observation may predate prospective intent;
                # re-observe under the shared lease before the exact mutation.
                stopped = self._qbittorrent.observe_existing_stopped(torrent_hash, source.category, save_path=source.qbittorrent_save_path)
                if stopped.kind != "observed":
                    return False
                tagged = self._qbittorrent.apply_reservation_tag(torrent_hash, reservation.reservation_id)
                tagged_bytes = self._qbittorrent.observe_tagged_stopped(torrent_hash, source.category, reservation.reservation_id, save_path=source.qbittorrent_save_path) if tagged else None
                if tagged_bytes is None or tagged_bytes.kind != "observed" or tagged_bytes.observed_bytes is None:
                    return False
                candidate = self._state.clone()
                within_capacity = candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=tagged_bytes.observed_bytes, remaining_download_bytes=tagged_bytes.remaining_bytes)
                # The effect is now known; make its durable ownership transition
                # visible before another lease holder can observe the hash.
                self._persist(candidate)
        except Exception:
            return False
        if not within_capacity or not self._physical_ready(self._state):
            return False
        candidate = self._state.clone()
        try:
            candidate.request_resume(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        try:
            with self._hold_mutation_lease():
                stopped = self._qbittorrent.observe_active(torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)
                active = stopped.kind == "observed" and stopped.active is True
                if stopped.kind == "observed" and stopped.active is False:
                    active = self._qbittorrent.resume(torrent_hash) and (activity := self._qbittorrent.observe_active(torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)).kind == "observed" and activity.active is True
        except Exception:
            active = False
        if not active:
            return False
        candidate = self._state.clone()
        try:
            candidate.mark_qbittorrent_active(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        return True

    def recover(self) -> None:
        """Reconcile durable effects; an uncertain add is never replayed."""
        for record in self._state.records():
            operation_id = record["operation_id"]
            reservation_id = record["reservation_id"]
            torrent_hash = record["torrent_hash"]
            state = record["state"]
            assert isinstance(operation_id, str) and isinstance(reservation_id, str)
            if state == ReservationState.GRAB_BOUND.value:
                if self._state.is_post_pnr_adoption(operation_id):
                    self._claim_post_pnr(operation_id)
                    continue
                download_id = record["download_id"]
                if isinstance(download_id, str) and isinstance(torrent_hash, str):
                    self.bind_grab(operation_id, download_id, torrent_hash)
            elif state == ReservationState.TAG_INTENT_RECORDED.value:
                if self._state.is_post_pnr_adoption(operation_id):
                    self._recover_post_pnr_tag(operation_id)
                    continue
                reservation = self._state.reservation(operation_id)
                source = self._sources.get(reservation.source)
                if source is None or not isinstance(torrent_hash, str):
                    continue
                try:
                    with self._hold_mutation_lease():
                        observed_bytes = self._qbittorrent.observe_tagged_stopped(torrent_hash, source.category, reservation_id, save_path=source.qbittorrent_save_path)
                        if observed_bytes.kind != "observed" or observed_bytes.observed_bytes is None:
                            continue
                        candidate = self._state.clone()
                        within_capacity = candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=observed_bytes.observed_bytes, remaining_download_bytes=observed_bytes.remaining_bytes)
                        self._persist(candidate)
                except Exception:
                    continue
                if within_capacity and self._ready_for_resume(self._state):
                    candidate = self._state.clone()
                    candidate.request_resume(operation_id)
                    self._persist(candidate)
                    self._recover_resume(operation_id, reservation_id, torrent_hash, reservation.source)
            elif state == ReservationState.QBITTORRENT_STOPPED.value:
                if self._state.is_post_pnr_adoption(operation_id):
                    continue
                if not self._ready_for_resume(self._state):
                    continue
                candidate = self._state.clone()
                candidate.request_resume(operation_id)
                self._persist(candidate)
                self._recover_resume(operation_id, reservation_id, torrent_hash, self._state.reservation(operation_id).source)
            elif state == ReservationState.RESUME_INTENT_RECORDED.value:
                if not self._state.quiescing:
                    self._recover_resume(operation_id, reservation_id, torrent_hash, self._state.reservation(operation_id).source)
            elif state == ReservationState.PAUSE_INTENT_RECORDED.value:
                self._recover_pause(operation_id)
            elif state == ReservationState.FREEZE_INTENT_RECORDED.value:
                self._recover_freeze(operation_id)

    def _recover_resume(self, operation_id: str, reservation_id: str, torrent_hash: object, source: str) -> None:
        if self._state.quiescing or not self._ready_for_resume(self._state) or not isinstance(torrent_hash, str):
            return
        profile = self._sources.get(source)
        if profile is None:
            return
        try:
            with self._hold_mutation_lease():
                observed = self._qbittorrent.observe_active(torrent_hash, reservation_id, profile.category, save_path=profile.qbittorrent_save_path)
                active = observed.kind == "observed" and observed.active is True
                if observed.kind == "observed" and observed.active is False:
                    retried = self._qbittorrent.resume(torrent_hash) and self._qbittorrent.observe_active(torrent_hash, reservation_id, profile.category, save_path=profile.qbittorrent_save_path)
                    active = retried.kind == "observed" and retried.active is True if isinstance(retried, QbittorrentActivityObservation) else False
        except Exception:
            active = None
        if active is True:
            candidate = self._state.clone()
            candidate.mark_qbittorrent_active(operation_id)
            self._persist(candidate)

    def observe(self, operation_id: str) -> Envelope | None:
        """Persist exact transfer completion before returning terminal observations."""
        try:
            reservation = self._state.reservation(operation_id)
        except KeyError:
            return None
        if reservation.state is ReservationState.TERMINAL:
            return self._state.terminal_observation(operation_id)
        if reservation.state is not ReservationState.QBITTORRENT_ACTIVE or reservation.torrent_hash is None:
            return None
        try:
            source = self._sources.get(reservation.source)
            terminal = None if source is None else self._qbittorrent.terminal_observed(reservation.torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)
        except Exception:
            terminal = None
        if terminal is None or terminal.kind != "observed" or terminal.active is not True:
            return None
        candidate = self._state.clone()
        envelope = candidate.complete(operation_id)
        self._persist(candidate)
        return envelope

    def freeze(self, operation_id: str) -> bool:
        """Durably stop one terminal owned hash for a Publisher hardlink copy."""
        try:
            reservation = self._state.reservation(operation_id)
        except KeyError:
            return False
        if reservation.state is ReservationState.QBITTORRENT_FROZEN:
            return True
        if reservation.state is not ReservationState.TERMINAL:
            return False
        candidate = self._state.clone()
        try:
            candidate.request_freeze(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        return self._recover_freeze(operation_id)

    def _recover_freeze(self, operation_id: str) -> bool:
        try:
            reservation = self._state.reservation(operation_id)
            source = self._sources[reservation.source]
            if reservation.state is not ReservationState.FREEZE_INTENT_RECORDED or reservation.torrent_hash is None:
                return reservation.state is ReservationState.QBITTORRENT_FROZEN
            with self._hold_mutation_lease():
                observed = self._qbittorrent.observe_active(reservation.torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)
                frozen = observed.kind == "observed" and observed.active is False
                if observed.kind == "observed" and observed.active is True:
                    frozen = self._qbittorrent.pause(reservation.torrent_hash) and (after := self._qbittorrent.observe_active(reservation.torrent_hash, reservation.reservation_id, source.category, save_path=source.qbittorrent_save_path)).kind == "observed" and after.active is False
        except Exception:
            return False
        if not frozen:
            return False
        candidate = self._state.clone()
        try:
            candidate.mark_qbittorrent_frozen(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        return True

    def complete(self, operation_id: str) -> Envelope:
        candidate = self._state.clone()
        terminal = candidate.complete(operation_id)
        self._persist(candidate)
        return terminal

    def accept_custody(self, receipt: Envelope) -> bool:
        candidate = self._state.clone()
        if not candidate.accept_custody(receipt):
            return False
        self._persist(candidate)
        return True
