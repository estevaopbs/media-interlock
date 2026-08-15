"""Publisher orchestration with durable custody-before-receipt ordering."""

from __future__ import annotations

from typing import Callable, Protocol
from pathlib import Path
from pathlib import PurePath
import re

from ..contracts import (
    ContractError,
    Envelope,
    publisher_operation_binding_sha256,
    publisher_operation_receipt,
    publisher_operation_status,
    terminal_acquisition,
)
from ..adapters.arr import ArrCandidate
from ..adapters.jellyfin import CatalogExpectation, CatalogObservation
from .model import PublicationState, PublisherState
from .filesystem import VerifiedBundle, VerifiedCandidate


def verified_bundle_from_manifest(manifest: object, inspection: "CandidateInspection") -> VerifiedBundle:
    """Re-observe a local source; a caller-supplied manifest is comparison-only."""
    if not isinstance(manifest, dict):
        raise ContractError("publisher intake manifest is invalid")
    relative = manifest.get("candidate_relative_path")
    members = manifest.get("bundle_members")
    if not isinstance(relative, str) or not isinstance(members, list):
        raise ContractError("publisher intake manifest is invalid")
    observed = inspection.verify(relative)
    if not isinstance(observed, VerifiedBundle):
        raise ContractError("publisher intake requires a complete bundle")
    expected = tuple(sorted(
        (
            member.get("path"), member.get("bytes"), member.get("allocated"), member.get("device"),
            member.get("inode"), member.get("modified_ns"), member.get("sha256")
        )
        for member in members if isinstance(member, dict)
    ))
    actual = tuple(sorted((member.relative_path, member.bytes_verified, member.allocated_bytes, member.device, member.inode, member.modified_ns, member.sha256) for member in observed.members))
    inspection = manifest.get("inspection")
    expected_inspection = (
        tuple(inspection.get("audio_languages", ())) if isinstance(inspection, dict) else (),
        tuple(inspection.get("subtitle_languages", ())) if isinstance(inspection, dict) else (),
        tuple(inspection.get("container_evidence", ())) if isinstance(inspection, dict) else (),
    )
    actual_inspection = (observed.inspection.audio_languages, observed.inspection.subtitle_languages, observed.inspection.container_evidence)
    if len(expected) != len(members) or actual != expected or actual_inspection != expected_inspection:
        raise ContractError("publisher intake source differs from the sealed manifest")
    return observed


class PublicationStore(Protocol):
    def save(self, state: PublisherState) -> None: ...


class CandidateCorrelation(Protocol):
    def candidate_relative_path(self, upstream_id: str, media_id: str) -> str | None: ...


class CandidateInspection(Protocol):
    def verify(self, relative_path: str, *, allow_hardlinks: bool = False) -> VerifiedCandidate | VerifiedBundle: ...


class AssetGenerationControl(Protocol):
    def visible_generation(self, asset_slot: str) -> str | None: ...

    def publish(self, asset_slot: str, generation_id: str, candidate: VerifiedCandidate | VerifiedBundle, *, previous_generation_id: str | None = None, hardlink_frozen: bool = False, item_type: str | None = None, provider_ids: Mapping[str, str] | None = None) -> Path: ...

    def ensure_catalog_identity(self, asset_slot: str, generation_id: str, item_type: str, provider_ids: Mapping[str, str], *, candidate_relative_path: str | None = None) -> Path: ...

    def garbage_collect(self, asset_slot: str, retained_generation_ids: set[str]) -> None: ...


class ExactCatalog(Protocol):
    def submit_update(self, internal_path: str, update_type: str): ...

    def observe_catalog(self, expected: CatalogExpectation) -> CatalogObservation | None: ...

    def direct_play_matches(self, observation: CatalogObservation, *, expected_bytes: int, expected_sha256: str) -> bool: ...


