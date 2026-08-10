"""Versioned, fail-closed cross-component contract envelopes."""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


CONTRACT_VERSION = "v1"
MAX_ENVELOPE_BYTES = 64 * 1024
PUBLISHER_OPERATION_STATES = frozenset({"accepted", "pending", "catalog-confirmed", "conflict", "unavailable"})


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


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _post_pnr_adoption_body(value: Mapping[str, Any]) -> None:
    expected = {"source", "download_client_id", "entity_id", "torrent_hash", "category", "save_path"}
    if (
        set(value) != expected
        or value.get("source") not in {"radarr", "sonarr"}
        or isinstance(value.get("download_client_id"), bool)
        or not isinstance(value.get("download_client_id"), int)
        or value["download_client_id"] <= 0
        or not isinstance(value.get("entity_id"), str)
        or not value["entity_id"].isdecimal()
        or str(int(value["entity_id"])) != value["entity_id"]
        or not isinstance(value.get("torrent_hash"), str)
        or len(value["torrent_hash"]) != 40
        or any(character not in "0123456789abcdef" for character in value["torrent_hash"])
        or not isinstance(value.get("category"), str)
        or not value["category"]
        or not isinstance(value.get("save_path"), str)
        or not value["save_path"].startswith("/")
        or "\x00" in value["save_path"]
        or any(part in {"", ".", ".."} for part in value["save_path"].split("/")[1:])
    ):
        raise ContractError("post-PNR adoption fields are invalid")


