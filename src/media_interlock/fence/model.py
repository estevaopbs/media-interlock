"""Pure fail-closed Fence admission and custody state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Iterable, Mapping

from ..contracts import ContractError, Envelope, acquisition_pre_admission, terminal_acquisition


def _torrent_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(character in "0123456789abcdef" for character in value)


class ReservationState(StrEnum):
    PRE_ADMITTED = "pre_admitted"
    GRAB_BOUND = "grab_bound"
    TAG_INTENT_RECORDED = "tag_intent_recorded"
    QBITTORRENT_STOPPED = "qbittorrent_stopped"
    RESUME_INTENT_RECORDED = "resume_intent_recorded"
    QBITTORRENT_ACTIVE = "qbittorrent_active"
    PAUSE_INTENT_RECORDED = "pause_intent_recorded"
    QBITTORRENT_PAUSED = "qbittorrent_paused"
    TERMINAL = "terminal"
    FREEZE_INTENT_RECORDED = "freeze_intent_recorded"
    QBITTORRENT_FROZEN = "qbittorrent_frozen"
    RELEASED = "released"


@dataclass(frozen=True)
class FencePolicy:
    capacity_bytes: int
    max_inflight: int

    def __post_init__(self) -> None:
        if self.capacity_bytes <= 0 or self.max_inflight <= 0:
            raise ValueError("Fence policy bounds must be positive")


@dataclass(frozen=True)
class PreAdmissionIntent:
    operation_id: str
    source: str
    media_id: str
    selector_fingerprint: str
    expected_bytes: int
    watermark: str


@dataclass(frozen=True)
class ExternalAdoptionIntent:
    """A stopped Arr grab observed without a Reconciler release request."""

    operation_id: str
    source: str
    entity_id: str
    download_id: str
    torrent_hash: str
    expected_bytes: int
    history_id: int
    observation_fingerprint: str


@dataclass(frozen=True)
class PostPnrAdoptionIntent:
    """Deployment-authorized claim of one already-stopped Arr grab."""

    operation_id: str
    source: str
    download_client_id: int
    entity_id: str
    torrent_hash: str
    category: str
    save_path: str
    expected_bytes: int
    history_id: int


@dataclass
class Reservation:
    operation_id: str
    reservation_id: str
    source: str
    upstream_id: str
    media_id: str
    bytes_reserved: int
    requested_bytes: int
    state: ReservationState
    torrent_hash: str | None = None
    publisher_reservation_id: str | None = None
    selector_fingerprint: str | None = None
    watermark: str | None = None
    download_id: str | None = None
    observation_fingerprint: str | None = None
    external_history_id: int | None = None
    remaining_download_bytes: int | None = None


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str


@dataclass(frozen=True)
class QbittorrentObservation:
    """An exact qBittorrent query result; absence is not an error inference."""

    kind: str
    observed_bytes: int | None = None
    remaining_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"absent", "unknown", "ambiguous", "observed"}:
            raise ValueError("qBittorrent observation kind is invalid")
        valid_size = isinstance(self.observed_bytes, int) and not isinstance(self.observed_bytes, bool) and self.observed_bytes > 0
        valid_remaining = self.remaining_bytes is None or (isinstance(self.remaining_bytes, int) and not isinstance(self.remaining_bytes, bool) and self.observed_bytes is not None and 0 <= self.remaining_bytes <= self.observed_bytes)
        if (self.kind == "observed") != valid_size or (self.kind != "observed" and (self.observed_bytes is not None or self.remaining_bytes is not None)) or not valid_remaining:
            raise ValueError("qBittorrent observation size is invalid")


@dataclass(frozen=True)
class QbittorrentActivityObservation:
    """A qBittorrent active or terminal query without inferred absence."""

    kind: str
    active: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"absent", "unknown", "ambiguous", "observed"}:
            raise ValueError("qBittorrent activity observation kind is invalid")
        if (self.kind == "observed") != isinstance(self.active, bool):
            raise ValueError("qBittorrent activity observation value is invalid")


class FenceState:
    """Single-writer state model; callers persist every transition before effects."""

    def __init__(self, policy: FencePolicy) -> None:
        self._policy = policy
        self._reservations: dict[str, Reservation] = {}
        self._watermarks: dict[str, int] = {}
        self._quiescing = False
        self._post_pnr_adoptions: dict[str, PostPnrAdoptionIntent] = {}

    @property
    def reserved_bytes(self) -> int:
        return sum(item.bytes_reserved for item in self._reservations.values() if item.state is not ReservationState.RELEASED)

    @property
    def within_capacity(self) -> bool:
        return self.reserved_bytes <= self._policy.capacity_bytes

    def reservation(self, operation_id: str) -> Reservation:
        return self._reservations[operation_id]

    @property
    def quiescing(self) -> bool:
        return self._quiescing

    def begin_quiescence(self) -> None:
        self._quiescing = True

    def end_quiescence(self) -> None:
        self._quiescing = False

    def watermark(self, source: str) -> int | None:
        return self._watermarks.get(source)

    def operation_for_observation(self, fingerprint: str) -> str | None:
        if not isinstance(fingerprint, str):
            return None
        for operation_id, reservation in self._reservations.items():
            if reservation.observation_fingerprint == fingerprint:
                return operation_id
        return None

    def post_pnr_adoption(self, operation_id: str) -> PostPnrAdoptionIntent | None:
        return self._post_pnr_adoptions.get(operation_id)

    def record_watermark(self, source: str, watermark: int) -> None:
        if not isinstance(source, str) or not source or isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            raise ContractError("external Arr watermark is invalid")
        current = self._watermarks.get(source)
        if current is not None and watermark < current:
            raise ContractError("external Arr watermark regressed")
        self._watermarks[source] = watermark

    def pre_admit(self, intent: PreAdmissionIntent, *, qbittorrent_ready: bool, prowlarr_ready: bool, publisher_ready: bool) -> AdmissionDecision:
        if self._quiescing:
            return AdmissionDecision(False, "quiescing")
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
        self._reservations[intent.operation_id] = Reservation(intent.operation_id, f"fence:{intent.operation_id}", intent.source, intent.media_id, intent.media_id, intent.expected_bytes, intent.expected_bytes, ReservationState.PRE_ADMITTED, selector_fingerprint=intent.selector_fingerprint, watermark=intent.watermark)
        return AdmissionDecision(True, "admitted")

    def adopt_external(self, intent: ExternalAdoptionIntent, *, qbittorrent_ready: bool, publisher_ready: bool) -> AdmissionDecision:
        """Persist a unique external observation before its qBittorrent effect."""
        if self._quiescing:
            return AdmissionDecision(False, "quiescing")
        valid_fingerprint = isinstance(intent.observation_fingerprint, str) and len(intent.observation_fingerprint) == 64 and all(character in "0123456789abcdef" for character in intent.observation_fingerprint)
        valid_history = isinstance(intent.history_id, int) and not isinstance(intent.history_id, bool) and intent.history_id > 0
        valid_bytes = isinstance(intent.expected_bytes, int) and not isinstance(intent.expected_bytes, bool) and intent.expected_bytes > 0
        if not valid_fingerprint or not valid_history or not valid_bytes or not isinstance(intent.source, str) or not intent.source or not isinstance(intent.entity_id, str) or not intent.entity_id or not isinstance(intent.operation_id, str) or not intent.operation_id or not _torrent_hash(intent.torrent_hash) or not isinstance(intent.download_id, str) or intent.download_id.lower() != intent.torrent_hash:
            return AdmissionDecision(False, "invalid_external_observation")
        existing = self._reservations.get(intent.operation_id)
        by_fingerprint = next((item for item in self._reservations.values() if item.observation_fingerprint == intent.observation_fingerprint), None)
        if existing is not None or by_fingerprint is not None:
            item = existing if existing is not None else by_fingerprint
            assert item is not None
            immutable = (item.source, item.media_id, item.download_id, item.torrent_hash, item.requested_bytes, item.external_history_id, item.observation_fingerprint)
            observed = (intent.source, intent.entity_id, intent.download_id, intent.torrent_hash, intent.expected_bytes, intent.history_id, intent.observation_fingerprint)
            if immutable != observed:
                return AdmissionDecision(False, "conflict")
            return AdmissionDecision(True, "idempotent")
        if not qbittorrent_ready:
            return AdmissionDecision(False, "qbittorrent_unready")
        if not publisher_ready:
            return AdmissionDecision(False, "publisher_backpressure")
        active = [item for item in self._reservations.values() if item.state is not ReservationState.RELEASED]
        if len(active) >= self._policy.max_inflight:
            return AdmissionDecision(False, "concurrency_exhausted")
        if self.reserved_bytes + intent.expected_bytes > self._policy.capacity_bytes:
            return AdmissionDecision(False, "capacity_exhausted")
        self._reservations[intent.operation_id] = Reservation(intent.operation_id, f"fence:{intent.operation_id}", intent.source, intent.entity_id, intent.entity_id, intent.expected_bytes, intent.expected_bytes, ReservationState.PRE_ADMITTED, torrent_hash=intent.torrent_hash, download_id=intent.download_id, observation_fingerprint=intent.observation_fingerprint, external_history_id=intent.history_id)
        return AdmissionDecision(True, "admitted")

    def adopt_post_pnr(self, intent: PostPnrAdoptionIntent, *, qbittorrent_ready: bool) -> AdmissionDecision:
        """Persist one deployment-authorized sealed eligibility before tagging it."""
        valid = (
            isinstance(intent.operation_id, str) and intent.operation_id
            and intent.source in {"radarr", "sonarr"}
            and isinstance(intent.download_client_id, int) and not isinstance(intent.download_client_id, bool) and intent.download_client_id > 0
            and isinstance(intent.entity_id, str) and intent.entity_id.isdecimal() and str(int(intent.entity_id)) == intent.entity_id
            and _torrent_hash(intent.torrent_hash)
            and isinstance(intent.category, str) and bool(intent.category)
            and isinstance(intent.save_path, str) and intent.save_path.startswith("/") and "\x00" not in intent.save_path and all(part not in {"", ".", ".."} for part in intent.save_path.split("/")[1:])
            and isinstance(intent.expected_bytes, int) and not isinstance(intent.expected_bytes, bool) and intent.expected_bytes > 0
            and isinstance(intent.history_id, int) and not isinstance(intent.history_id, bool) and intent.history_id > 0
        )
        if not valid or self._quiescing:
            return AdmissionDecision(False, "invalid_post_pnr_adoption" if not valid else "quiescing")
        existing = self._post_pnr_adoptions.get(intent.operation_id)
        if existing is not None:
            immutable = (existing.source, existing.download_client_id, existing.entity_id, existing.torrent_hash, existing.category, existing.save_path)
            requested = (intent.source, intent.download_client_id, intent.entity_id, intent.torrent_hash, intent.category, intent.save_path)
            return AdmissionDecision(immutable == requested, "idempotent" if immutable == requested else "conflict")
        if intent.operation_id in self._reservations:
            return AdmissionDecision(False, "conflict")
        if not qbittorrent_ready:
            return AdmissionDecision(False, "qbittorrent_unready")
        active = [item for item in self._reservations.values() if item.state is not ReservationState.RELEASED]
        if len(active) >= self._policy.max_inflight:
            return AdmissionDecision(False, "concurrency_exhausted")
        if self.reserved_bytes + intent.expected_bytes > self._policy.capacity_bytes:
            return AdmissionDecision(False, "capacity_exhausted")
        fingerprint = hashlib.sha256((intent.source + "\x00" + intent.entity_id + "\x00" + intent.torrent_hash + "\x00" + str(intent.history_id)).encode("ascii")).hexdigest()
        self._reservations[intent.operation_id] = Reservation(intent.operation_id, f"fence:{intent.operation_id}", intent.source, intent.entity_id, intent.entity_id, intent.expected_bytes, intent.expected_bytes, ReservationState.GRAB_BOUND, torrent_hash=intent.torrent_hash, download_id=intent.torrent_hash, selector_fingerprint=fingerprint, watermark="post-pnr")
        self._post_pnr_adoptions[intent.operation_id] = intent
        return AdmissionDecision(True, "admitted")

    def bind_observed_grab(self, operation_id: str, *, download_id: str, torrent_hash: str) -> None:
        reservation = self.reservation(operation_id)
        if not isinstance(download_id, str) or len(download_id) != 40 or any(character not in "0123456789abcdefABCDEF" for character in download_id) or not _torrent_hash(torrent_hash) or download_id.lower() != torrent_hash:
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

    def request_tag(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.GRAB_BOUND or not reservation.torrent_hash or not reservation.download_id:
            raise ContractError("qBittorrent tag intent transition is invalid")
        reservation.state = ReservationState.TAG_INTENT_RECORDED

    def mark_qbittorrent_tagged(self, operation_id: str, *, observed_bytes: int, remaining_download_bytes: int | None = None) -> bool:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.TAG_INTENT_RECORDED or not reservation.torrent_hash:
            raise ContractError("qBittorrent tag observation transition is invalid")
        if isinstance(observed_bytes, bool) or not isinstance(observed_bytes, int) or observed_bytes <= 0:
            raise ContractError("qBittorrent size observation is invalid")
        if observed_bytes > reservation.bytes_reserved:
            reservation.bytes_reserved = observed_bytes
        if remaining_download_bytes is not None:
            if isinstance(remaining_download_bytes, bool) or not isinstance(remaining_download_bytes, int) or not 0 <= remaining_download_bytes <= observed_bytes:
                raise ContractError("qBittorrent remaining size observation is invalid")
            reservation.remaining_download_bytes = remaining_download_bytes
        reservation.state = ReservationState.QBITTORRENT_STOPPED
        return self.within_capacity

    def request_resume(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state not in {ReservationState.QBITTORRENT_STOPPED, ReservationState.QBITTORRENT_PAUSED} or not reservation.torrent_hash or self._quiescing:
            raise ContractError("qBittorrent resume transition is invalid")
        reservation.state = ReservationState.RESUME_INTENT_RECORDED

    def request_pause(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if not self._quiescing or reservation.state is not ReservationState.QBITTORRENT_ACTIVE or not reservation.torrent_hash:
            raise ContractError("qBittorrent pause intent transition is invalid")
        reservation.state = ReservationState.PAUSE_INTENT_RECORDED

    def mark_qbittorrent_paused(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if not self._quiescing or reservation.state is not ReservationState.PAUSE_INTENT_RECORDED:
            raise ContractError("qBittorrent pause observation transition is invalid")
        reservation.state = ReservationState.QBITTORRENT_PAUSED

    def mark_qbittorrent_active(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.RESUME_INTENT_RECORDED:
            raise ContractError("qBittorrent active transition is invalid")
        reservation.state = ReservationState.QBITTORRENT_ACTIVE

    def request_freeze(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.TERMINAL or not reservation.torrent_hash:
            raise ContractError("qBittorrent freeze intent transition is invalid")
        reservation.state = ReservationState.FREEZE_INTENT_RECORDED

    def mark_qbittorrent_frozen(self, operation_id: str) -> None:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.FREEZE_INTENT_RECORDED:
            raise ContractError("qBittorrent freeze observation transition is invalid")
        reservation.state = ReservationState.QBITTORRENT_FROZEN

    def complete(self, operation_id: str) -> Envelope:
        reservation = self.reservation(operation_id)
        if reservation.state is not ReservationState.QBITTORRENT_ACTIVE:
            raise ContractError("terminal acquisition transition is invalid")
        reservation.state = ReservationState.TERMINAL
        return self.terminal_observation(operation_id)

    def terminal_observation(self, operation_id: str) -> Envelope:
        reservation = self.reservation(operation_id)
        if reservation.state not in {ReservationState.TERMINAL, ReservationState.QBITTORRENT_FROZEN}:
            raise ContractError("terminal acquisition is unavailable")
        assert reservation.download_id is not None
        return terminal_acquisition(operation_id=reservation.operation_id, fence_reservation_id=reservation.reservation_id, source=reservation.source, upstream_id=reservation.upstream_id, media_id=reservation.media_id, bytes_reserved=reservation.bytes_reserved, download_id=reservation.download_id)

    def accept_custody(self, receipt: Envelope) -> bool:
        reservation = self._reservations.get(receipt.operation_id)
        if reservation is None or receipt.kind != "custody_receipt":
            return False
        publisher_reservation_id = receipt.body.get("publisher_reservation_id")
        if receipt.body.get("fence_reservation_id") != reservation.reservation_id or not isinstance(publisher_reservation_id, str):
            return False
        if reservation.state is ReservationState.RELEASED:
            return reservation.publisher_reservation_id == publisher_reservation_id
        if reservation.state not in {ReservationState.TERMINAL, ReservationState.QBITTORRENT_FROZEN}:
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
                "torrent_hash": item.torrent_hash,
                "selector_fingerprint": item.selector_fingerprint,
                "watermark": item.watermark,
                "download_id": item.download_id,
                "observation_fingerprint": item.observation_fingerprint,
                "external_history_id": item.external_history_id,
                "remaining_download_bytes": item.remaining_download_bytes,
                "state": item.state.value,
                "publisher_reservation_id": item.publisher_reservation_id,
            }
            for item in self._reservations.values()
        )

    @classmethod
    def from_records(cls, policy: FencePolicy, records: Iterable[Mapping[str, object]]) -> "FenceState":
        result = cls(policy)
        expected = {"operation_id", "reservation_id", "source", "upstream_id", "media_id", "bytes_reserved", "requested_bytes", "torrent_hash", "selector_fingerprint", "watermark", "download_id", "observation_fingerprint", "external_history_id", "remaining_download_bytes", "state", "publisher_reservation_id"}
        for record in records:
            if set(record) != expected:
                raise ContractError("durable Fence reservation has unknown fields")
            string_fields = ("operation_id", "reservation_id", "source", "upstream_id", "media_id")
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
                    torrent_hash=record["torrent_hash"],
                    selector_fingerprint=record["selector_fingerprint"],
                    watermark=record["watermark"],
                    download_id=record["download_id"],
                    observation_fingerprint=record["observation_fingerprint"],
                    external_history_id=record["external_history_id"],
                    remaining_download_bytes=record["remaining_download_bytes"],
                    state=ReservationState(record["state"]),
                    publisher_reservation_id=record["publisher_reservation_id"],
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("durable Fence reservation is invalid") from exc
            bound_state = item.state is not ReservationState.PRE_ADMITTED
            external = item.observation_fingerprint is not None
            if not isinstance(item.bytes_reserved, int) or isinstance(item.bytes_reserved, bool) or item.bytes_reserved <= 0 or not isinstance(item.requested_bytes, int) or isinstance(item.requested_bytes, bool) or item.requested_bytes <= 0 or item.bytes_reserved < item.requested_bytes or (item.remaining_download_bytes is not None and (not isinstance(item.remaining_download_bytes, int) or isinstance(item.remaining_download_bytes, bool) or not 0 <= item.remaining_download_bytes <= item.bytes_reserved)) or (item.publisher_reservation_id is not None and not isinstance(item.publisher_reservation_id, str)) or (item.torrent_hash is not None and not _torrent_hash(item.torrent_hash)) or (item.selector_fingerprint is not None and (not isinstance(item.selector_fingerprint, str) or len(item.selector_fingerprint) != 64 or any(character not in "0123456789abcdef" for character in item.selector_fingerprint))) or (item.observation_fingerprint is not None and (not isinstance(item.observation_fingerprint, str) or len(item.observation_fingerprint) != 64 or any(character not in "0123456789abcdef" for character in item.observation_fingerprint))) or (item.external_history_id is not None and (not isinstance(item.external_history_id, int) or isinstance(item.external_history_id, bool) or item.external_history_id <= 0)) or (item.watermark is not None and (not isinstance(item.watermark, str) or not item.watermark)) or (item.download_id is not None and (not isinstance(item.download_id, str) or len(item.download_id) != 40 or any(character not in "0123456789abcdefABCDEF" for character in item.download_id))) or (item.download_id is not None and item.torrent_hash is not None and item.download_id.lower() != item.torrent_hash) or (external and (item.selector_fingerprint is not None or item.watermark is not None or item.external_history_id is None or not item.torrent_hash or not item.download_id)) or (not external and (not item.selector_fingerprint or not item.watermark or item.external_history_id is not None)) or (item.state is ReservationState.PRE_ADMITTED and not external and (item.torrent_hash is not None or item.download_id is not None)) or (bound_state and (not item.torrent_hash or not item.download_id)) or (item.state is ReservationState.RELEASED and not item.publisher_reservation_id):
                raise ContractError("durable Fence reservation is invalid")
            if item.operation_id in result._reservations:
                raise ContractError("durable Fence reservation operation is duplicated")
            result._reservations[item.operation_id] = item
        return result

    def clone(self) -> "FenceState":
        result = FenceState.from_records(self._policy, self.records())
        result._watermarks = dict(self._watermarks)
        result._quiescing = self._quiescing
        result._post_pnr_adoptions = dict(self._post_pnr_adoptions)
        return result

    def replace_with(self, other: "FenceState") -> None:
        self._reservations = other._reservations
        self._watermarks = other._watermarks
        self._quiescing = other._quiescing
        self._post_pnr_adoptions = other._post_pnr_adoptions

    def watermarks(self) -> dict[str, int]:
        return dict(self._watermarks)

    def post_pnr_records(self) -> tuple[dict[str, object], ...]:
        return tuple({
            "operation_id": item.operation_id,
            "source": item.source,
            "download_client_id": item.download_client_id,
            "entity_id": item.entity_id,
            "torrent_hash": item.torrent_hash,
            "category": item.category,
            "save_path": item.save_path,
            "expected_bytes": item.expected_bytes,
            "history_id": item.history_id,
        } for item in self._post_pnr_adoptions.values())

    @classmethod
    def from_snapshot(cls, policy: FencePolicy, records: Iterable[Mapping[str, object]], watermarks: Mapping[str, object], *, quiescing: bool = False, post_pnr_adoptions: Iterable[Mapping[str, object]] = ()) -> "FenceState":
        result = cls.from_records(policy, records)
        for source, watermark in watermarks.items():
            result.record_watermark(source, watermark)  # type: ignore[arg-type]
        if not isinstance(quiescing, bool):
            raise ContractError("durable Fence quiescence is invalid")
        result._quiescing = quiescing
        expected = {"operation_id", "source", "download_client_id", "entity_id", "torrent_hash", "category", "save_path", "expected_bytes", "history_id"}
        for record in post_pnr_adoptions:
            if set(record) != expected:
                raise ContractError("durable post-PNR adoption is invalid")
            try:
                intent = PostPnrAdoptionIntent(
                    operation_id=record["operation_id"], source=record["source"], download_client_id=record["download_client_id"],
                    entity_id=record["entity_id"], torrent_hash=record["torrent_hash"], category=record["category"],
                    save_path=record["save_path"], expected_bytes=record["expected_bytes"], history_id=record["history_id"],
                )
            except TypeError as exc:
                raise ContractError("durable post-PNR adoption is invalid") from exc
            probe = cls(policy)
            if not probe.adopt_post_pnr(intent, qbittorrent_ready=True).admitted:
                raise ContractError("durable post-PNR adoption is invalid")
            reservation = result._reservations.get(intent.operation_id)
            if intent.operation_id in result._post_pnr_adoptions or reservation is None or reservation.source != intent.source or reservation.media_id != intent.entity_id or reservation.torrent_hash != intent.torrent_hash or reservation.requested_bytes != intent.expected_bytes or reservation.state not in {ReservationState.GRAB_BOUND, ReservationState.TAG_INTENT_RECORDED, ReservationState.QBITTORRENT_STOPPED}:
                raise ContractError("durable post-PNR adoption is invalid")
            result._post_pnr_adoptions[intent.operation_id] = intent
        return result
