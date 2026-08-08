"""Publisher orchestration with durable custody-before-receipt ordering."""

from __future__ import annotations

from typing import Protocol
from pathlib import Path
from pathlib import PurePath
import re

from ..contracts import ContractError, Envelope
from ..adapters.arr import ArrCandidate
from ..adapters.jellyfin import CatalogExpectation, CatalogObservation
from .model import PublicationState, PublisherState
from .filesystem import VerifiedCandidate


class PublicationStore(Protocol):
    def save(self, state: PublisherState) -> None: ...


class CandidateCorrelation(Protocol):
    def candidate_relative_path(self, upstream_id: str, media_id: str) -> str | None: ...


class CandidateInspection(Protocol):
    def verify(self, relative_path: str) -> VerifiedCandidate: ...


class AssetGenerationControl(Protocol):
    def visible_generation(self, asset_slot: str) -> str | None: ...

    def publish(self, asset_slot: str, generation_id: str, candidate: VerifiedCandidate, *, previous_generation_id: str | None = None) -> Path: ...

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
        suffix = PurePath(candidate_relative_path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            raise ValueError("candidate has no safe media extension")
        return self._logical_root / asset_slot.replace(":", "-") / ("payload" + suffix)


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

    def _persist(self, candidate: PublisherState) -> None:
        self._store.save(candidate)
        self._state.replace_with(candidate)

    def verify_candidate(self, operation_id: str, candidate: VerifiedCandidate) -> None:
        durable = self._state.clone()
        durable.mark_candidate_verified(operation_id, candidate.relative_path, candidate.bytes_verified, candidate.sha256)
        self._persist(durable)

    def correlate_and_verify(self, operation_id: str, correlation: CandidateCorrelation, inspection: CandidateInspection) -> bool:
        publication = self._state.publication(operation_id)
        if publication.state.name != "CUSTODY_RESERVED":
            return False
        relative_path = correlation.candidate_relative_path(publication.upstream_id, publication.media_id)
        if relative_path is None:
            return False
        try:
            candidate = inspection.verify(relative_path)
        except Exception:
            return False
        self.verify_candidate(operation_id, candidate)
        return True

    def correlate_identify_and_verify(self, operation_id: str, correlation: object, inspection: CandidateInspection) -> bool:
        publication = self._state.publication(operation_id)
        if publication.state is not PublicationState.CUSTODY_RESERVED:
            return False
        derive = getattr(correlation, "candidate_identity", None)
        if not callable(derive):
            return False
        identity = derive(publication.upstream_id, publication.media_id)
        if not isinstance(identity, ArrCandidate):
            return False
        try:
            candidate = inspection.verify(identity.relative_path)
        except Exception:
            return False
        durable = self._state.clone()
        durable.mark_candidate_verified(operation_id, candidate.relative_path, candidate.bytes_verified, candidate.sha256)
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
        try:
            path = generations.publish(
                publication.asset_slot,
                publication.generation_id,
                VerifiedCandidate(publication.candidate_relative_path, publication.candidate_bytes, publication.candidate_sha256),
                previous_generation_id=publication.previous_generation_id,
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
        assert publication.asset_slot and publication.item_type and publication.provider_ids and publication.candidate_bytes is not None and publication.candidate_sha256
        assert publication.candidate_relative_path is not None
        internal_path = translation.to_jellyfin(translation.logical_payload(publication.asset_slot, publication.candidate_relative_path))
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

    def __init__(self, service: PublisherService, correlations: dict[str, object], inspection: CandidateInspection, generations: AssetGenerationControl, catalog: ExactCatalog, translation: PathTranslation, *, library_id: str) -> None:
        self._service = service
        self._correlations = correlations
        self._inspection = inspection
        self._generations = generations
        self._catalog = catalog
        self._translation = translation
        self._library_id = library_id

    def __call__(self, operation_id: str) -> None:
        try:
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.CUSTODY_RESERVED:
                correlation = self._correlations.get(publication.source)
                if correlation is None or not self._service.correlate_identify_and_verify(operation_id, correlation, self._inspection):
                    return
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.CANDIDATE_VERIFIED:
                self._service.commit_asset_generation(operation_id, self._generations)
            publication = self._service._state.publication(operation_id)
            if publication.state is PublicationState.CATALOG_PENDING:
                if self._service.observe_and_deliver_asset(operation_id, self._catalog, self._translation, library_id=self._library_id):
                    self._service.garbage_collect_assets(self._generations)
        except (ContractError, OSError, ValueError, RuntimeError):
            return