def _canonical_entity_ids(value: object, *, source: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        return None
    if not all(isinstance(item, str) and item.isdecimal() and str(int(item)) == item for item in value):
        return None
    canonical = tuple(value)
    if canonical != tuple(sorted(canonical, key=int)) or len(set(canonical)) != len(canonical):
        return None
    if source == "radarr" and len(canonical) != 1:
        return None
    return canonical


def _post_pnr_historical_adoption_body(value: Mapping[str, Any]) -> None:
    expected = {"source", "download_client_id", "entity_ids", "torrent_hash", "category", "save_path"}
    if set(value) != expected:
        raise ContractError("historical post-PNR adoption fields are invalid")
    source = value.get("source")
    base = dict(value)
    base["entity_id"] = "1"
    base.pop("entity_ids")
    try:
        _post_pnr_adoption_body(base)
    except ContractError as exc:
        raise ContractError("historical post-PNR adoption fields are invalid") from exc
    if _canonical_entity_ids(value.get("entity_ids"), source=source) is None:
        raise ContractError("historical post-PNR adoption fields are invalid")


def _manifest_sha256(manifest: Mapping[str, Any] | object) -> str:
    try:
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("publisher intake manifest is not JSON-safe") from exc
    return hashlib.sha256(encoded).hexdigest()


def _intake_manifest(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected = {"source", "upstream_id", "media_id", "asset_slot", "item_type", "provider_ids", "candidate_relative_path", "bundle_members", "inspection", "expected_catalog_path"}
    if set(value) != expected or value.get("source") not in {"radarr", "sonarr"} or value.get("item_type") not in {"Movie", "Episode"}:
        return False
    if not all(isinstance(value[name], str) and value[name] for name in expected - {"provider_ids", "bundle_members", "inspection"}):
        return False
    if not value["asset_slot"].startswith(value["source"] + ":") or value["candidate_relative_path"].startswith("/") or ".." in value["candidate_relative_path"].split("/"):
        return False
    providers = value["provider_ids"]
    members = value["bundle_members"]
    if providers is not None and (not isinstance(providers, dict) or len(providers) > 16 or any(not isinstance(key, str) or not key or not isinstance(item, str) or not item for key, item in providers.items())):
        return False
    if not isinstance(members, list) or not 1 <= len(members) <= 129:
        return False
    inspection = value["inspection"]
    if not isinstance(inspection, dict) or set(inspection) != {"audio_languages", "subtitle_languages", "container_evidence"} or not all(
        isinstance(group, list) and len(group) <= 64 and all(isinstance(item, str) and item and len(item) <= 64 for item in group)
        for group in inspection.values()
    ) or not inspection["container_evidence"]:
        return False
    paths: set[str] = set()
    payload = False
    for member in members:
        fields = {"path", "bytes", "allocated", "device", "inode", "modified_ns", "sha256"}
        if not isinstance(member, dict) or set(member) != fields or not isinstance(member["path"], str) or not member["path"] or member["path"].startswith("/") or ".." in member["path"].split("/") or member["path"] in paths or not _sha256(member["sha256"]):
            return False
        if any(isinstance(member[name], bool) or not isinstance(member[name], int) or member[name] < 0 for name in ("bytes", "allocated", "device", "inode", "modified_ns")) or member["bytes"] <= 0:
            return False
        paths.add(member["path"])
        payload = payload or member["path"] == value["candidate_relative_path"]
    return payload


@dataclass(frozen=True)
class Envelope:
    version: str
    kind: str
    operation_id: str
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or self.version != CONTRACT_VERSION:
            raise ContractError("unsupported contract version")
        if not isinstance(self.kind, str) or self.kind not in {"status", "acquisition_pre_admission", "acquisition_grab_binding", "acquisition_freeze", "post_pnr_adoption", "post_pnr_adoption_query", "post_pnr_adoption_receipt", "post_pnr_historical_adoption", "post_pnr_historical_adoption_query", "post_pnr_historical_adoption_receipt", "terminal_acquisition", "custody_receipt", "publisher_bootstrap", "publisher_assisted_intent", "publisher_assisted_complete", "publisher_operation_query", "publisher_operation_status", "publisher_operation_receipt", "metrics", "observe", "quiesce"}:
            raise ContractError("unknown contract kind")
        _operation_id(self.operation_id)
        normalized = _json_body(self.body)
        if self.kind == "status":
            expected = {"code", "message"}
            if set(normalized) != expected or not isinstance(normalized["code"], str) or normalized["code"] not in {code.value for code in StatusCode} or not isinstance(normalized["message"], str):
                raise ContractError("invalid status fields")
        elif self.kind == "acquisition_pre_admission":
            expected = {"expected_bytes", "media_id", "selector_fingerprint", "source", "watermark"}
            selector = normalized.get("selector_fingerprint")
            if set(normalized) != expected or normalized.get("source") not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"expected_bytes", "source"}) or not isinstance(selector, str) or len(selector) != 64 or any(char not in "0123456789abcdef" for char in selector) or isinstance(normalized["expected_bytes"], bool) or not isinstance(normalized["expected_bytes"], int) or normalized["expected_bytes"] <= 0:
                raise ContractError("acquisition pre-admission fields are invalid")
        elif self.kind == "acquisition_grab_binding":
            if set(normalized) != {"download_id", "torrent_hash"} or not isinstance(normalized.get("download_id"), str) or not normalized["download_id"] or len(normalized["download_id"]) != 40 or any(char not in "0123456789abcdefABCDEF" for char in normalized["download_id"]) or not isinstance(normalized.get("torrent_hash"), str) or len(normalized["torrent_hash"]) != 40 or any(char not in "0123456789abcdef" for char in normalized["torrent_hash"]) or normalized["download_id"].lower() != normalized["torrent_hash"]:
                raise ContractError("acquisition grab binding fields are invalid")
        elif self.kind == "post_pnr_adoption":
            _post_pnr_adoption_body(normalized)
        elif self.kind == "post_pnr_adoption_query":
            if normalized:
                raise ContractError("post-PNR adoption query has fields")
        elif self.kind == "post_pnr_adoption_receipt":
            if normalized.get("state") != "adopted" or not isinstance(normalized.get("fence_reservation_id"), str) or not normalized["fence_reservation_id"]:
                raise ContractError("post-PNR adoption receipt fields are invalid")
            _post_pnr_adoption_body({key: value for key, value in normalized.items() if key not in {"state", "fence_reservation_id"}})
        elif self.kind == "post_pnr_historical_adoption":
            _post_pnr_historical_adoption_body(normalized)
        elif self.kind == "post_pnr_historical_adoption_query":
            if normalized:
                raise ContractError("historical post-PNR adoption query has fields")
        elif self.kind == "post_pnr_historical_adoption_receipt":
            if normalized.get("state") != "adopted" or not isinstance(normalized.get("fence_reservation_id"), str) or not normalized["fence_reservation_id"]:
                raise ContractError("historical post-PNR adoption receipt fields are invalid")
            _post_pnr_historical_adoption_body({key: value for key, value in normalized.items() if key not in {"state", "fence_reservation_id"}})
        elif self.kind == "terminal_acquisition":
            expected = {"bytes_reserved", "download_id", "fence_reservation_id", "media_id", "source", "upstream_id"}
            if set(normalized) != expected:
                raise ContractError("unknown terminal acquisition fields")
            if not isinstance(normalized["source"], str) or normalized["source"] not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"bytes_reserved", "source"}) or isinstance(normalized["bytes_reserved"], bool) or not isinstance(normalized["bytes_reserved"], int) or normalized["bytes_reserved"] <= 0:
                raise ContractError("terminal acquisition fields are invalid")
        elif self.kind == "custody_receipt":
            expected = {"fence_reservation_id", "publisher_reservation_id"}
            if set(normalized) != expected or not all(isinstance(normalized[name], str) and normalized[name] for name in expected):
                raise ContractError("invalid custody receipt fields")
        elif self.kind == "metrics":
            if normalized and (set(normalized) != {"text"} or not isinstance(normalized["text"], str)):
                raise ContractError("invalid metrics fields")
        elif self.kind == "quiesce":
            if set(normalized) != {"enabled"} or not isinstance(normalized["enabled"], bool):
                raise ContractError("invalid quiescence fields")
        elif self.kind == "acquisition_freeze":
            if normalized:
                raise ContractError("acquisition freeze request has fields")
        elif self.kind in {"publisher_bootstrap", "publisher_assisted_complete"}:
            if set(normalized) != {"manifest", "manifest_sha256"} or not _intake_manifest(normalized["manifest"]) or not isinstance(normalized["manifest_sha256"], str) or normalized["manifest_sha256"] != _manifest_sha256(normalized["manifest"]):
                raise ContractError("publisher intake manifest is invalid")
        elif self.kind == "publisher_assisted_intent":
            expected = {"expected_bytes", "manifest_sha256", "media_id", "source", "upstream_id"}
            if set(normalized) != expected or normalized["source"] not in {"radarr", "sonarr"} or not all(isinstance(normalized[name], str) and normalized[name] for name in expected - {"source", "expected_bytes"}) or not _sha256(normalized["manifest_sha256"]) or isinstance(normalized["expected_bytes"], bool) or not isinstance(normalized["expected_bytes"], int) or normalized["expected_bytes"] <= 0:
                raise ContractError("publisher assisted intent fields are invalid")
        elif self.kind == "publisher_operation_status":
            state = normalized.get("state")
            bound_fields = {"binding_sha256", "expected_bytes", "media_id", "source", "state", "upstream_id"}
            bound = set(normalized) == bound_fields
            if (
                state not in PUBLISHER_OPERATION_STATES
                or (set(normalized) != {"state"} and not bound)
                or (state != "unavailable" and not bound)
                or (bound and (
                    normalized.get("source") not in {"radarr", "sonarr"}
                    or not all(isinstance(normalized.get(name), str) and normalized[name] for name in ("upstream_id", "media_id"))
                    or isinstance(normalized.get("expected_bytes"), bool)
                    or not isinstance(normalized.get("expected_bytes"), int)
                    or normalized["expected_bytes"] <= 0
                    or not _sha256(normalized.get("binding_sha256"))
                ))
            ):
                raise ContractError("publisher operation status fields are invalid")
        elif self.kind == "publisher_operation_receipt":
            expected = {
                "asset_slot", "expected_catalog_path", "generation_id", "generation_sha256", "item_id",
                "library_id", "media_id", "media_source_id", "source", "state", "upstream_id",
            }
            strings = expected - {"source", "state", "generation_sha256", "expected_catalog_path", "asset_slot", "generation_id"}
            if (
                set(normalized) != expected
                or normalized.get("state") != "visible-confirmed"
                or normalized.get("source") not in {"radarr", "sonarr"}
                or not all(isinstance(normalized.get(name), str) and normalized[name] for name in strings)
                or not isinstance(normalized.get("asset_slot"), str)
                or not normalized["asset_slot"].startswith(normalized["source"] + ":")
                or not isinstance(normalized.get("generation_id"), str)
                or normalized["generation_id"] != self.operation_id
                or not _sha256(normalized.get("generation_sha256"))
                or not isinstance(normalized.get("expected_catalog_path"), str)
                or not normalized["expected_catalog_path"].startswith("/")
                or ".." in normalized["expected_catalog_path"].split("/")
            ):
                raise ContractError("publisher operation receipt fields are invalid")
        elif self.kind == "publisher_operation_query":
            if normalized:
                raise ContractError("publisher operation query has fields")
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


