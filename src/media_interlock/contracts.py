"""Versioned, fail-closed cross-component contract envelopes."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


CONTRACT_VERSION = "v1"
MAX_ENVELOPE_BYTES = 64 * 1024


class ContractError(ValueError):
    """Raised when a cross-component message is malformed or ambiguous."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


class StatusCode(StrEnum):
    OK = "ok"
    INHIBITED = "inhibited"
    UNAVAILABLE = "unavailable"
    INVALID_CONTRACT = "invalid_contract"
    CONFLICT = "conflict"


def _operation_id(value: object) -> str:
    if not isinstance(value, str):
        raise ContractError("operation_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ContractError("operation_id must be a canonical UUID") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ContractError("operation_id must be a canonical UUID")
    return value


def _json_body(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("contract body must be an object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ContractError("contract body must be JSON-safe") from exc
    if not isinstance(decoded, dict):
        raise ContractError("contract body must be an object")
    return decoded


@dataclass(frozen=True)
class Envelope:
    version: str
    kind: str
    operation_id: str
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or self.version != CONTRACT_VERSION:
            raise ContractError("unsupported contract version")
        if not isinstance(self.kind, str) or self.kind not in {"status", "acquisition_intent", "acquisition_pre_admission", "terminal_acquisition", "custody_receipt", "metrics", "observe"}:
            raise ContractError("unknown contract kind")
        _operation_id(self.operation_id)
        normalized = _json_body(self.body)
        if self.kind == "status":
            expected = {"code", "message"}
            if set(normalized) != expected or not isinstance(normalized["code"], str) or normalized["code"] not in {code.value for code in StatusCode} or not isinstance(normalized["message"], str):
                raise ContractError("invalid status fields")
        elif self.kind == "acquisition_intent":
            expected = {"bytes_reserved", "media_id", "source", "source_fingerprint", "source_locator", "upstream_id"}
            fingerprint = normalized.get("source_fingerprint")
            if set(normalized) != expected or normalized.get("source") not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"bytes_reserved", "source"}) or not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint) or isinstance(normalized["bytes_reserved"], bool) or not isinstance(normalized["bytes_reserved"], int) or normalized["bytes_reserved"] <= 0:
                raise ContractError("acquisition intent fields are invalid")
        elif self.kind == "acquisition_pre_admission":
            expected = {"expected_bytes", "media_id", "selector_fingerprint", "source", "watermark"}
            selector = normalized.get("selector_fingerprint")
            if set(normalized) != expected or normalized.get("source") not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"expected_bytes", "source"}) or not isinstance(selector, str) or len(selector) != 64 or any(char not in "0123456789abcdef" for char in selector) or isinstance(normalized["expected_bytes"], bool) or not isinstance(normalized["expected_bytes"], int) or normalized["expected_bytes"] <= 0:
                raise ContractError("acquisition pre-admission fields are invalid")
        elif self.kind == "terminal_acquisition":
            expected = {"bytes_reserved", "fence_reservation_id", "media_id", "source", "upstream_id"}
            extended = expected | {"download_id"}
            if set(normalized) != expected and set(normalized) != extended:
                raise ContractError("unknown terminal acquisition fields")
            if not isinstance(normalized["source"], str) or normalized["source"] not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"bytes_reserved", "source"}) or ("download_id" in normalized and (not isinstance(normalized["download_id"], str) or not normalized["download_id"])) or isinstance(normalized["bytes_reserved"], bool) or not isinstance(normalized["bytes_reserved"], int) or normalized["bytes_reserved"] <= 0:
                raise ContractError("terminal acquisition fields are invalid")
        elif self.kind == "custody_receipt":
            expected = {"fence_reservation_id", "publisher_reservation_id"}
            if set(normalized) != expected or not all(isinstance(normalized[name], str) and normalized[name] for name in expected):
                raise ContractError("invalid custody receipt fields")
        elif self.kind == "metrics":
            if normalized and (set(normalized) != {"text"} or not isinstance(normalized["text"], str)):
                raise ContractError("invalid metrics fields")
        elif normalized:
            raise ContractError("observe request has fields")
        object.__setattr__(self, "body", normalized)

    @classmethod
    def from_mapping(cls, value: object) -> "Envelope":
        if not isinstance(value, dict):
            raise ContractError("envelope must be an object")
        expected = {"version", "kind", "operation_id", "body"}
        if set(value) != expected:
            unknown = set(value) - expected
            if unknown:
                raise ContractError(f"unknown envelope fields: {sorted(unknown)[0]}")
            raise ContractError("envelope is missing required fields")
        return cls(value["version"], value["kind"], _operation_id(value["operation_id"]), _json_body(value["body"]))

    def encode(self) -> bytes:
        result = json.dumps(
            {"version": self.version, "kind": self.kind, "operation_id": self.operation_id, "body": dict(self.body)},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(result) + 1 > MAX_ENVELOPE_BYTES:
            raise ContractError("contract envelope exceeds maximum size")
        return result + b"\n"

    @classmethod
    def decode(cls, raw: bytes) -> "Envelope":
        if len(raw) > MAX_ENVELOPE_BYTES or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ContractError("contract envelope must be exactly one bounded newline-delimited frame")
        try:
            value = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("contract envelope is not valid JSON") from exc
        envelope = cls.from_mapping(value)
        if raw != envelope.encode():
            raise ContractError("contract envelope must use canonical JSON")
        return envelope


def acquisition_pre_admission(*, operation_id: str, source: str, media_id: str, selector_fingerprint: str, expected_bytes: int, watermark: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "acquisition_pre_admission", _operation_id(operation_id), {"expected_bytes": expected_bytes, "media_id": media_id, "selector_fingerprint": selector_fingerprint, "source": source, "watermark": watermark})


def terminal_acquisition(*, operation_id: str, fence_reservation_id: str, source: str, upstream_id: str, media_id: str, bytes_reserved: int, download_id: str | None = None) -> Envelope:
    body: dict[str, object] = {"bytes_reserved": bytes_reserved, "fence_reservation_id": fence_reservation_id, "media_id": media_id, "source": source, "upstream_id": upstream_id}
    if download_id is not None:
        body["download_id"] = download_id
    return Envelope(CONTRACT_VERSION, "terminal_acquisition", _operation_id(operation_id), body)


def acquisition_intent(*, operation_id: str, source: str, source_locator: str, upstream_id: str, media_id: str, bytes_reserved: int, source_fingerprint: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "acquisition_intent", _operation_id(operation_id), {"bytes_reserved": bytes_reserved, "media_id": media_id, "source": source, "source_fingerprint": source_fingerprint, "source_locator": source_locator, "upstream_id": upstream_id})


def metrics_response(operation_id: str, text: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "metrics", _operation_id(operation_id), {"text": text})


def custody_receipt(operation_id: str, fence_reservation_id: str, publisher_reservation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "custody_receipt", _operation_id(operation_id), {"fence_reservation_id": fence_reservation_id, "publisher_reservation_id": publisher_reservation_id})


def status_response(operation_id: str, code: StatusCode, message: str) -> Envelope:
    if not isinstance(message, str):
        raise ContractError("status message must be a string")
    return Envelope(CONTRACT_VERSION, "status", _operation_id(operation_id), {"code": code.value, "message": message})


@dataclass(frozen=True)
class ReservationSnapshot:
    fence_count: int
    publisher_count: int


class CustodyLedger:
    """Pure conservative model used by each owner to validate handoff ordering."""

    def __init__(self, operation_id: str, fence_reservation_id: str) -> None:
        self._operation_id = _operation_id(operation_id)
        self._fence_reservation_id = fence_reservation_id
        self._terminal_announced = False
        self._fence_active = True
        self._publisher_reservation_id: str | None = None

    def announce_terminal(self) -> None:
        if not self._fence_active or self._terminal_announced:
            raise ContractError("terminal acquisition transition is invalid")
        self._terminal_announced = True

    def reserve_publisher(self, reservation_id: str) -> None:
        if not self._terminal_announced or not self._fence_active or self._publisher_reservation_id is not None or not reservation_id:
            raise ContractError("publisher custody reservation transition is invalid")
        self._publisher_reservation_id = reservation_id

    def accept_receipt(self, receipt: Envelope) -> None:
        if receipt.kind != "custody_receipt" or self._publisher_reservation_id is None:
            raise ContractError("custody receipt transition is invalid")
        expected = {"fence_reservation_id": self._fence_reservation_id, "publisher_reservation_id": self._publisher_reservation_id}
        if receipt.operation_id != self._operation_id or dict(receipt.body) != expected:
            raise ContractError("custody receipt does not match the durable custody reservation")
        self._fence_active = False

    def snapshot(self) -> ReservationSnapshot:
        return ReservationSnapshot(int(self._fence_active), int(self._publisher_reservation_id is not None))
