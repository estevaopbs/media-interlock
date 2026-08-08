"""Pure fail-closed Fence admission and custody state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping

from ..contracts import ContractError, Envelope, acquisition_pre_admission, terminal_acquisition


def _torrent_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


class ReservationState(StrEnum):
    PRE_ADMITTED = "pre_admitted"
    GRAB_BOUND = "grab_bound"
    INTENT_RECORDED = "intent_recorded"
    QBITTORRENT_STOPPED = "qbittorrent_stopped"
    RESUME_INTENT_RECORDED = "resume_intent_recorded"
    QBITTORRENT_ACTIVE = "qbittorrent_active"
    TERMINAL = "terminal"
    RELEASED = "released"


@dataclass(frozen=True)
class FencePolicy:
    capacity_bytes: int
    max_inflight: int

    def __post_init__(self) -> None:
        if self.capacity_bytes <= 0 or self.max_inflight <= 0:
            raise ValueError("Fence policy bounds must be positive")


@dataclass(frozen=True)
class AcquisitionIntent:
    operation_id: str
    source: str
    upstream_id: str
    media_id: str
    bytes_reserved: int
    source_fingerprint: str


@dataclass(frozen=True)
class PreAdmissionIntent:
    operation_id: str
    source: str
    media_id: str
    selector_fingerprint: str
    expected_bytes: int
    watermark: str


@dataclass
class Reservation:
    operation_id: str
    reservation_id: str
    source: str
    upstream_id: str
    media_id: str
    bytes_reserved: int
    requested_bytes: int
    source_fingerprint: str
    state: ReservationState
    torrent_hash: str | None = None
    publisher_reservation_id: str | None = None
    selector_fingerprint: str | None = None
    watermark: str | None = None
    download_id: str | None = None


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str


class FenceState:
    """Single-writer state model; callers persist every transition before effects."""

    def __init__(self, policy: FencePolicy) -> None:
        self._policy = policy
        self._reservations: dict[str, Reservation] = {}

    @property
    def reserved_bytes(self) -> int:
        return sum(item.bytes_reserved for item in self._reservations.values() if item.state is not ReservationState.RELEASED)

    @property
    def within_capacity(self) -> bool:
        return self.reserved_bytes <= self._policy.capacity_bytes

    def reservation(self, operation_id: str) -> Reservation:
        return self._reservations[operation_id]

    def pre_admit(self, intent: PreAdmissionIntent, *, qbittorrent_ready: bool, prowlarr_ready: bool, publisher_ready: bool) -> AdmissionDecision:
        existing = self._reservations.get(intent.operation_id)
        if existing is not None:
            immutable = (existing.source, existing.media_id, existing.requested_bytes, existing.selector_fingerprint, existing.watermark)
            requested = (intent.source, intent.media_id, intent.expected_bytes, intent.selector_fingerprint, intent.watermark)
            if immutable != requested:
                return AdmissionDecision(False, "conflict")
            return AdmissionDecision(existing.state is not ReservationState.RELEASED, "idempotent")
        if not qbittorrent_ready:
            return AdmissionDecision(False, "qbittorrent_unready")
        if not prowlarr_ready:
            return AdmissionDecision(False, "prowlarr_unready")
        if not publisher_ready:
            return AdmissionDecision(False, "publisher_backpressure")
        active = [item for item in self._reservations.values() if item.state is not ReservationState.RELEASED]
        if len(active) >= self._policy.max_inflight:
            return AdmissionDecision(False, "concurrency_exhausted")
        if self.reserved_bytes + intent.expected_bytes > self._policy.capacity_bytes:
            return AdmissionDecision(False, "capacity_exhausted")
        try:
            acquisition_pre_admission(operation_id=intent.operation_id, source=intent.source, media_id=intent.media_id, selector_fingerprint=intent.selector_fingerprint, expected_bytes=intent.expected_bytes, watermark=intent.watermark)
        except ContractError:
            return AdmissionDecision(False, "invalid_intent")
        self._reservations[intent.operation_id] = Reservation(intent.operation_id, f"fence:{intent.operation_id}", intent.source, intent.media_id, intent.media_id, intent.expected_bytes, intent.expected_bytes, intent.selector_fingerprint, ReservationState.PRE_ADMITTED, selector_fingerprint=intent.selector_fingerprint, watermark=intent.watermark)
        return AdmissionDecision(True, "admitted")

    def bind_observed_grab(self, operation_id: str, *, download_id: str, torrent_hash: str) -> None:
        reservation = self.reservation(operation_id)
        if not isinstance(download_id, str) or not download_id or len(download_id) > 256 or any(character in download_id for character in "\x00/\\") or not _torrent_hash(torrent_hash):
            raise ContractError("observed Arr grab identity is invalid")
        if reservation.state is ReservationState.GRAB_BOUND:
            if reservation.download_id == download_id and reservation.torrent_hash == torrent_hash:
                return
            raise ContractError("observed Arr grab conflicts with reservation")
        if reservation.state is not ReservationState.PRE_ADMITTED:
            raise ContractError("observed Arr grab transition is invalid")
        reservation.download_id = download_id
        reservation.torrent_hash = torrent_hash
        reservation.state = ReservationState.GRAB_BOUND

    def admit(self, intent: AcquisitionIntent, *, qbittorrent_ready: bool, prowlarr_ready: bool, publisher_ready: bool) -> AdmissionDecision:
        existing = self._reservations.get(intent.operation_id)
        if existing is not None:
            immutable = (existing.source, existing.upstream_id, existing.media_id, existing.requested_bytes, existing.source_fingerprint)
            requested = (intent.source, intent.upstream_id, intent.media_id, intent.bytes_reserved, intent.source_fingerprint)
            if immutable != requested:
                return AdmissionDecision(False, "conflict")
            if existing.state in {ReservationState.INTENT_RECORDED, ReservationState.QBITTORRENT_STOPPED, ReservationState.RESUME_INTENT_RECORDED}:
                return AdmissionDecision(False, "recovering")
            return AdmissionDecision(existing.state is not ReservationState.RELEASED, "idempotent")
        if not qbittorrent_ready:
            return AdmissionDecision(False, "qbittorrent_unready")
        if not prowlarr_ready:
            return AdmissionDecision(False, "prowlarr_unready")
        if not publisher_ready:
            return AdmissionDecision(False, "publisher_backpressure")
        if any(item.state in {ReservationState.INTENT_RECORDED, ReservationState.RESUME_INTENT_RECORDED} for item in self._reservations.values()):
            return AdmissionDecision(False, "recovering")
        active = [item for item in self._reservations.values() if item.state is not ReservationState.RELEASED]
        if len(active) >= self._policy.max_inflight:
            return AdmissionDecision(False, "concurrency_exhausted")
        if intent.bytes_reserved <= 0 or not intent.source_fingerprint or self.reserved_bytes + intent.bytes_reserved > self._policy.capacity_bytes:
            return AdmissionDecision(False, "capacity_exhausted")
        try:
            terminal_acquisition(operation_id=intent.operation_id, fence_reservation_id=f"fence:{intent.operation_id}", source=intent.source, upstream_id=intent.upstream_id, media_id=intent.media_id, bytes_reserved=intent.bytes_reserved)
        except ContractError:
            return AdmissionDecision(False, "invalid_intent")
        self._reservations[intent.operation_id] = Reservation(intent.operation_id, f"fence:{intent.operation_id}", intent.source, intent.upstream_id, intent.media_id, intent.bytes_reserved, intent.bytes_reserved, intent.source_fingerprint, ReservationState.INTENT_RECORDED)
        return AdmissionDecision(True, "admitted")

    def mark_qbittorrent_stopped(self, operation_id: str, torrent_hash: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.INTENT_RECORDED or not _torrent_hash(torrent_hash):
            raise ContractError("qBittorrent observation transition is invalid")
        reservation.torrent_hash = torrent_hash
        reservation.state = ReservationState.QBITTORRENT_STOPPED

    def request_resume(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.QBITTORRENT_STOPPED or not reservation.torrent_hash:
            raise ContractError("qBittorrent resume transition is invalid")
        reservation.state = ReservationState.RESUME_INTENT_RECORDED

    def account_observed_bytes(self, operation_id: str, observed_bytes: int) -> bool:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.QBITTORRENT_STOPPED or isinstance(observed_bytes, bool) or not isinstance(observed_bytes, int) or observed_bytes <= 0:
            raise ContractError("qBittorrent size observation is invalid")
        if observed_bytes > reservation.bytes_reserved:
            reservation.bytes_reserved = observed_bytes
        return self.reserved_bytes <= self._policy.capacity_bytes

    def mark_qbittorrent_active(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.RESUME_INTENT_RECORDED:
            raise ContractError("qBittorrent active transition is invalid")
        reservation.state = ReservationState.QBITTORRENT_ACTIVE

    def reconcile_qbittorrent_observation(self, operation_id: str, torrent_hash: str | None) -> bool:
        """Advance only an exact observed postcondition; unknown preserves the hold."""
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.INTENT_RECORDED or not _torrent_hash(torrent_hash):
            return False
        reservation.torrent_hash = torrent_hash
        reservation.state = ReservationState.QBITTORRENT_STOPPED
        return True

    def complete(self, operation_id: str) -> Envelope:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.QBITTORRENT_ACTIVE:
            raise ContractError("terminal acquisition transition is invalid")
        reservation.state = ReservationState.TERMINAL
        return self.terminal_observation(operation_id)

    def terminal_observation(self, operation_id: str) -> Envelope:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.TERMINAL:
            raise ContractError("terminal acquisition is unavailable")
        return terminal_acquisition(operation_id=reservation.operation_id, fence_reservation_id=reservation.reservation_id, source=reservation.source, upstream_id=reservation.upstream_id, media_id=reservation.media_id, bytes_reserved=reservation.bytes_reserved)

    def accept_custody(self, receipt: Envelope) -> bool:
        reservation = self._reservations.get(receipt.operation_id)
        if reservation is None or receipt.kind != "custody_receipt":
            return False
        publisher_reservation_id = receipt.body.get("publisher_reservation_id")
        if receipt.body.get("fence_reservation_id") != reservation.reservation_id or not isinstance(publisher_reservation_id, str):
            return False
        if reservation.state is ReservationState.RELEASED:
            return reservation.publisher_reservation_id == publisher_reservation_id
        if reservation.state is not ReservationState.TERMINAL:
            return False
        reservation.publisher_reservation_id = publisher_reservation_id
        reservation.state = ReservationState.RELEASED
        return True

    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "operation_id": item.operation_id,
                "reservation_id": item.reservation_id,
                "source": item.source,
                "upstream_id": item.upstream_id,
                "media_id": item.media_id,
                "bytes_reserved": item.bytes_reserved,
                "requested_bytes": item.requested_bytes,
                "source_fingerprint": item.source_fingerprint,
                "torrent_hash": item.torrent_hash,
                "selector_fingerprint": item.selector_fingerprint,
                "watermark": item.watermark,
                "download_id": item.download_id,
                "state": item.state.value,
                "publisher_reservation_id": item.publisher_reservation_id,
            }
            for item in self._reservations.values()
        )

    @classmethod
    def from_records(cls, policy: FencePolicy, records: Iterable[Mapping[str, object]]) -> "FenceState":
        result = cls(policy)
        expected = {"operation_id", "reservation_id", "source", "upstream_id", "media_id", "bytes_reserved", "requested_bytes", "source_fingerprint", "torrent_hash", "selector_fingerprint", "watermark", "download_id", "state", "publisher_reservation_id"}
        for record in records:
            if set(record) != expected:
                raise ContractError("durable Fence reservation has unknown fields")
            string_fields = ("operation_id", "reservation_id", "source", "upstream_id", "media_id", "source_fingerprint")
            if not all(isinstance(record[field], str) and record[field] for field in string_fields):
                raise ContractError("durable Fence reservation is invalid")
            try:
                item = Reservation(
                    operation_id=record["operation_id"],
                    reservation_id=record["reservation_id"],
                    source=record["source"],
                    upstream_id=record["upstream_id"],
                    media_id=record["media_id"],
                    bytes_reserved=record["bytes_reserved"],
                    requested_bytes=record["requested_bytes"],
                    source_fingerprint=record["source_fingerprint"],
                    torrent_hash=record["torrent_hash"],
                    selector_fingerprint=record["selector_fingerprint"],
                    watermark=record["watermark"],
                    download_id=record["download_id"],
                    state=ReservationState(record["state"]),
                    publisher_reservation_id=record["publisher_reservation_id"],
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("durable Fence reservation is invalid") from exc
            new_state = item.state in {ReservationState.PRE_ADMITTED, ReservationState.GRAB_BOUND}
            if not isinstance(item.bytes_reserved, int) or isinstance(item.bytes_reserved, bool) or item.bytes_reserved <= 0 or not isinstance(item.requested_bytes, int) or isinstance(item.requested_bytes, bool) or item.requested_bytes <= 0 or item.bytes_reserved < item.requested_bytes or (item.publisher_reservation_id is not None and not isinstance(item.publisher_reservation_id, str)) or (item.torrent_hash is not None and not _torrent_hash(item.torrent_hash)) or (item.selector_fingerprint is not None and (not isinstance(item.selector_fingerprint, str) or len(item.selector_fingerprint) != 64 or any(character not in "0123456789abcdef" for character in item.selector_fingerprint))) or (item.watermark is not None and (not isinstance(item.watermark, str) or not item.watermark)) or (item.download_id is not None and (not isinstance(item.download_id, str) or not item.download_id)) or (new_state and (not item.selector_fingerprint or not item.watermark)) or (item.state is ReservationState.PRE_ADMITTED and (item.torrent_hash is not None or item.download_id is not None)) or (item.state is ReservationState.GRAB_BOUND and (not item.torrent_hash or not item.download_id)) or (item.state in {ReservationState.QBITTORRENT_STOPPED, ReservationState.RESUME_INTENT_RECORDED, ReservationState.QBITTORRENT_ACTIVE, ReservationState.TERMINAL, ReservationState.RELEASED} and not item.torrent_hash) or (item.state is ReservationState.RELEASED and not item.publisher_reservation_id):
                raise ContractError("durable Fence reservation is invalid")
            if item.operation_id in result._reservations:
                raise ContractError("durable Fence reservation operation is duplicated")
            result._reservations[item.operation_id] = item
        return result

    def clone(self) -> "FenceState":
        return FenceState.from_records(self._policy, self.records())

    def replace_with(self, other: "FenceState") -> None:
        self._reservations = other._reservations