def acquisition_grab_binding(*, operation_id: str, download_id: str, torrent_hash: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "acquisition_grab_binding", _operation_id(operation_id), {"download_id": download_id, "torrent_hash": torrent_hash})


def acquisition_freeze(*, operation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "acquisition_freeze", _operation_id(operation_id), {})


def post_pnr_adoption(*, operation_id: str, source: str, download_client_id: int, entity_id: str, torrent_hash: str, category: str, save_path: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_adoption", _operation_id(operation_id), {
        "source": source,
        "download_client_id": download_client_id,
        "entity_id": entity_id,
        "torrent_hash": torrent_hash,
        "category": category,
        "save_path": save_path,
    })


def post_pnr_adoption_query(operation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_adoption_query", _operation_id(operation_id), {})


def post_pnr_adoption_receipt(operation_id: str, *, source: str, download_client_id: int, entity_id: str, torrent_hash: str, category: str, save_path: str, fence_reservation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_adoption_receipt", _operation_id(operation_id), {
        "source": source,
        "download_client_id": download_client_id,
        "entity_id": entity_id,
        "torrent_hash": torrent_hash,
        "category": category,
        "save_path": save_path,
        "fence_reservation_id": fence_reservation_id,
        "state": "adopted",
    })


def post_pnr_historical_adoption(*, operation_id: str, source: str, download_client_id: int, entity_ids: tuple[str, ...] | list[str], torrent_hash: str, category: str, save_path: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_historical_adoption", _operation_id(operation_id), {
        "source": source,
        "download_client_id": download_client_id,
        "entity_ids": list(entity_ids),
        "torrent_hash": torrent_hash,
        "category": category,
        "save_path": save_path,
    })


