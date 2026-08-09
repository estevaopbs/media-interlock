"""Pure Publisher custody and publication state transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Iterable, Mapping
import uuid

from ..contracts import ContractError, Envelope, custody_receipt
from .filesystem import BundleMember, MediaInspection, VerifiedBundle, VerifiedCandidate


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
    bundle_members: tuple[tuple[object, ...], ...] | None = None
    bundle_inspection: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None
    hardlink_frozen: bool = False
    provenance: str = "fence"
    intake_manifest_digest: str | None = None
    generation_id: str | None = None
    previous_generation_id: str | None = None
    asset_slot: str | None = None
    item_type: str | None = None
    provider_ids: tuple[tuple[str, str], ...] | None = None
    notification_attempted: bool = False
    public_conflict: bool = False
    catalog_library_id: str | None = None
    expected_catalog_path: str | None = None
    catalog_item_id: str | None = None
    catalog_media_source_id: str | None = None

    def bundle(self) -> VerifiedBundle | None:
        return _publication_bundle(self)


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

    def mark_bundle_verified(self, operation_id: str, bundle: VerifiedBundle) -> None:
        if not isinstance(bundle, VerifiedBundle) or not bundle.members or bundle.payload.relative_path not in {member.relative_path for member in bundle.members}:
            raise ContractError("candidate bundle verification transition is invalid")
        self.mark_candidate_verified(operation_id, bundle.payload.relative_path, bundle.payload.bytes_verified, bundle.payload.sha256)
        members = tuple(
            (member.relative_path, member.bytes_verified, member.allocated_bytes, member.device, member.inode, member.modified_ns, member.sha256)
            for member in bundle.members
        )
        if len(members) > 129 or len({member[0] for member in members}) != len(members):
            raise ContractError("candidate bundle verification transition is invalid")
        inspection = (bundle.inspection.audio_languages, bundle.inspection.subtitle_languages, bundle.inspection.container_evidence)
        self._publications[operation_id] = replace(self.publication(operation_id), bundle_members=members, bundle_inspection=inspection)

    def mark_hardlink_frozen(self, operation_id: str) -> None:
        publication = self.publication(operation_id)
        if publication.state is not PublicationState.CUSTODY_RESERVED:
            raise ContractError("hardlink freeze transition is invalid")
        self._publications[operation_id] = replace(publication, hardlink_frozen=True)

    def adopt_bootstrap(self, *, operation_id: str, source: str, upstream_id: str, media_id: str, asset_slot: str, item_type: str, provider_ids: Mapping[str, str], bundle: VerifiedBundle, manifest_digest: str) -> None:
        """Create an owner-bound, already-sealed candidate without Fence custody."""
        if not _manifest_digest(manifest_digest) or source not in {"radarr", "sonarr"}:
            raise ContractError("bootstrap intake is invalid")
        existing = self._publications.get(operation_id)
        if existing is not None:
            expected = ("bootstrap", manifest_digest, source, upstream_id, media_id, asset_slot, item_type, tuple(sorted(provider_ids.items())))
            actual = (existing.provenance, existing.intake_manifest_digest, existing.source, existing.upstream_id, existing.media_id, existing.asset_slot, existing.item_type, existing.provider_ids)
            if actual != expected or existing.bundle() != bundle:
                raise ContractError("bootstrap intake conflicts with durable Publisher state")
            return
        publication = Publication(operation_id, f"bootstrap:{operation_id}", f"publisher:{operation_id}", source, upstream_id, "bootstrap", media_id, bundle.bytes_verified, PublicationState.CUSTODY_RESERVED, provenance="bootstrap", intake_manifest_digest=manifest_digest)
        self._publications[operation_id] = publication
        self.mark_bundle_verified(operation_id, bundle)
        self.bind_asset_identity(operation_id, asset_slot, item_type, provider_ids)

    def record_assisted_intent(self, *, operation_id: str, source: str, upstream_id: str, media_id: str, expected_bytes: int, manifest_digest: str) -> None:
        if not _manifest_digest(manifest_digest) or source not in {"radarr", "sonarr"} or isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
            raise ContractError("assisted intake intent is invalid")
        existing = self._publications.get(operation_id)
        if existing is not None:
            if (existing.provenance, existing.intake_manifest_digest, existing.source, existing.upstream_id, existing.media_id, existing.bytes_reserved) != ("assisted", manifest_digest, source, upstream_id, media_id, expected_bytes):
                raise ContractError("assisted intake conflicts with durable Publisher state")
            return
        self._publications[operation_id] = Publication(operation_id, f"assisted:{operation_id}", f"publisher:{operation_id}", source, upstream_id, "assisted", media_id, expected_bytes, PublicationState.CUSTODY_RESERVED, provenance="assisted", intake_manifest_digest=manifest_digest)

    def complete_assisted(self, *, operation_id: str, asset_slot: str, item_type: str, provider_ids: Mapping[str, str], bundle: VerifiedBundle, manifest_digest: str) -> None:
        publication = self.publication(operation_id)
        if publication.provenance != "assisted" or publication.intake_manifest_digest != manifest_digest or publication.bytes_reserved != bundle.bytes_verified:
            raise ContractError("assisted intake does not match durable owner intent")
        expected_identity = (asset_slot, item_type, tuple(sorted(provider_ids.items())))
        actual_identity = (publication.asset_slot, publication.item_type, publication.provider_ids)
        if publication.state is not PublicationState.CUSTODY_RESERVED:
            if actual_identity != expected_identity or publication.bundle() != bundle:
                raise ContractError("assisted intake conflicts with durable Publisher state")
            return
        self.mark_bundle_verified(operation_id, bundle)
        self.bind_asset_identity(operation_id, asset_slot, item_type, provider_ids)

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
        if publication.state is not PublicationState.CATALOG_PENDING or not publication.catalog_library_id or not publication.expected_catalog_path:
            raise ContractError("catalog submission transition is invalid")
        self._publications[operation_id] = replace(publication, notification_attempted=True)

    def mark_operation_conflict(self, operation_id: str) -> None:
        publication = self.publication(operation_id)
        self._publications[operation_id] = replace(publication, public_conflict=True)

    def bind_catalog_expectation(self, operation_id: str, library_id: str, expected_catalog_path: str) -> None:
        publication = self.publication(operation_id)
        if (
            publication.state not in {PublicationState.CATALOG_PENDING, PublicationState.DELIVERED}
            or not isinstance(library_id, str)
            or not library_id
            or not isinstance(expected_catalog_path, str)
            or not expected_catalog_path.startswith("/")
            or ".." in expected_catalog_path.split("/")
        ):
            raise ContractError("catalog expectation transition is invalid")
        existing = (publication.catalog_library_id, publication.expected_catalog_path)
        if existing != (None, None) and existing != (library_id, expected_catalog_path):
            raise ContractError("catalog expectation conflicts with durable Publisher state")
        legacy_observation = publication.state is PublicationState.CATALOG_PENDING and existing == (None, None)
        self._publications[operation_id] = replace(
            publication,
            catalog_library_id=library_id,
            expected_catalog_path=expected_catalog_path,
            catalog_item_id=None if legacy_observation else publication.catalog_item_id,
            catalog_media_source_id=None if legacy_observation else publication.catalog_media_source_id,
        )

    def mark_catalog_observed(self, operation_id: str, item_id: str, media_source_id: str) -> None:
        publication = self.publication(operation_id)
        if (
            publication.state is not PublicationState.CATALOG_PENDING
            or not publication.notification_attempted
            or not publication.catalog_library_id
            or not publication.expected_catalog_path
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
        expected = {"operation_id", "fence_reservation_id", "publisher_reservation_id", "source", "upstream_id", "download_id", "media_id", "bytes_reserved", "state", "candidate_relative_path", "candidate_bytes", "candidate_sha256", "bundle_members", "bundle_inspection", "hardlink_frozen", "provenance", "intake_manifest_digest", "generation_id", "previous_generation_id", "asset_slot", "item_type", "provider_ids", "notification_attempted", "public_conflict", "catalog_library_id", "expected_catalog_path", "catalog_item_id", "catalog_media_source_id"}
        for record in records:
            legacy_expected = expected - {"public_conflict", "catalog_library_id", "expected_catalog_path"}
            if set(record) == legacy_expected:
                record = dict(record) | {"public_conflict": False, "catalog_library_id": None, "expected_catalog_path": None}
            elif set(record) != expected:
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
                    bundle_members=tuple(tuple(member) for member in record["bundle_members"]) if isinstance(record["bundle_members"], (list, tuple)) else None,
                    bundle_inspection=tuple(tuple(group) for group in record["bundle_inspection"]) if isinstance(record["bundle_inspection"], (list, tuple)) and len(record["bundle_inspection"]) == 3 else None,
                    hardlink_frozen=record["hardlink_frozen"],
                    provenance=record["provenance"],
                    intake_manifest_digest=record["intake_manifest_digest"],
                    generation_id=record["generation_id"],
                    previous_generation_id=record["previous_generation_id"],
                    asset_slot=record["asset_slot"],
                    item_type=record["item_type"],
                    provider_ids=tuple(tuple(pair) for pair in record["provider_ids"]) if isinstance(record["provider_ids"], (list, tuple)) else None,
                    notification_attempted=record["notification_attempted"],
                    public_conflict=record["public_conflict"],
                    catalog_library_id=record["catalog_library_id"],
                    expected_catalog_path=record["expected_catalog_path"],
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
                and all(isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(value, str) and value for value in pair) for pair in publication.provider_ids)
            )
            observed = publication.catalog_item_id is not None or publication.catalog_media_source_id is not None
            bound = publication.catalog_library_id is not None or publication.expected_catalog_path is not None
            valid_binding = not bound or (
                isinstance(publication.catalog_library_id, str)
                and bool(publication.catalog_library_id)
                and isinstance(publication.expected_catalog_path, str)
                and publication.expected_catalog_path.startswith("/")
                and ".." not in publication.expected_catalog_path.split("/")
            )
            valid_bundle = (publication.bundle_members is None and publication.bundle_inspection is None) or (
                publication.bundle_members is not None
                and publication.bundle_inspection is not None
                and _bundle_members_valid(publication.bundle_members, publication.candidate_relative_path, publication.candidate_bytes, publication.candidate_sha256)
                and _inspection_valid(publication.bundle_inspection)
            )
            valid_provenance = (
                publication.provenance == "fence" and publication.intake_manifest_digest is None
            ) or (
                publication.provenance in {"bootstrap", "assisted"}
                and _manifest_digest(publication.intake_manifest_digest)
                and publication.fence_reservation_id == f"{publication.provenance}:{publication.operation_id}"
                and publication.download_id == publication.provenance
            )
            if not all(isinstance(field, str) and field for field in string_fields) or publication.source not in {"radarr", "sonarr"} or not isinstance(publication.bytes_reserved, int) or isinstance(publication.bytes_reserved, bool) or publication.bytes_reserved <= 0 or publication.publisher_reservation_id != f"publisher:{publication.operation_id}" or publication.operation_id in state._publications or not isinstance(publication.hardlink_frozen, bool) or (publication.provenance != "fence" and publication.hardlink_frozen) or not valid_provenance or (progressed and (not isinstance(publication.candidate_relative_path, str) or not publication.candidate_relative_path or not isinstance(publication.candidate_bytes, int) or isinstance(publication.candidate_bytes, bool) or publication.candidate_bytes <= 0 or not isinstance(publication.candidate_sha256, str) or len(publication.candidate_sha256) != 64)) or not valid_bundle or (has_generation and publication.generation_id != publication.operation_id) or (not has_generation and (publication.generation_id is not None or publication.previous_generation_id is not None)) or (has_generation and not previous_is_valid) or not valid_identity or not isinstance(publication.notification_attempted, bool) or not isinstance(publication.public_conflict, bool) or not valid_binding or (observed and (not publication.notification_attempted or not isinstance(publication.catalog_item_id, str) or not publication.catalog_item_id or not isinstance(publication.catalog_media_source_id, str) or not publication.catalog_media_source_id)) or (not observed and (publication.catalog_item_id is not None or publication.catalog_media_source_id is not None)) or (publication.state is PublicationState.DELIVERED and (publication.previous_generation_id is not None or not observed)):
                raise ContractError("durable Publisher publication is invalid")
            state._publications[publication.operation_id] = publication
        return state

    def clone(self) -> "PublisherState":
        return self.from_records(self.records())

    def replace_with(self, other: "PublisherState") -> None:
        self._publications = other._publications


def _bundle_members_valid(members: tuple[tuple[object, ...], ...], payload_path: object, payload_bytes: object, payload_sha256: object) -> bool:
    if not 1 <= len(members) <= 129 or not isinstance(payload_path, str) or not isinstance(payload_bytes, int) or isinstance(payload_bytes, bool) or not isinstance(payload_sha256, str):
        return False
    paths: set[str] = set()
    payload_found = False
    for member in members:
        if len(member) != 7:
            return False
        path, bytes_verified, allocated, device, inode, modified, digest = member
        if not isinstance(path, str) or not path or path in paths or not isinstance(bytes_verified, int) or isinstance(bytes_verified, bool) or bytes_verified <= 0 or not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (allocated, device, inode, modified)) or not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return False
        paths.add(path)
        payload_found = payload_found or (path == payload_path and bytes_verified == payload_bytes and digest == payload_sha256)
    return payload_found


def _publication_bundle(publication: Publication) -> VerifiedBundle | None:
    if publication.bundle_members is None or publication.bundle_inspection is None or publication.candidate_relative_path is None or publication.candidate_bytes is None or publication.candidate_sha256 is None:
        return None
    if not _bundle_members_valid(publication.bundle_members, publication.candidate_relative_path, publication.candidate_bytes, publication.candidate_sha256):
        return None
    members = tuple(BundleMember(*member) for member in publication.bundle_members)
    inspection = MediaInspection(*publication.bundle_inspection)
    return VerifiedBundle(VerifiedCandidate(publication.candidate_relative_path, publication.candidate_bytes, publication.candidate_sha256), members, sum(member.bytes_verified for member in members), inspection)


def _generation_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version == 4


def _manifest_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _inspection_valid(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 3 and all(
        isinstance(group, tuple) and len(group) <= 64 and all(isinstance(item, str) and item and len(item) <= 64 for item in group)
        for group in value
    ) and bool(value[2])
