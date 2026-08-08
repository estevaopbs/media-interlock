"""Pure Publisher custody and publication state transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Iterable, Mapping
import uuid

from ..contracts import ContractError, Envelope, custody_receipt


class PublicationState(StrEnum):
    CUSTODY_RESERVED = "custody_reserved"
    CANDIDATE_VERIFIED = "candidate_verified"
    GENERATION_INTENT = "generation_intent"
    CATALOG_PENDING = "catalog_pending"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class Publication:
    operation_id: str
    fence_reservation_id: str
    publisher_reservation_id: str
    source: str
    upstream_id: str
    download_id: str
    media_id: str
    bytes_reserved: int
    state: PublicationState
    candidate_relative_path: str | None = None
    candidate_bytes: int | None = None
    candidate_sha256: str | None = None
    generation_id: str | None = None
    previous_generation_id: str | None = None
    asset_slot: str | None = None
    item_type: str | None = None
    provider_ids: tuple[tuple[str, str], ...] | None = None
    notification_attempted: bool = False
    catalog_item_id: str | None = None
    catalog_media_source_id: str | None = None


class PublisherState:
    """Single-writer model; service callers persist each returned transition."""

    def __init__(self) -> None:
        self._publications: dict[str, Publication] = {}

    def publication(self, operation_id: str) -> Publication:
        return self._publications[operation_id]

    def adopt_terminal(self, terminal: Envelope) -> Envelope:
        if terminal.kind != "terminal_acquisition":
            raise ContractError("Publisher accepts only terminal acquisition observations")
        existing = self._publications.get(terminal.operation_id)
        body = terminal.body
        fields = (body["fence_reservation_id"], body["source"], body["upstream_id"], body["download_id"], body["media_id"], body["bytes_reserved"])
        if existing is not None:
            if fields != (existing.fence_reservation_id, existing.source, existing.upstream_id, existing.download_id, existing.media_id, existing.bytes_reserved):
                raise ContractError("terminal acquisition conflicts with durable Publisher custody")
            return custody_receipt(existing.operation_id, existing.fence_reservation_id, existing.publisher_reservation_id)
        reservation_id = f"publisher:{terminal.operation_id}"
        publication = Publication(
            terminal.operation_id,
            str(body["fence_reservation_id"]),
            reservation_id,
            str(body["source"]),
            str(body["upstream_id"]),
            str(body["download_id"]),
            str(body["media_id"]),
            int(body["bytes_reserved"]),
            PublicationState.CUSTODY_RESERVED,
        )
        self._publications[terminal.operation_id] = publication
        return custody_receipt(publication.operation_id, publication.fence_reservation_id, publication.publisher_reservation_id)

    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(asdict(publication) | {"state": publication.state.value} for publication in self._publications.values())

    def mark_candidate_verified(self, operation_id: str, relative_path: str, bytes_verified: int, sha256: str) -> None:
        publication = self.publication(operation_id)
        if publication.state is not PublicationState.CUSTODY_RESERVED or not relative_path or isinstance(bytes_verified, bool) or not isinstance(bytes_verified, int) or bytes_verified <= 0 or len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ContractError("candidate verification transition is invalid")
        self._publications[operation_id] = replace(publication, state=PublicationState.CANDIDATE_VERIFIED, candidate_relative_path=relative_path, candidate_bytes=bytes_verified, candidate_sha256=sha256)

    def bind_asset_identity(self, operation_id: str, asset_slot: str, item_type: str, provider_ids: Mapping[str, str]) -> None:
        publication = self.publication(operation_id)
        normalized = tuple(sorted(provider_ids.items()))
        if (
            publication.state is not PublicationState.CANDIDATE_VERIFIED
            or not isinstance(asset_slot, str)
            or not asset_slot.startswith(f"{publication.source}:")
            or item_type not in {"Movie", "Episode"}
            or (publication.source == "radarr" and item_type != "Movie")
            or (publication.source == "sonarr" and item_type != "Episode")
            or not normalized
            or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value for key, value in normalized)
        ):
            raise ContractError("asset identity transition is invalid")
        self._publications[operation_id] = replace(publication, asset_slot=asset_slot, item_type=item_type, provider_ids=normalized)

    def record_generation_intent(self, operation_id: str, previous_generation_id: str | None) -> None:
        publication = self.publication(operation_id)
        if (
            publication.state is not PublicationState.CANDIDATE_VERIFIED
            or publication.candidate_relative_path is None
            or (previous_generation_id is not None and (not _generation_id(previous_generation_id) or previous_generation_id == operation_id))
        ):
            raise ContractError("generation intent transition is invalid")
        self._publications[operation_id] = replace(
            publication,
            state=PublicationState.GENERATION_INTENT,
            generation_id=operation_id,
            previous_generation_id=previous_generation_id,
        )

    def mark_generation_committed(self, operation_id: str) -> None:
        publication = self.publication(operation_id)
        if publication.state is not PublicationState.GENERATION_INTENT:
            raise ContractError("generation commit transition is invalid")
        self._publications[operation_id] = replace(publication, state=PublicationState.CATALOG_PENDING)

    def mark_notification_attempted(self, operation_id: str) -> None:
        publication = self.publication(operation_id)
        if publication.state is not PublicationState.CATALOG_PENDING:
            raise ContractError("catalog submission transition is invalid")
        self._publications[operation_id] = replace(publication, notification_attempted=True)

    def mark_catalog_observed(self, operation_id: str, item_id: str, media_source_id: str) -> None:
        publication = self.publication(operation_id)
        if (
            publication.state is not PublicationState.CATALOG_PENDING
            or not publication.notification_attempted
            or not isinstance(item_id, str)
            or not item_id
            or not isinstance(media_source_id, str)
            or not media_source_id
        ):
            raise ContractError("catalog observation transition is invalid")
        self._publications[operation_id] = replace(publication, catalog_item_id=item_id, catalog_media_source_id=media_source_id)

    def mark_catalog_delivered(self, operation_id: str) -> None:
        publication = self.publication(operation_id)
        if (
            publication.state is not PublicationState.CATALOG_PENDING
            or not publication.notification_attempted
            or not publication.catalog_item_id
            or not publication.catalog_media_source_id
        ):
            raise ContractError("catalog delivery transition is invalid")
        self._publications[operation_id] = replace(publication, state=PublicationState.DELIVERED, previous_generation_id=None)

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, object]]) -> "PublisherState":
        state = cls()
        expected = {"operation_id", "fence_reservation_id", "publisher_reservation_id", "source", "upstream_id", "download_id", "media_id", "bytes_reserved", "state", "candidate_relative_path", "candidate_bytes", "candidate_sha256", "generation_id", "previous_generation_id", "asset_slot", "item_type", "provider_ids", "notification_attempted", "catalog_item_id", "catalog_media_source_id"}
        for record in records:
            if set(record) != expected:
                raise ContractError("durable Publisher publication has unknown fields")
            try:
                publication = Publication(
                    operation_id=record["operation_id"],
                    fence_reservation_id=record["fence_reservation_id"],
                    publisher_reservation_id=record["publisher_reservation_id"],
                    source=record["source"],
                    upstream_id=record["upstream_id"],
                    download_id=record["download_id"],
                    media_id=record["media_id"],
                    bytes_reserved=record["bytes_reserved"],
                    state=PublicationState(record["state"]),
                    candidate_relative_path=record["candidate_relative_path"],
                    candidate_bytes=record["candidate_bytes"],
                    candidate_sha256=record["candidate_sha256"],
                    generation_id=record["generation_id"],
                    previous_generation_id=record["previous_generation_id"],
                    asset_slot=record["asset_slot"],
                    item_type=record["item_type"],
                    provider_ids=tuple(tuple(pair) for pair in record["provider_ids"]) if isinstance(record["provider_ids"], (list, tuple)) else None,
                    notification_attempted=record["notification_attempted"],
                    catalog_item_id=record["catalog_item_id"],
                    catalog_media_source_id=record["catalog_media_source_id"],
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("durable Publisher publication is invalid") from exc
            string_fields = (publication.operation_id, publication.fence_reservation_id, publication.publisher_reservation_id, publication.source, publication.upstream_id, publication.download_id, publication.media_id)
            progressed = publication.state is not PublicationState.CUSTODY_RESERVED
            has_generation = publication.state in {PublicationState.GENERATION_INTENT, PublicationState.CATALOG_PENDING, PublicationState.DELIVERED}
            previous_is_valid = publication.previous_generation_id is None or _generation_id(publication.previous_generation_id) and publication.previous_generation_id != publication.operation_id
            valid_identity = (
                publication.asset_slot is None
                and publication.item_type is None
                and publication.provider_ids is None
            ) or (
                isinstance(publication.asset_slot, str)
                and publication.asset_slot.startswith(f"{publication.source}:")
                and publication.item_type in {"Movie", "Episode"}
                and not (publication.source == "radarr" and publication.item_type != "Movie")
                and not (publication.source == "sonarr" and publication.item_type != "Episode")
                and isinstance(publication.provider_ids, tuple)
                and bool(publication.provider_ids)
                and all(isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(value, str) and value for value in pair) for pair in publication.provider_ids)
            )
            observed = publication.catalog_item_id is not None or publication.catalog_media_source_id is not None
            if not all(isinstance(field, str) and field for field in string_fields) or publication.source not in {"radarr", "sonarr"} or not isinstance(publication.bytes_reserved, int) or isinstance(publication.bytes_reserved, bool) or publication.bytes_reserved <= 0 or publication.publisher_reservation_id != f"publisher:{publication.operation_id}" or publication.operation_id in state._publications or (progressed and (not isinstance(publication.candidate_relative_path, str) or not publication.candidate_relative_path or not isinstance(publication.candidate_bytes, int) or isinstance(publication.candidate_bytes, bool) or publication.candidate_bytes <= 0 or not isinstance(publication.candidate_sha256, str) or len(publication.candidate_sha256) != 64)) or (has_generation and publication.generation_id != publication.operation_id) or (not has_generation and (publication.generation_id is not None or publication.previous_generation_id is not None)) or (has_generation and not previous_is_valid) or not valid_identity or not isinstance(publication.notification_attempted, bool) or (observed and (not publication.notification_attempted or not isinstance(publication.catalog_item_id, str) or not publication.catalog_item_id or not isinstance(publication.catalog_media_source_id, str) or not publication.catalog_media_source_id)) or (not observed and (publication.catalog_item_id is not None or publication.catalog_media_source_id is not None)) or (publication.state is PublicationState.DELIVERED and publication.previous_generation_id is not None):
                raise ContractError("durable Publisher publication is invalid")
            state._publications[publication.operation_id] = publication
        return state

    def clone(self) -> "PublisherState":
        return self.from_records(self.records())

    def replace_with(self, other: "PublisherState") -> None:
        self._publications = other._publications


def _generation_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version == 4