def post_pnr_historical_adoption_query(operation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_historical_adoption_query", _operation_id(operation_id), {})


def post_pnr_historical_adoption_receipt(operation_id: str, *, source: str, download_client_id: int, entity_ids: tuple[str, ...] | list[str], torrent_hash: str, category: str, save_path: str, fence_reservation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "post_pnr_historical_adoption_receipt", _operation_id(operation_id), {
        "source": source,
        "download_client_id": download_client_id,
        "entity_ids": list(entity_ids),
        "torrent_hash": torrent_hash,
        "category": category,
        "save_path": save_path,
        "fence_reservation_id": fence_reservation_id,
        "state": "adopted",
    })


def publisher_bootstrap(*, operation_id: str, manifest: Mapping[str, Any]) -> Envelope:
    return Envelope(CONTRACT_VERSION, "publisher_bootstrap", _operation_id(operation_id), {"manifest": dict(manifest), "manifest_sha256": _manifest_sha256(manifest)})


def publisher_assisted_intent(*, operation_id: str, source: str, upstream_id: str, media_id: str, expected_bytes: int, manifest_sha256: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "publisher_assisted_intent", _operation_id(operation_id), {"source": source, "upstream_id": upstream_id, "media_id": media_id, "expected_bytes": expected_bytes, "manifest_sha256": manifest_sha256})


def publisher_assisted_complete(*, operation_id: str, manifest: Mapping[str, Any]) -> Envelope:
    return Envelope(CONTRACT_VERSION, "publisher_assisted_complete", _operation_id(operation_id), {"manifest": dict(manifest), "manifest_sha256": _manifest_sha256(manifest)})


def publisher_operation_query(operation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "publisher_operation_query", _operation_id(operation_id), {})


def publisher_operation_status(
    operation_id: str,
    state: str,
    *,
    source: str | None = None,
    upstream_id: str | None = None,
    media_id: str | None = None,
    expected_bytes: int | None = None,
    binding_sha256: str | None = None,
) -> Envelope:
    details = (source, upstream_id, media_id, expected_bytes, binding_sha256)
    body: dict[str, object] = {"state": state}
    if any(value is not None for value in details):
        body |= {
            "binding_sha256": binding_sha256,
            "expected_bytes": expected_bytes,
            "media_id": media_id,
            "source": source,
            "upstream_id": upstream_id,
        }
    return Envelope(CONTRACT_VERSION, "publisher_operation_status", _operation_id(operation_id), body)


def publisher_operation_binding_sha256(request: Envelope) -> str:
    if request.kind in {"publisher_bootstrap", "publisher_assisted_intent", "publisher_assisted_complete"}:
        return str(request.body["manifest_sha256"])
    if request.kind == "terminal_acquisition":
        encoded = json.dumps(dict(request.body), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    raise ContractError("message has no Publisher operation binding")


def publisher_operation_receipt(
    operation_id: str,
    *,
    source: str,
    upstream_id: str,
    media_id: str,
    asset_slot: str,
    generation_id: str,
    generation_sha256: str,
    library_id: str,
    item_id: str,
    media_source_id: str,
    expected_catalog_path: str,
) -> Envelope:
    return Envelope(CONTRACT_VERSION, "publisher_operation_receipt", _operation_id(operation_id), {
        "asset_slot": asset_slot,
        "expected_catalog_path": expected_catalog_path,
        "generation_id": generation_id,
        "generation_sha256": generation_sha256,
        "item_id": item_id,
        "library_id": library_id,
        "media_id": media_id,
        "media_source_id": media_source_id,
        "source": source,
        "state": "visible-confirmed",
        "upstream_id": upstream_id,
    })


def terminal_acquisition(*, operation_id: str, fence_reservation_id: str, source: str, upstream_id: str, media_id: str, bytes_reserved: int, download_id: str) -> Envelope:
    body: dict[str, object] = {"bytes_reserved": bytes_reserved, "download_id": download_id, "fence_reservation_id": fence_reservation_id, "media_id": media_id, "source": source, "upstream_id": upstream_id}
    return Envelope(CONTRACT_VERSION, "terminal_acquisition", _operation_id(operation_id), body)


def metrics_response(operation_id: str, text: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "metrics", _operation_id(operation_id), {"text": text})


def custody_receipt(operation_id: str, fence_reservation_id: str, publisher_reservation_id: str) -> Envelope:
    return Envelope(CONTRACT_VERSION, "custody_receipt", _operation_id(operation_id), {"fence_reservation_id": fence_reservation_id, "publisher_reservation_id": publisher_reservation_id})


def quiesce_request(operation_id: str, *, enabled: bool) -> Envelope:
    return Envelope(CONTRACT_VERSION, "quiesce", _operation_id(operation_id), {"enabled": enabled})


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
