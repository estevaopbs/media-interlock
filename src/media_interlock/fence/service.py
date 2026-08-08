"""Fence orchestration preserving durable intent-before-effect ordering."""

from __future__ import annotations

import hashlib
from typing import Protocol

from ..contracts import Envelope
from .model import AcquisitionIntent, AdmissionDecision, FenceState, ReservationState


class ReservationStore(Protocol):
    def save(self, state: FenceState) -> None: ...


class QbittorrentControl(Protocol):
    def ready(self) -> bool: ...

    def add_stopped(self, source: str, reservation_id: str) -> tuple[str, int] | None: ...

    def observe_stopped(self, reservation_id: str) -> str | None: ...

    def resume(self, torrent_hash: str) -> bool: ...

    def observe_active(self, torrent_hash: str, reservation_id: str) -> bool | None: ...

    def terminal_observed(self, torrent_hash: str, reservation_id: str) -> bool | None: ...


class ProwlarrReadiness(Protocol):
    def ready(self) -> bool: ...


class FenceService:
    def __init__(self, state: FenceState, store: ReservationStore, qbittorrent: QbittorrentControl, prowlarr: ProwlarrReadiness | None) -> None:
        self._state = state
        self._store = store
        self._qbittorrent = qbittorrent
        self._prowlarr = prowlarr

    def _persist(self, candidate: FenceState) -> None:
        self._store.save(candidate)
        self._state.replace_with(candidate)

    def admit(self, intent: AcquisitionIntent, *, source: str, publisher_ready: bool) -> AdmissionDecision:
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != intent.source_fingerprint:
            return AdmissionDecision(False, "source_fingerprint_mismatch")
        try:
            qbittorrent_ready = self._qbittorrent.ready()
            prowlarr_ready = self._prowlarr is None or self._prowlarr.ready()
        except Exception:
            qbittorrent_ready = False
            prowlarr_ready = False
        candidate = self._state.clone()
        decision = candidate.admit(
            intent,
            qbittorrent_ready=qbittorrent_ready,
            prowlarr_ready=prowlarr_ready,
            publisher_ready=publisher_ready,
        )
        if decision.reason == "recovering":
            self.recover()
            candidate = self._state.clone()
            decision = candidate.admit(intent, qbittorrent_ready=qbittorrent_ready, prowlarr_ready=prowlarr_ready, publisher_ready=publisher_ready)
        if not decision.admitted or decision.reason in {"idempotent", "conflict"}:
            return decision
        self._persist(candidate)
        reservation = self._state.reservation(intent.operation_id)
        try:
            observed = self._qbittorrent.add_stopped(source, reservation.reservation_id)
        except Exception:
            observed = None
        if observed is None:
            return AdmissionDecision(False, "qbittorrent_effect_unobserved")
        torrent_hash, observed_bytes = observed
        candidate = self._state.clone()
        candidate.mark_qbittorrent_stopped(intent.operation_id, torrent_hash)
        within_capacity = candidate.account_observed_bytes(intent.operation_id, observed_bytes)
        self._persist(candidate)
        if not within_capacity:
            return AdmissionDecision(False, "capacity_exhausted_after_observation")
        candidate = self._state.clone()
        candidate.request_resume(intent.operation_id)
        self._persist(candidate)
        try:
            resumed = self._qbittorrent.resume(torrent_hash)
            active = resumed and self._qbittorrent.observe_active(torrent_hash, reservation.reservation_id) is True
        except Exception:
            active = False
        if not active:
            return AdmissionDecision(False, "qbittorrent_resume_unobserved")
        candidate = self._state.clone()
        candidate.mark_qbittorrent_active(intent.operation_id)
        self._persist(candidate)
        return decision

    def recover(self) -> None:
        """Reconcile durable effects; an uncertain add is never replayed."""
        for record in self._state.records():
            operation_id = record["operation_id"]
            reservation_id = record["reservation_id"]
            torrent_hash = record["torrent_hash"]
            state = record["state"]
            assert isinstance(operation_id, str) and isinstance(reservation_id, str)
            if state == ReservationState.INTENT_RECORDED.value:
                try:
                    observed = self._qbittorrent.observe_stopped(reservation_id)
                except Exception:
                    observed = None
                candidate = self._state.clone()
                if observed is not None and candidate.reconcile_qbittorrent_observation(operation_id, observed[0]):
                    candidate.account_observed_bytes(operation_id, observed[1])
                    self._persist(candidate)
                    if self._state.within_capacity:
                        candidate = self._state.clone()
                        candidate.request_resume(operation_id)
                        self._persist(candidate)
                        self._recover_resume(operation_id, reservation_id, observed[0])
            elif state == ReservationState.QBITTORRENT_STOPPED.value:
                if not self._state.within_capacity:
                    continue
                candidate = self._state.clone()
                candidate.request_resume(operation_id)
                self._persist(candidate)
                self._recover_resume(operation_id, reservation_id, torrent_hash)
            elif state == ReservationState.RESUME_INTENT_RECORDED.value:
                self._recover_resume(operation_id, reservation_id, torrent_hash)

    def _recover_resume(self, operation_id: str, reservation_id: str, torrent_hash: object) -> None:
        if not isinstance(torrent_hash, str):
            return
        try:
            active = self._qbittorrent.observe_active(torrent_hash, reservation_id)
            if active is False:
                active = self._qbittorrent.resume(torrent_hash) and self._qbittorrent.observe_active(torrent_hash, reservation_id)
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
            terminal = self._qbittorrent.terminal_observed(reservation.torrent_hash, reservation.reservation_id)
        except Exception:
            terminal = None
        if terminal is not True:
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
