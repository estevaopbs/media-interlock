"""Fence orchestration preserving durable intent-before-effect ordering."""

from __future__ import annotations

from typing import Mapping, Protocol

from ..contracts import Envelope
from .model import AdmissionDecision, FenceState, PreAdmissionIntent, QbittorrentActivityObservation, QbittorrentObservation, ReservationState


class ReservationStore(Protocol):
    def save(self, state: FenceState) -> None: ...


class QbittorrentControl(Protocol):
    def ready(self) -> bool: ...

    def observe_existing_stopped(self, torrent_hash: str, category: str) -> QbittorrentObservation: ...

    def apply_reservation_tag(self, torrent_hash: str, reservation_id: str) -> bool: ...

    def observe_tagged_stopped(self, torrent_hash: str, category: str, reservation_id: str) -> QbittorrentObservation: ...

    def resume(self, torrent_hash: str) -> bool: ...

    def observe_active(self, torrent_hash: str, reservation_id: str, category: str) -> QbittorrentActivityObservation: ...

    def terminal_observed(self, torrent_hash: str, reservation_id: str, category: str) -> QbittorrentActivityObservation: ...


class ProwlarrReadiness(Protocol):
    def ready(self) -> bool: ...


class FenceService:
    def __init__(self, state: FenceState, store: ReservationStore, qbittorrent: QbittorrentControl, prowlarr: ProwlarrReadiness | None, *, categories: Mapping[str, str] | None = None) -> None:
        self._state = state
        self._store = store
        self._qbittorrent = qbittorrent
        self._prowlarr = prowlarr
        self._categories = dict(categories or {})

    def _persist(self, candidate: FenceState) -> None:
        self._store.save(candidate)
        self._state.replace_with(candidate)

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
        if decision.admitted and decision.reason == "admitted":
            self._persist(candidate)
        return decision

    def bind_grab(self, operation_id: str, download_id: str, torrent_hash: str) -> bool:
        """Bind the Arr identity while operating qBittorrent by its canonical hash."""
        try:
            reservation = self._state.reservation(operation_id)
            category = self._categories[reservation.source]
        except (KeyError, AttributeError):
            return False
        if reservation.download_id is not None:
            if reservation.download_id != download_id or reservation.torrent_hash != torrent_hash:
                return False
            if reservation.state is ReservationState.GRAB_BOUND:
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
            observed_bytes = self._qbittorrent.observe_existing_stopped(torrent_hash, category)
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
            tagged = self._qbittorrent.apply_reservation_tag(torrent_hash, reservation.reservation_id)
            tagged_bytes = self._qbittorrent.observe_tagged_stopped(torrent_hash, category, reservation.reservation_id) if tagged else None
        except Exception:
            return False
        if tagged_bytes is None or tagged_bytes.kind != "observed" or tagged_bytes.observed_bytes is None:
            return False
        candidate = self._state.clone()
        try:
            within_capacity = candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=tagged_bytes.observed_bytes)
        except Exception:
            return False
        self._persist(candidate)
        if not within_capacity:
            return False
        candidate = self._state.clone()
        try:
            candidate.request_resume(operation_id)
        except Exception:
            return False
        self._persist(candidate)
        try:
            active = self._qbittorrent.resume(torrent_hash) and (activity := self._qbittorrent.observe_active(torrent_hash, reservation.reservation_id, category)).kind == "observed" and activity.active is True
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
                download_id = record["download_id"]
                if isinstance(download_id, str) and isinstance(torrent_hash, str):
                    self.bind_grab(operation_id, download_id, torrent_hash)
            elif state == ReservationState.TAG_INTENT_RECORDED.value:
                reservation = self._state.reservation(operation_id)
                category = self._categories.get(reservation.source)
                if category is None or not isinstance(torrent_hash, str):
                    continue
                try:
                    observed_bytes = self._qbittorrent.observe_tagged_stopped(torrent_hash, category, reservation_id)
                except Exception:
                    observed_bytes = None
                if observed_bytes is None or observed_bytes.kind != "observed" or observed_bytes.observed_bytes is None:
                    continue
                candidate = self._state.clone()
                try:
                    within_capacity = candidate.mark_qbittorrent_tagged(operation_id, observed_bytes=observed_bytes.observed_bytes)
                except Exception:
                    continue
                self._persist(candidate)
                if within_capacity:
                    candidate = self._state.clone()
                    candidate.request_resume(operation_id)
                    self._persist(candidate)
                    self._recover_resume(operation_id, reservation_id, torrent_hash, reservation.source)
            elif state == ReservationState.QBITTORRENT_STOPPED.value:
                if not self._state.within_capacity:
                    continue
                candidate = self._state.clone()
                candidate.request_resume(operation_id)
                self._persist(candidate)
                self._recover_resume(operation_id, reservation_id, torrent_hash, self._state.reservation(operation_id).source)
            elif state == ReservationState.RESUME_INTENT_RECORDED.value:
                self._recover_resume(operation_id, reservation_id, torrent_hash, self._state.reservation(operation_id).source)

    def _recover_resume(self, operation_id: str, reservation_id: str, torrent_hash: object, source: str) -> None:
        if not isinstance(torrent_hash, str):
            return
        category = self._categories.get(source)
        if category is None:
            return
        try:
            observed = self._qbittorrent.observe_active(torrent_hash, reservation_id, category)
            active = observed.kind == "observed" and observed.active is True
            if observed.kind == "observed" and observed.active is False:
                retried = self._qbittorrent.resume(torrent_hash) and self._qbittorrent.observe_active(torrent_hash, reservation_id, category)
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
            category = self._categories.get(reservation.source)
            terminal = None if category is None else self._qbittorrent.terminal_observed(reservation.torrent_hash, reservation.reservation_id, category)
        except Exception:
            terminal = None
        if terminal is None or terminal.kind != "observed" or terminal.active is not True:
            return None
        candidate = self._state.clone()
        envelope = candidate.complete(operation_id)
        self._persist(candidate)
        return envelope

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