class PathTranslation:
    """One allowlisted logical tree translation, never per-item mapping."""

    def __init__(self, canonical_root: Path, namespace: str, jellyfin_prefix: str) -> None:
        self._logical_root = canonical_root / namespace
        self._jellyfin_prefix = jellyfin_prefix.rstrip("/")

    def to_jellyfin(self, canonical_path: Path) -> str:
        try:
            relative = canonical_path.relative_to(self._logical_root)
        except ValueError as exc:
            raise ValueError("canonical path is outside the configured logical namespace") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("canonical path is unsafe")
        return self._jellyfin_prefix + "/" + "/".join(relative.parts)

    def logical_payload(self, asset_slot: str, candidate_relative_path: str) -> Path:
        relative = PurePath(candidate_relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("candidate path is unsafe")
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", relative.suffix.lower()):
            raise ValueError("candidate has no safe media extension")
        return self._logical_root / relative


class PublisherService:
    def __init__(self, state: PublisherState, store: PublicationStore) -> None:
        self._state = state
        self._store = store

    def accept_terminal(self, terminal: Envelope) -> Envelope:
        candidate = self._state.clone()
        receipt = candidate.adopt_terminal(terminal)
        self._store.save(candidate)
        self._state.replace_with(candidate)
        return receipt

    def operation_response(self, operation_id: str) -> Envelope:
        """Project private durable progress onto the stable public operation contract."""
        try:
            publication = self._state.publication(operation_id)
        except KeyError:
            return publisher_operation_status(operation_id, "unavailable")
        binding_sha256 = publication.intake_manifest_digest
        if binding_sha256 is None:
            binding_sha256 = publisher_operation_binding_sha256(terminal_acquisition(
                operation_id=operation_id,
                fence_reservation_id=publication.fence_reservation_id,
                source=publication.source,
                upstream_id=publication.upstream_id,
                media_id=publication.media_id,
                bytes_reserved=publication.bytes_reserved,
                download_id=publication.download_id,
            ))
        status = lambda state: publisher_operation_status(
            operation_id,
            state,
            source=publication.source,
            upstream_id=publication.upstream_id,
            media_id=publication.media_id,
            expected_bytes=publication.bytes_reserved,
            binding_sha256=binding_sha256,
        )
        if publication.public_conflict:
            return status("conflict")
        if publication.state is PublicationState.CUSTODY_RESERVED:
            return status("accepted")
        if publication.state in {PublicationState.CANDIDATE_VERIFIED, PublicationState.GENERATION_INTENT}:
            return status("pending")
        if publication.state is PublicationState.CATALOG_PENDING:
            if publication.catalog_item_id is not None and (not publication.catalog_library_id or not publication.expected_catalog_path):
                return status("unavailable")
            return status("catalog-confirmed" if publication.catalog_item_id is not None else "pending")
        required = (
            publication.asset_slot,
            publication.generation_id,
            publication.candidate_sha256,
            publication.catalog_library_id,
            publication.catalog_item_id,
            publication.catalog_media_source_id,
            publication.expected_catalog_path,
        )
        if publication.state is not PublicationState.DELIVERED or not all(isinstance(value, str) and value for value in required):
            return status("unavailable")
        return publisher_operation_receipt(
            operation_id,
            source=publication.source,
            upstream_id=publication.upstream_id,
            media_id=publication.media_id,
            asset_slot=publication.asset_slot,
            generation_id=publication.generation_id,
            generation_sha256=publication.candidate_sha256,
            library_id=publication.catalog_library_id,
            item_id=publication.catalog_item_id,
            media_source_id=publication.catalog_media_source_id,
            expected_catalog_path=publication.expected_catalog_path,
        )

    def record_operation_conflict(self, operation_id: str) -> bool:
        try:
            self._state.publication(operation_id)
        except KeyError:
            return False
        durable = self._state.clone()
        durable.mark_operation_conflict(operation_id)
        self._persist(durable)
        return True

    def bootstrap_bundle(self, *, operation_id: str, source: str, upstream_id: str, media_id: str, asset_slot: str, item_type: str, provider_ids: dict[str, str], bundle: VerifiedBundle, manifest_digest: str) -> None:
        durable = self._state.clone()
        durable.adopt_bootstrap(
            operation_id=operation_id,
            source=source,
            upstream_id=upstream_id,
            media_id=media_id,
            asset_slot=asset_slot,
            item_type=item_type,
            provider_ids=provider_ids,
            bundle=bundle,
            manifest_digest=manifest_digest,
        )
        self._persist(durable)

    def record_assisted_intent(self, *, operation_id: str, source: str, upstream_id: str, media_id: str, expected_bytes: int, manifest_digest: str) -> None:
        durable = self._state.clone()
        durable.record_assisted_intent(operation_id=operation_id, source=source, upstream_id=upstream_id, media_id=media_id, expected_bytes=expected_bytes, manifest_digest=manifest_digest)
        self._persist(durable)

    def complete_assisted_bundle(self, *, operation_id: str, asset_slot: str, item_type: str, provider_ids: dict[str, str], bundle: VerifiedBundle, manifest_digest: str) -> None:
        durable = self._state.clone()
        durable.complete_assisted(operation_id=operation_id, asset_slot=asset_slot, item_type=item_type, provider_ids=provider_ids, bundle=bundle, manifest_digest=manifest_digest)
        self._persist(durable)

    def _persist(self, candidate: PublisherState) -> None:
        self._store.save(candidate)
        self._state.replace_with(candidate)

    def verify_candidate(self, operation_id: str, candidate: VerifiedCandidate | VerifiedBundle) -> None:
        durable = self._state.clone()
        if isinstance(candidate, VerifiedBundle):
            durable.mark_bundle_verified(operation_id, candidate)
        elif isinstance(candidate, VerifiedCandidate):
            durable.mark_candidate_verified(operation_id, candidate.relative_path, candidate.bytes_verified, candidate.sha256)
        else:
            raise ContractError("candidate inspection result is invalid")
        self._persist(durable)

    def correlate_and_verify(self, operation_id: str, correlation: CandidateCorrelation, inspection: CandidateInspection) -> bool:
        publication = self._state.publication(operation_id)
        if publication.state.name != "CUSTODY_RESERVED" or publication.provenance != "fence":
            return False
        relative_path = correlation.candidate_relative_path(publication.download_id, publication.media_id)
        if relative_path is None:
            return False
        try:
            candidate = inspection.verify(relative_path)
        except Exception:
            return False
        self.verify_candidate(operation_id, candidate)
        return True

    def correlate_identify_and_verify(self, operation_id: str, correlation: object, inspection: CandidateInspection, *, freeze: Callable[[str], bool] | None = None) -> bool:
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.CUSTODY_RESERVED or publication.provenance != "fence":
            return False
        derive = getattr(correlation, "candidate_identity", None)
        if not callable(derive):
            return False
        identity = derive(publication.download_id, publication.media_id)
        if not isinstance(identity, ArrCandidate):
            return False
        requires_freeze = getattr(inspection, "requires_freeze", None)
        try:
            hardlinked = bool(requires_freeze(identity.relative_path)) if callable(requires_freeze) else False
            if hardlinked and (freeze is None or not freeze(operation_id)):
                return False
            candidate = inspection.verify(identity.relative_path, allow_hardlinks=True) if hardlinked else inspection.verify(identity.relative_path)
        except Exception:
            return False
        durable = self._state.clone()
        if hardlinked:
            durable.mark_hardlink_frozen(operation_id)
        if isinstance(candidate, VerifiedBundle):
            durable.mark_bundle_verified(operation_id, candidate)
        elif isinstance(candidate, VerifiedCandidate):
            durable.mark_candidate_verified(operation_id, candidate.relative_path, candidate.bytes_verified, candidate.sha256)
        else:
            return False
        durable.bind_asset_identity(operation_id, identity.asset_slot, identity.item_type, identity.provider_ids)
        self._persist(durable)
        return True

    def commit_asset_generation(self, operation_id: str, generations: AssetGenerationControl) -> Path | None:
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.CANDIDATE_VERIFIED or publication.asset_slot is None:
            return None
        if self._asset_has_unresolved_operation(publication.asset_slot, operation_id):
            return None
        durable = self._state.clone()
        durable.record_generation_intent(operation_id, generations.visible_generation(publication.asset_slot))
        self._persist(durable)
        return self._finish_asset_generation(operation_id, generations)

    def _asset_has_unresolved_operation(self, asset_slot: str, operation_id: str) -> bool:
        return any(
            publication.operation_id != operation_id
            and publication.asset_slot == asset_slot
            and publication.state is not PublicationState.DELIVERED
            for record in self._state.records()
            if (publication := self._state.publication(str(record["operation_id"])))
        )

    def _finish_asset_generation(self, operation_id: str, generations: AssetGenerationControl) -> Path | None:
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.GENERATION_INTENT:
            return None
        assert publication.asset_slot and publication.generation_id and publication.candidate_relative_path and publication.candidate_bytes is not None and publication.candidate_sha256
        candidate = publication.bundle() or VerifiedCandidate(publication.candidate_relative_path, publication.candidate_bytes, publication.candidate_sha256)
        try:
            path = generations.publish(
                publication.asset_slot,
                publication.generation_id,
                candidate,
                previous_generation_id=publication.previous_generation_id,
                # A historical bootstrap is sealed from an Arr-imported
                # staging file.  Such files are normally hardlinks to an
                # already-complete torrent, so its double-observed bundle is
                # copied as an independent generation without Fence custody.
                hardlink_frozen=(publication.hardlink_frozen or publication.provenance == "bootstrap"),
                item_type=publication.item_type,
                provider_ids=dict(publication.provider_ids or ()),
            )
        except Exception:
            return None
        durable = self._state.clone()
        durable.mark_generation_committed(operation_id)
        self._persist(durable)
        return path

    def observe_and_deliver_asset(self, operation_id: str, catalog: ExactCatalog, translation: PathTranslation, *, library_id: str) -> bool:
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.CATALOG_PENDING:
            return False
        assert publication.asset_slot and publication.item_type and publication.provider_ids is not None and publication.candidate_bytes is not None and publication.candidate_sha256
        assert publication.candidate_relative_path is not None
        internal_path = translation.to_jellyfin(translation.logical_payload(publication.asset_slot, publication.candidate_relative_path))
        durable = self._state.clone()
        durable.bind_catalog_expectation(operation_id, library_id, internal_path)
        if (publication.catalog_library_id, publication.expected_catalog_path) != (library_id, internal_path):
            self._persist(durable)
            publication = self._state.publication(operation_id)
        def observe() -> CatalogObservation | None:
            known_item_id = self._known_item_id(publication.asset_slot, operation_id)
            if known_item_id is False:
                return None
            return catalog.observe_catalog(CatalogExpectation(
                library_id=library_id,
                internal_path=internal_path,
                item_type=publication.item_type,
                provider_ids=dict(publication.provider_ids),
                expected_bytes=publication.candidate_bytes,
                known_item_id=known_item_id if isinstance(known_item_id, str) else publication.catalog_item_id,
            ))

        if publication.notification_attempted:
            # A previous POST may have reached Jellyfin. Observe first on every
            # recovery; only an absent bounded observation permits re-submit.
            observed = observe()
        else:
            durable = self._state.clone()
            durable.mark_notification_attempted(operation_id)
            self._persist(durable)
            observed = None
        if observed is None:
            submitted = catalog.submit_update(internal_path, "modified" if publication.previous_generation_id else "created")
            if not getattr(submitted, "accepted", False):
                return False
            observed = observe()
            if observed is None:
                return False
        durable = self._state.clone()
        durable.mark_catalog_observed(operation_id, observed.item_id, observed.media_source_id)
        self._persist(durable)
        publication = self._state.publication(operation_id)
        assert publication.catalog_item_id and publication.catalog_media_source_id
        observed = CatalogObservation(publication.catalog_item_id, publication.catalog_media_source_id, internal_path, publication.candidate_bytes)
        if not catalog.direct_play_matches(observed, expected_bytes=publication.candidate_bytes, expected_sha256=publication.candidate_sha256):
            return False
        durable = self._state.clone()
        durable.mark_catalog_delivered(operation_id)
        self._persist(durable)
        return True

    def revalidate_delivered_binding(self, operation_id: str, catalog: ExactCatalog, translation: PathTranslation, *, library_id: str) -> bool:
        """Upgrade a pre-contract delivery only after repeating exact catalog proof."""
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.DELIVERED:
            return False
        assert publication.asset_slot and publication.item_type and publication.provider_ids is not None
        assert publication.candidate_relative_path and publication.candidate_bytes is not None and publication.candidate_sha256
        assert publication.catalog_item_id and publication.catalog_media_source_id
        internal_path = translation.to_jellyfin(translation.logical_payload(publication.asset_slot, publication.candidate_relative_path))
        if publication.catalog_library_id and publication.expected_catalog_path:
            return (publication.catalog_library_id, publication.expected_catalog_path) == (library_id, internal_path)
        observed = catalog.observe_catalog(CatalogExpectation(
            library_id=library_id,
            internal_path=internal_path,
            item_type=publication.item_type,
            provider_ids=dict(publication.provider_ids),
            expected_bytes=publication.candidate_bytes,
            known_item_id=publication.catalog_item_id,
        ))
        if (
            observed is None
            or observed.item_id != publication.catalog_item_id
            or observed.media_source_id != publication.catalog_media_source_id
            or not catalog.direct_play_matches(observed, expected_bytes=publication.candidate_bytes, expected_sha256=publication.candidate_sha256)
        ):
            return False
        durable = self._state.clone()
        durable.bind_catalog_expectation(operation_id, library_id, internal_path)
        self._persist(durable)
        return True

    def _known_item_id(self, asset_slot: str, operation_id: str) -> str | bool | None:
        known = {
            publication.catalog_item_id
            for record in self._state.records()
            if (publication := self._state.publication(str(record["operation_id"]))).operation_id != operation_id
            and publication.asset_slot == asset_slot
            and publication.state is PublicationState.DELIVERED
            and publication.catalog_item_id is not None
        }
        return next(iter(known)) if len(known) == 1 else (False if len(known) > 1 else None)

    def recover_assets(self, generations: AssetGenerationControl, catalog: ExactCatalog, translation: PathTranslation, *, library_id: str, correlations: dict[str, object] | None = None, inspection: CandidateInspection | None = None) -> None:
        """Resume durable work without changing a possibly consumed slot back."""
        for record in tuple(self._state.records()):
            operation_id = str(record["operation_id"])
            publication = self._state.publication(operation_id)
            if publication.state is PublicationState.CUSTODY_RESERVED and correlations is not None and inspection is not None:
                correlation = correlations.get(publication.source)
                if correlation is not None:
                    self.correlate_identify_and_verify(operation_id, correlation, inspection)
                publication = self._state.publication(operation_id)
            if publication.state is PublicationState.CANDIDATE_VERIFIED:
                self.commit_asset_generation(operation_id, generations)
                publication = self._state.publication(operation_id)
            if publication.state is PublicationState.GENERATION_INTENT:
                self._finish_asset_generation(operation_id, generations)
                publication = self._state.publication(operation_id)
            if publication.state is PublicationState.CATALOG_PENDING:
                self.observe_and_deliver_asset(operation_id, catalog, translation, library_id=library_id)

    def garbage_collect_assets(self, generations: AssetGenerationControl) -> None:
        retained: dict[str, set[str]] = {}
        known_slots: set[str] = set()
        for record in self._state.records():
            publication = self._state.publication(str(record["operation_id"]))
            if publication.asset_slot is None:
                continue
            known_slots.add(publication.asset_slot)
            if publication.state is not PublicationState.DELIVERED and publication.generation_id is not None:
                retained.setdefault(publication.asset_slot, set()).add(publication.generation_id)
                if publication.previous_generation_id is not None:
                    retained[publication.asset_slot].add(publication.previous_generation_id)
        for asset_slot in known_slots:
            generations.garbage_collect(asset_slot, retained.get(asset_slot, set()))

class AssetPublisherWorkProcessor:
    """Single-operation worker for the per-asset catalog-confirmed publisher."""

    def __init__(self, service: PublisherService, correlations: dict[str, object], inspection: CandidateInspection, generations: AssetGenerationControl, catalog: ExactCatalog, translation: PathTranslation, *, library_id: str, freeze: Callable[[str], bool] | None = None) -> None:
        self._service = service
        self._correlations = correlations
        self._inspection = inspection
        self._generations = generations
        self._catalog = catalog
        self._translation = translation
        self._library_id = library_id
        self._freeze = freeze

    def __call__(self, operation_id: str) -> bool:
        try:
            publication = self._service._state.publication(operation_id)
            if publication.public_conflict:
                return False
            if publication.state is PublicationState.CUSTODY_RESERVED:
                correlation = self._correlations.get(publication.source)
                if correlation is None or not self._service.correlate_identify_and_verify(operation_id, correlation, self._inspection, freeze=self._freeze):
                    return False
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.CANDIDATE_VERIFIED:
                self._service.commit_asset_generation(operation_id, self._generations)
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.GENERATION_INTENT:
                self._service._finish_asset_generation(operation_id, self._generations)
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.CATALOG_PENDING:
                assert publication.asset_slot and publication.generation_id and publication.item_type and publication.provider_ids is not None
                self._generations.ensure_catalog_identity(
                    publication.asset_slot,
                    publication.generation_id,
                    publication.item_type,
                    dict(publication.provider_ids),
                    candidate_relative_path=publication.candidate_relative_path,
                )
                if self._service.observe_and_deliver_asset(operation_id, self._catalog, self._translation, library_id=self._library_id):
                    self._service.garbage_collect_assets(self._generations)
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.DELIVERED and (not publication.catalog_library_id or not publication.expected_catalog_path):
                return self._service.revalidate_delivered_binding(
                    operation_id, self._catalog, self._translation, library_id=self._library_id,
                )
            return publication.state in {PublicationState.CATALOG_PENDING, PublicationState.DELIVERED}
        except (ContractError, OSError, ValueError, RuntimeError):
            return False
