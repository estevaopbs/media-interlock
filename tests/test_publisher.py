from __future__ import annotations

import unittest
import uuid
import tempfile
import os
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import ContractError, terminal_acquisition
from media_interlock.publisher.model import PublisherState, PublicationState
from media_interlock.publisher.service import PublisherService
from media_interlock.publisher.store import PublisherStore
from media_interlock.publisher.filesystem import CandidateVerifier, CandidateSafetyError
from media_interlock.publisher.generation import GenerationPublisher
from media_interlock.publisher.generation import CanonicalWriterLock
from media_interlock.publisher.filesystem import VerifiedCandidate


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))


class PublisherCustodyTests(unittest.TestCase):
    def terminal(self):
        return terminal_acquisition(
            operation_id=OPERATION_ID,
            fence_reservation_id="fence:12345678-1234-4678-9234-567812345678",
            source="radarr",
            upstream_id="grab-42",
            media_id="movie-42",
            bytes_reserved=400,
            download_id="grab-42",
        )

    def test_terminal_adoption_is_idempotent_and_returns_exact_receipt(self) -> None:
        state = PublisherState()

        first = state.adopt_terminal(self.terminal())
        second = state.adopt_terminal(self.terminal())

        self.assertEqual(first, second)
        self.assertEqual("custody_receipt", first.kind)
        self.assertEqual(PublicationState.CUSTODY_RESERVED, state.publication(OPERATION_ID).state)
        self.assertEqual("publisher:12345678-1234-4678-9234-567812345678", first.body["publisher_reservation_id"])

    def test_operation_identity_cannot_adopt_a_different_terminal_payload(self) -> None:
        state = PublisherState()
        state.adopt_terminal(self.terminal())
        conflicting = terminal_acquisition(
            operation_id=OPERATION_ID,
            fence_reservation_id="fence:12345678-1234-4678-9234-567812345678",
            source="radarr",
            upstream_id="grab-42",
            media_id="movie-other",
            bytes_reserved=400,
            download_id="grab-42",
        )

        with self.assertRaisesRegex(ContractError, "conflicts"):
            state.adopt_terminal(conflicting)

    def test_custody_receipt_is_not_returned_after_failed_durable_adoption(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                raise OSError("full")

        state = PublisherState()
        service = PublisherService(state, Store())

        with self.assertRaises(OSError):
            service.accept_terminal(self.terminal())
        with self.assertRaises(KeyError):
            state.publication(OPERATION_ID)

    def test_restart_preserves_durable_custody_before_any_candidate_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PublisherStore.open(Path(directory) / "publisher")
            state = store.load()
            receipt = PublisherService(state, store).accept_terminal(self.terminal())
            store.close()

            restarted = PublisherStore.open(Path(directory) / "publisher")
            self.addCleanup(restarted.close)
            restored = restarted.load()
            self.assertEqual(receipt, restored.adopt_terminal(self.terminal()))
            self.assertEqual(PublicationState.CUSTODY_RESERVED, restored.publication(OPERATION_ID).state)

    def test_legacy_publisher_state_requires_an_explicit_bundle_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PublisherStore.open(Path(directory) / "publisher")
            self.addCleanup(store.close)
            store._store.put("publisher.publications.v1", "[]")  # type: ignore[attr-defined]

            with self.assertRaisesRegex(ContractError, "explicit migration"):
                store.load()

    def test_catalog_submission_and_observation_are_distinct_durable_boundaries(self) -> None:
        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)

        state.bind_catalog_expectation(OPERATION_ID, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        state.mark_notification_attempted(OPERATION_ID)
        pending = state.publication(OPERATION_ID)
        self.assertEqual(PublicationState.CATALOG_PENDING, pending.state)
        self.assertTrue(pending.notification_attempted)
        state.mark_catalog_observed(OPERATION_ID, "jellyfin-item", "media-source")
        self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)
        self.assertEqual("jellyfin-item", state.publication(OPERATION_ID).catalog_item_id)
        state.mark_catalog_delivered(OPERATION_ID)
        delivered = state.publication(OPERATION_ID)
        self.assertEqual(PublicationState.DELIVERED, delivered.state)
        self.assertEqual("library-1", delivered.catalog_library_id)
        self.assertEqual("/jellyfin/library/radarr-tmdb-42/payload.mkv", delivered.expected_catalog_path)
        with self.assertRaisesRegex(ContractError, "conflicts"):
            state.bind_catalog_expectation(OPERATION_ID, "library-2", "/other/payload.mkv")

    def test_unobserved_catalog_pending_binding_can_follow_a_new_mount_prefix(self) -> None:
        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        state.bind_catalog_expectation(
            OPERATION_ID,
            "library-1",
            "/old/radarr-tmdb-42/payload.mkv",
        )
        state.mark_notification_attempted(OPERATION_ID)

        state.bind_catalog_expectation(
            OPERATION_ID,
            "library-1",
            "/new/radarr-tmdb-42/payload.mkv",
        )

        publication = state.publication(OPERATION_ID)
        self.assertEqual("/new/radarr-tmdb-42/payload.mkv", publication.expected_catalog_path)
        self.assertIsNone(publication.catalog_item_id)

    def test_sealed_bundle_manifest_survives_a_publisher_state_round_trip(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            (staging / "movie.en.srt").write_bytes(b"subtitle")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")
            state = PublisherState()
            state.adopt_terminal(self.terminal())

            state.mark_bundle_verified(OPERATION_ID, bundle)
            restored = PublisherState.from_records(state.records())

            self.assertEqual(bundle.payload, restored.publication(OPERATION_ID).bundle().payload)
            self.assertEqual(bundle.members, restored.publication(OPERATION_ID).bundle().members)

    def test_v012_delivered_record_is_unavailable_until_exact_receipt_binding_is_revalidated(self) -> None:
        from media_interlock.adapters.jellyfin import CatalogObservation
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Catalog:
            def observe_catalog(self, expected):
                self.expected = expected
                return CatalogObservation("jellyfin-item", "media-source", expected.internal_path, expected.expected_bytes)
            def direct_play_matches(self, observation, *, expected_bytes: int, expected_sha256: str) -> bool:
                return observation.media_source_id == "media-source" and expected_bytes == 5 and expected_sha256 == "a" * 64

        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        state.bind_catalog_expectation(OPERATION_ID, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        state.mark_notification_attempted(OPERATION_ID)
        state.mark_catalog_observed(OPERATION_ID, "jellyfin-item", "media-source")
        state.mark_catalog_delivered(OPERATION_ID)
        legacy = dict(state.records()[0])
        legacy.pop("public_conflict")
        legacy.pop("catalog_library_id")
        legacy.pop("expected_catalog_path")
        restored = PublisherState.from_records([legacy])
        service = PublisherService(restored, Store())

        self.assertEqual("unavailable", service.operation_response(OPERATION_ID).body["state"])
        self.assertTrue(service.revalidate_delivered_binding(
            OPERATION_ID, Catalog(), PathTranslation(Path("/canonical"), "library", "/jellyfin/library"), library_id="library-1",
        ))
        receipt = service.operation_response(OPERATION_ID)
        self.assertEqual("publisher_operation_receipt", receipt.kind)
        self.assertEqual("/jellyfin/library/movie.mkv", receipt.body["expected_catalog_path"])

    def test_v012_catalog_observation_without_binding_is_not_catalog_confirmed(self) -> None:
        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        state.bind_catalog_expectation(OPERATION_ID, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        state.mark_notification_attempted(OPERATION_ID)
        state.mark_catalog_observed(OPERATION_ID, "jellyfin-item", "media-source")
        legacy = dict(state.records()[0])
        legacy.pop("public_conflict")
        legacy.pop("catalog_library_id")
        legacy.pop("expected_catalog_path")
        restored = PublisherState.from_records([legacy])

        self.assertEqual("unavailable", PublisherService(restored, object()).operation_response(OPERATION_ID).body["state"])
        restored.bind_catalog_expectation(OPERATION_ID, "library-1", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        self.assertIsNone(restored.publication(OPERATION_ID).catalog_item_id)
        self.assertEqual("pending", PublisherService(restored, object()).operation_response(OPERATION_ID).body["state"])

    def test_public_conflict_is_durable_and_absorbing(self) -> None:
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        state = PublisherState()
        state.record_assisted_intent(
            operation_id=OPERATION_ID, source="radarr", upstream_id="import-42", media_id="42", expected_bytes=5,
            manifest_digest="a" * 64,
        )
        service = PublisherService(state, Store())

        self.assertTrue(service.record_operation_conflict(OPERATION_ID))
        self.assertEqual("conflict", service.operation_response(OPERATION_ID).body["state"])
        restored = PublisherState.from_records(state.records())
        self.assertEqual("conflict", PublisherService(restored, Store()).operation_response(OPERATION_ID).body["state"])

    def test_bootstrap_is_owner_bound_idempotent_and_rejects_manifest_drift(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        class Store:
            def save(self, _: PublisherState) -> None: pass

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")
            state = PublisherState()
            service = PublisherService(state, Store())
            arguments = dict(operation_id=OPERATION_ID, source="radarr", upstream_id="bootstrap-import-42", media_id="42", asset_slot="radarr:tmdb-42", item_type="Movie", provider_ids={"Tmdb": "42"}, bundle=bundle, manifest_digest="b" * 64)

            service.bootstrap_bundle(**arguments)
            service.bootstrap_bundle(**arguments)
            self.assertEqual("bootstrap", state.publication(OPERATION_ID).provenance)
            self.assertEqual(PublicationState.CANDIDATE_VERIFIED, state.publication(OPERATION_ID).state)
            with self.assertRaisesRegex(ContractError, "conflicts"):
                service.bootstrap_bundle(**(arguments | {"manifest_digest": "c" * 64}))

            absent_operation = "12345678-1234-4678-9234-567812345679"
            service.bootstrap_bundle(**(arguments | {"operation_id": absent_operation, "provider_ids": {}, "manifest_digest": "c" * 64}))
            self.assertEqual((), state.publication(absent_operation).provider_ids)

    def test_verified_bootstrap_hardlink_copies_without_fence_custody(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier
        from media_interlock.publisher.generation import AssetGenerationPublisher

        class Store:
            def save(self, _: PublisherState) -> None: pass

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "source.mkv").write_bytes(b"video")
            os.link(staging / "source.mkv", staging / "movie.mkv")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv", allow_hardlinks=True)
            state = PublisherState()
            service = PublisherService(state, Store())
            service.bootstrap_bundle(
                operation_id=OPERATION_ID,
                source="radarr",
                upstream_id="bootstrap-import-42",
                media_id="42",
                asset_slot="radarr:tmdb-42",
                item_type="Movie",
                provider_ids={"Tmdb": "42"},
                bundle=bundle,
                manifest_digest="b" * 64,
            )

            published = service.commit_asset_generation(
                OPERATION_ID,
                AssetGenerationPublisher(staging, canonical, namespace="movies"),
            )

            self.assertIsNotNone(published)
            assert published is not None
            self.assertEqual(b"video", published.read_bytes())
            self.assertNotEqual((staging / "movie.mkv").stat().st_ino, published.stat().st_ino)

    def test_assisted_intent_cannot_fabricate_fence_custody_and_requires_its_manifest(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        class Store:
            def save(self, _: PublisherState) -> None: pass

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")
            state = PublisherState()
            service = PublisherService(state, Store())
            service.record_assisted_intent(operation_id=OPERATION_ID, source="radarr", upstream_id="assisted-import-42", media_id="42", expected_bytes=bundle.bytes_verified, manifest_digest="d" * 64)

            with self.assertRaisesRegex(ContractError, "does not match"):
                service.complete_assisted_bundle(operation_id=OPERATION_ID, asset_slot="radarr:tmdb-42", item_type="Movie", provider_ids={"Tmdb": "42"}, bundle=bundle, manifest_digest="e" * 64)
            service.complete_assisted_bundle(operation_id=OPERATION_ID, asset_slot="radarr:tmdb-42", item_type="Movie", provider_ids={"Tmdb": "42"}, bundle=bundle, manifest_digest="d" * 64)
            service.complete_assisted_bundle(operation_id=OPERATION_ID, asset_slot="radarr:tmdb-42", item_type="Movie", provider_ids={"Tmdb": "42"}, bundle=bundle, manifest_digest="d" * 64)
            publication = state.publication(OPERATION_ID)
            self.assertEqual("assisted", publication.provenance)
            self.assertEqual("assisted", publication.download_id)
            self.assertEqual(PublicationState.CANDIDATE_VERIFIED, publication.state)

    def test_assisted_intent_cannot_enter_the_fence_correlation_recovery_path(self) -> None:
        from media_interlock.adapters.arr import ArrCandidate

        class Store:
            def save(self, _: PublisherState) -> None: pass

        class Correlation:
            def candidate_identity(self, *_: object) -> ArrCandidate:
                raise AssertionError("assisted intake must require its completion envelope")

        class Inspection:
            def verify(self, _: str) -> VerifiedCandidate:
                raise AssertionError("assisted intake must require its sealed manifest")

        state = PublisherState()
        service = PublisherService(state, Store())
        service.record_assisted_intent(
            operation_id=OPERATION_ID,
            source="radarr",
            upstream_id="assisted-import-42",
            media_id="42",
            expected_bytes=5,
            manifest_digest="d" * 64,
        )

        self.assertFalse(service.correlate_identify_and_verify(OPERATION_ID, Correlation(), Inspection()))
        self.assertEqual(PublicationState.CUSTODY_RESERVED, state.publication(OPERATION_ID).state)

    def test_worker_passes_the_durable_bundle_to_generation(self) -> None:
        from media_interlock.adapters.arr import ArrCandidate
        from media_interlock.publisher.filesystem import BundleVerifier, VerifiedBundle

        class Store:
            def save(self, _: PublisherState) -> None: pass

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            (staging / "movie.en.srt").write_bytes(b"subtitle")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")

            class Correlation:
                def candidate_identity(self, *_: object) -> ArrCandidate:
                    return ArrCandidate("movie.mkv", "radarr:tmdb-42", "Movie", {"Tmdb": "42"})

            class Inspection:
                def verify(self, _: str) -> VerifiedBundle:
                    return bundle

            class Generations:
                def visible_generation(self, _: str) -> None: return None
                def publish(self, _asset: str, _generation: str, candidate, **__: object) -> Path:
                    self.candidate = candidate
                    return Path("/canonical/library/radarr-tmdb-42/payload.mkv")
                def ensure_catalog_identity(self, *_: object) -> Path:
                    return Path("/canonical/library/radarr-tmdb-42/payload.mkv")

            state = PublisherState()
            service = PublisherService(state, Store())
            service.accept_terminal(self.terminal())
            self.assertTrue(service.correlate_identify_and_verify(OPERATION_ID, Correlation(), Inspection()))
            generations = Generations()
            self.assertIsNotNone(service.commit_asset_generation(OPERATION_ID, generations))
            self.assertEqual(bundle, generations.candidate)

    def test_hardlinked_bundle_stays_pending_until_exact_freeze_then_copies_with_authority(self) -> None:
        from media_interlock.adapters.arr import ArrCandidate
        from media_interlock.publisher.filesystem import BundleVerifier
        from media_interlock.publisher.service import AssetPublisherWorkProcessor, PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None: pass

        class Correlation:
            def candidate_identity(self, *_: object) -> ArrCandidate:
                return ArrCandidate("imported.mkv", "radarr:tmdb-42", "Movie", {"Tmdb": "42"})

        class Generations:
            def visible_generation(self, _: str) -> None: return None
            def publish(self, _asset: str, _generation: str, _candidate: object, **kwargs: object) -> Path:
                self.hardlink_frozen = kwargs["hardlink_frozen"]
                return Path("/canonical/library/radarr-tmdb-42/payload.mkv")
            def ensure_catalog_identity(self, *_: object, **__: object) -> Path:
                return Path("/canonical/library/radarr-tmdb-42/payload.mkv")
            def garbage_collect(self, *_: object) -> None: pass

        class Catalog:
            def submit_update(self, *_: object):
                return type("Submission", (), {"accepted": False})()
            def observe_catalog(self, *_: object): return None
            def direct_play_matches(self, *_: object, **__: object) -> bool: return False

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            (staging / "payload.mkv").write_bytes(b"video")
            os.link(staging / "payload.mkv", staging / "imported.mkv")
            state = PublisherState()
            service = PublisherService(state, Store())
            service.accept_terminal(self.terminal())
            generations = Generations()
            freezes: list[str] = []
            processor = AssetPublisherWorkProcessor(
                service, {"radarr": Correlation()}, BundleVerifier(staging, settle_seconds=0), generations,
                Catalog(), PathTranslation(Path("/canonical"), "library", "/jellyfin/library"),
                library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f", freeze=lambda operation_id: (freezes.append(operation_id) or True),
            )

            self.assertTrue(processor(OPERATION_ID))
            self.assertEqual([OPERATION_ID], freezes)
            self.assertTrue(generations.hardlink_frozen)
            self.assertTrue(state.publication(OPERATION_ID).hardlink_frozen)
            self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)

    def test_exact_catalog_delivery_requires_observation_and_direct_play(self) -> None:
        from media_interlock.adapters.arr import ArrCandidate
        from media_interlock.adapters.jellyfin import CatalogObservation, CatalogSubmission
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Correlation:
            def candidate_identity(self, _: str, __: str) -> ArrCandidate:
                return ArrCandidate("movie.mkv", "radarr:tmdb-42", "Movie", {"Tmdb": "42"})

        class Inspection:
            def verify(self, _: str) -> VerifiedCandidate:
                return VerifiedCandidate("movie.mkv", 5, "a" * 64)

        class Generations:
            def visible_generation(self, _: str) -> None:
                return None
            def publish(self, asset_slot: str, generation_id: str, _: VerifiedCandidate, **__: object) -> Path:
                self.published = (asset_slot, generation_id)
                return Path("/canonical/library/radarr-tmdb-42/payload.mkv")

        class Catalog:
            def submit_update(self, path: str, update_type: str) -> CatalogSubmission:
                self.submitted = (path, update_type)
                return CatalogSubmission(True)
            def observe_catalog(self, expected):
                self.expected = expected
                return CatalogObservation("jellyfin-item", "source-id", expected.internal_path, expected.expected_bytes)
            def direct_play_matches(self, observation, *, expected_bytes: int, expected_sha256: str) -> bool:
                self.direct = (observation, expected_bytes, expected_sha256)
                return True

        state = PublisherState()
        service = PublisherService(state, Store())
        service.accept_terminal(self.terminal())
        self.assertTrue(service.correlate_identify_and_verify(OPERATION_ID, Correlation(), Inspection()))
        generations = Generations()
        self.assertIsNotNone(service.commit_asset_generation(OPERATION_ID, generations))
        catalog = Catalog()

        self.assertTrue(service.observe_and_deliver_asset(
            OPERATION_ID, catalog, PathTranslation(Path("/canonical"), "library", "/jellyfin/library"),
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
        ))
        self.assertEqual(PublicationState.DELIVERED, state.publication(OPERATION_ID).state)
        self.assertEqual(("/jellyfin/library/movie.mkv", "created"), catalog.submitted)
        self.assertEqual("2f9e0f39-70de-4502-85ce-7ed03cd2f01f", catalog.expected.library_id)

    def test_recovery_after_uncertain_notification_observes_without_filesystem_rollback(self) -> None:
        from media_interlock.adapters.jellyfin import CatalogSubmission
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Generations:
            def publish(self, *_: object) -> Path:
                raise AssertionError("recovery must not republish or roll back catalog-pending work")

        class Catalog:
            def __init__(self) -> None:
                self.events: list[str] = []
            def submit_update(self, *_: object) -> CatalogSubmission:
                self.events.append("submit")
                return CatalogSubmission(False)
            def observe_catalog(self, _):
                self.events.append("observe")
                return None
            def direct_play_matches(self, *_: object, **__: object) -> bool:
                raise AssertionError("there is no observed item to play")

        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        state.bind_catalog_expectation(OPERATION_ID, "2f9e0f39-70de-4502-85ce-7ed03cd2f01f", "/jellyfin/library/radarr-tmdb-42/payload.mkv")
        state.mark_notification_attempted(OPERATION_ID)
        catalog = Catalog()

        PublisherService(state, Store()).recover_assets(
            Generations(), catalog, PathTranslation(Path("/canonical"), "library", "/jellyfin/library"),
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
        )
        self.assertEqual(["observe", "submit"], catalog.events)
        self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)

    def test_catalog_scan_failure_after_204_never_marks_delivery(self) -> None:
        from media_interlock.adapters.jellyfin import CatalogSubmission
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Catalog:
            def submit_update(self, *_: object) -> CatalogSubmission:
                return CatalogSubmission(True)
            def observe_catalog(self, _):
                return None  # bounded catalog scan failed internally
            def direct_play_matches(self, *_: object, **__: object) -> bool:
                raise AssertionError("no scan result may reach direct play")

        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)

        delivered = PublisherService(state, Store()).observe_and_deliver_asset(
            OPERATION_ID, Catalog(), PathTranslation(Path("/canonical"), "library", "/jellyfin/library"),
            library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f",
        )
        self.assertFalse(delivered)
        self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)

    def test_later_bounded_retry_can_adopt_catalog_after_initial_204_scan_miss(self) -> None:
        from media_interlock.adapters.jellyfin import CatalogObservation, CatalogSubmission
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Generations:
            def publish(self, *_: object, **__: object) -> Path:
                raise AssertionError("pending catalog work already has a published asset")

        class Catalog:
            visible = False
            def submit_update(self, *_: object) -> CatalogSubmission:
                return CatalogSubmission(True)
            def observe_catalog(self, expected):
                return CatalogObservation("jellyfin-item", "source-id", expected.internal_path, expected.expected_bytes) if self.visible else None
            def direct_play_matches(self, *_: object, **__: object) -> bool:
                return True

        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        service = PublisherService(state, Store())
        catalog = Catalog()
        translation = PathTranslation(Path("/canonical"), "library", "/jellyfin/library")

        self.assertFalse(service.observe_and_deliver_asset(OPERATION_ID, catalog, translation, library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f"))
        catalog.visible = True
        service.recover_assets(Generations(), catalog, translation, library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f")
        self.assertEqual(PublicationState.DELIVERED, state.publication(OPERATION_ID).state)

    def test_second_operation_for_catalog_pending_asset_cannot_replace_its_slot(self) -> None:
        second_operation = "12345678-1234-4678-9234-567812345679"

        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Generations:
            def visible_generation(self, _: str) -> None:
                raise AssertionError("pending asset must be rejected before filesystem inspection")
            def publish(self, *_: object, **__: object) -> Path:
                raise AssertionError("pending asset must not be replaced")

        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)
        state.adopt_terminal(terminal_acquisition(
            operation_id=second_operation,
            fence_reservation_id="fence:12345678-1234-4678-9234-567812345679",
            source="radarr", upstream_id="grab-43", media_id="43", bytes_reserved=400, download_id="b" * 40,
        ))
        state.mark_candidate_verified(second_operation, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(second_operation, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})

        self.assertIsNone(PublisherService(state, Store()).commit_asset_generation(second_operation, Generations()))
        self.assertEqual(PublicationState.CANDIDATE_VERIFIED, state.publication(second_operation).state)

    def test_recovery_retries_transient_arr_correlation_from_durable_custody(self) -> None:
        from media_interlock.adapters.arr import ArrCandidate
        from media_interlock.adapters.jellyfin import CatalogSubmission
        from media_interlock.publisher.service import PathTranslation

        class Store:
            def save(self, _: PublisherState) -> None: pass
        class Correlation:
            healthy = False
            def candidate_identity(self, *_: object):
                return ArrCandidate("movie.mkv", "radarr:tmdb-42", "Movie", {"Tmdb": "42"}) if self.healthy else None
        class Inspection:
            def verify(self, _: str) -> VerifiedCandidate: return VerifiedCandidate("movie.mkv", 5, "a" * 64)
        class Generations:
            def visible_generation(self, _: str) -> None: return None
            def publish(self, *_: object, **__: object) -> Path: return Path("/canonical/library/radarr-tmdb-42/payload.mkv")
        class Catalog:
            def submit_update(self, *_: object) -> CatalogSubmission: return CatalogSubmission(False)
            def observe_catalog(self, _): return None
            def direct_play_matches(self, *_: object, **__: object) -> bool: return False

        state = PublisherState()
        service = PublisherService(state, Store())
        service.accept_terminal(self.terminal())
        correlation = Correlation()
        self.assertFalse(service.correlate_identify_and_verify(OPERATION_ID, correlation, Inspection()))
        self.assertEqual(PublicationState.CUSTODY_RESERVED, state.publication(OPERATION_ID).state)
        correlation.healthy = True
        service.recover_assets(Generations(), Catalog(), PathTranslation(Path("/canonical"), "library", "/jellyfin/library"), library_id="2f9e0f39-70de-4502-85ce-7ed03cd2f01f", correlations={"radarr": correlation}, inspection=Inspection())
        self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)

    def test_correlation_derives_candidate_before_durable_verification(self) -> None:
        outer = self
        class Store:
            def save(self, _: PublisherState) -> None:
                pass

        class Correlation:
            def candidate_relative_path(self, upstream_id: str, media_id: str) -> str | None:
                outer.assertEqual("grab-42", upstream_id)
                outer.assertEqual("movie-42", media_id)
                return "movie.mkv"

        class Verifier:
            def verify(self, relative_path: str) -> VerifiedCandidate:
                outer.assertEqual("movie.mkv", relative_path)
                return VerifiedCandidate(relative_path, 5, "a" * 64)

        state = PublisherState()
        service = PublisherService(state, Store())
        service.accept_terminal(self.terminal())

        self.assertTrue(service.correlate_and_verify(OPERATION_ID, Correlation(), Verifier()))
        self.assertEqual(PublicationState.CANDIDATE_VERIFIED, state.publication(OPERATION_ID).state)


class CandidateFilesystemTests(unittest.TestCase):
    def test_bundle_verifier_seals_one_video_and_its_matching_sidecars(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            (root / "episode.mkv").write_bytes(b"video")
            (root / "episode.en.srt").write_bytes(b"english")
            (root / "episode.pt-BR.ass").write_bytes(b"portuguese")
            (root / "unrelated.mkv").write_bytes(b"other")

            bundle = BundleVerifier(root, settle_seconds=0).verify("episode.mkv")

            self.assertEqual("episode.mkv", bundle.payload.relative_path)
            self.assertEqual(("episode.en.srt", "episode.mkv", "episode.pt-BR.ass"), tuple(member.relative_path for member in bundle.members))
            self.assertEqual(len(b"videoenglishportuguese"), bundle.bytes_verified)

    def test_bundle_verifier_rejects_an_unknown_matching_sidecar(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"video")
            (root / "movie.preview.bin").write_bytes(b"unknown")

            with self.assertRaises(CandidateSafetyError):
                BundleVerifier(root, settle_seconds=0).verify("movie.mkv")

    def test_bundle_policy_requires_configured_language_and_accepts_an_alias(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"video")
            (root / "movie.por.srt").write_bytes(b"subtitle")
            verifier = BundleVerifier(root, settle_seconds=0, required_languages=("pt-br",), language_aliases={"por": "pt-br"})

            self.assertEqual("movie.mkv", verifier.verify("movie.mkv").payload.relative_path)
            with self.assertRaises(CandidateSafetyError):
                BundleVerifier(root, settle_seconds=0, required_languages=("en",)).verify("movie.mkv")

    def test_bundle_inspection_failure_and_embedded_language_evidence_are_fail_closed(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier, MediaInspection

        class Inspector:
            def inspect(self, _):
                return MediaInspection(("en",), (), ("container:mkv",))

        class FailingInspector:
            def inspect(self, _):
                raise RuntimeError("decoder unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"video")

            bundle = BundleVerifier(root, settle_seconds=0, required_languages=("en",), media_inspector=Inspector()).verify("movie.mkv")
            self.assertEqual(("en",), bundle.inspection.audio_languages)
            self.assertEqual("movie.mkv", BundleVerifier(root, settle_seconds=0, required_container_evidence=("container:mkv",)).verify("movie.mkv").payload.relative_path)
            with self.assertRaises(CandidateSafetyError):
                BundleVerifier(root, settle_seconds=0, required_container_evidence=("container:mp4",)).verify("movie.mkv")
            with self.assertRaises(CandidateSafetyError):
                BundleVerifier(root, settle_seconds=0, media_inspector=FailingInspector()).verify("movie.mkv")

    def test_subtitle_requirement_never_accepts_audio_only_language_evidence(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier, MediaInspection

        class AudioOnlyInspector:
            def inspect(self, _):
                return MediaInspection(("pt-br", "en", "es"), (), ("container:mkv",))

        class SubtitleInspector:
            def inspect(self, _):
                return MediaInspection((), ("pt-br", "en", "es"), ("container:mkv",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"video")
            required = ("pt-br", "en", "es")

            with self.assertRaises(CandidateSafetyError):
                BundleVerifier(root, settle_seconds=0, required_subtitle_languages=required, media_inspector=AudioOnlyInspector()).verify("movie.mkv")
            verified = BundleVerifier(root, settle_seconds=0, required_subtitle_languages=required, media_inspector=SubtitleInspector()).verify("movie.mkv")
            self.assertEqual(required, verified.inspection.subtitle_languages)

    def test_generation_publisher_copies_a_sealed_bundle_to_independent_inodes(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            (staging / "movie.en.srt").write_bytes(b"subtitle")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")

            payload = GenerationPublisher(staging, canonical).prepare_bundle(OPERATION_ID, bundle)

            copied_sidecar = payload.parent / "movie.en.srt"
            self.assertEqual(b"video", payload.read_bytes())
            self.assertEqual(b"subtitle", copied_sidecar.read_bytes())
            self.assertNotEqual((staging / "movie.mkv").stat().st_ino, payload.stat().st_ino)
            self.assertNotEqual((staging / "movie.en.srt").stat().st_ino, copied_sidecar.stat().st_ino)

    def test_hardlinked_bundle_requires_frozen_copy_authority_and_never_reuses_the_inode(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "payload.mkv").write_bytes(b"video")
            os.link(staging / "payload.mkv", staging / "movie.mkv")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv", allow_hardlinks=True)

            with self.assertRaises(CandidateSafetyError):
                GenerationPublisher(staging, canonical).prepare_bundle(OPERATION_ID, bundle)
            payload = GenerationPublisher(staging, canonical).prepare_bundle(OPERATION_ID, bundle, allow_hardlinks=True)

            self.assertEqual(b"video", payload.read_bytes())
            self.assertNotEqual((staging / "movie.mkv").stat().st_ino, payload.stat().st_ino)

    def test_asset_publisher_exposes_a_complete_bundle_only_after_copy(self) -> None:
        from media_interlock.publisher.filesystem import BundleVerifier
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            (staging / "movie.en.srt").write_bytes(b"subtitle")
            bundle = BundleVerifier(staging, settle_seconds=0).verify("movie.mkv")

            payload = AssetGenerationPublisher(staging, canonical, namespace="library").publish("radarr:movie-a", OPERATION_ID, bundle)

            self.assertEqual(b"video", payload.read_bytes())
            self.assertEqual(b"subtitle", payload.with_name("movie.en.srt").read_bytes())
            self.assertNotEqual((staging / "movie.mkv").stat().st_ino, payload.stat().st_ino)

    def test_asset_publisher_seals_provider_identity_as_jellyfin_nfo(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")

            payload = AssetGenerationPublisher(
                staging, canonical, namespace="library"
            ).publish(
                "radarr:tmdb-45745",
                OPERATION_ID,
                CandidateVerifier(staging).verify("movie.mkv"),
                item_type="Movie",
                provider_ids={"Tmdb": "45745"},
            )

            nfo = payload.with_suffix(".nfo")
            self.assertEqual(0o444, nfo.stat().st_mode & 0o777)
            self.assertIn(b'<uniqueid type="tmdb" default="true">45745</uniqueid>', nfo.read_bytes())

    def test_catalog_pending_generation_repairs_a_missing_identity_sidecar(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "movie.mkv").write_bytes(b"video")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")
            payload = publisher.publish(
                "radarr:tmdb-45745",
                OPERATION_ID,
                CandidateVerifier(staging).verify("movie.mkv"),
            )
            self.assertFalse(payload.with_suffix(".nfo").exists())

            payload = publisher.ensure_catalog_identity(
                "radarr:tmdb-45745",
                OPERATION_ID,
                "Movie",
                {"Tmdb": "45745"},
                candidate_relative_path="movie.mkv",
            )

            self.assertIn(b">45745</uniqueid>", payload.with_suffix(".nfo").read_bytes())

    def test_catalog_pending_recovery_replaces_a_legacy_slot_with_the_arr_relative_route(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            candidate_path = "Show (2021)/Season 03/Show - S03E06.mkv"
            source = staging / candidate_path
            source.parent.mkdir(parents=True)
            canonical.mkdir()
            source.write_bytes(b"episode")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")
            publisher.publish(
                "sonarr:tvdb-11872046",
                OPERATION_ID,
                CandidateVerifier(staging).verify(candidate_path),
            )

            (canonical / "library" / candidate_path).unlink()
            (canonical / ".publisher" / "visible" / "library" / "sonarr-tvdb-11872046").unlink()
            legacy = canonical / "library" / "sonarr-tvdb-11872046"
            legacy.symlink_to(
                Path("..") / ".publisher" / "assets" / "sonarr-tvdb-11872046" / "generations" / OPERATION_ID
            )

            recovered = publisher.ensure_catalog_identity(
                "sonarr:tvdb-11872046",
                OPERATION_ID,
                "Episode",
                {"Tvdb": "11872046"},
                candidate_relative_path=candidate_path,
            )

            self.assertEqual(canonical / "library" / candidate_path, recovered)
            self.assertEqual(b"episode", recovered.read_bytes())
            self.assertIn(b">11872046</uniqueid>", recovered.with_suffix(".nfo").read_bytes())
            self.assertFalse(recovered.is_symlink())
            self.assertEqual(
                publisher.generation_payload("sonarr:tvdb-11872046", OPERATION_ID).stat().st_ino,
                recovered.stat().st_ino,
            )
            self.assertFalse(legacy.exists())
            self.assertEqual(OPERATION_ID, publisher.visible_generation("sonarr:tvdb-11872046"))

    def test_asset_slots_keep_unrelated_assets_visible_across_updates(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            movie_a = staging / "a.mkv"
            movie_b = staging / "b.mkv"
            movie_a.write_bytes(b"a-v1")
            movie_b.write_bytes(b"b-v1")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")

            publisher.publish("radarr:movie-a", OPERATION_ID, CandidateVerifier(staging).verify("a.mkv"))
            publisher.publish("radarr:movie-b", "12345678-1234-4678-9234-567812345679", CandidateVerifier(staging).verify("b.mkv"))
            movie_a.write_bytes(b"a-v2")
            publisher.publish("radarr:movie-a", "12345678-1234-4678-9234-567812345680", CandidateVerifier(staging).verify("a.mkv"), previous_generation_id=OPERATION_ID)

            self.assertEqual(b"a-v2", (canonical / "library" / "a.mkv").read_bytes())
            self.assertEqual(b"b-v1", (canonical / "library" / "b.mkv").read_bytes())

    def test_each_asset_retains_its_own_last_known_good_bundle(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            movie = staging / "movie.mkv"
            episode = staging / "episode.mkv"
            movie.write_bytes(b"movie-v1")
            episode.write_bytes(b"episode-v1")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")
            movie_v1 = OPERATION_ID
            episode_v1 = "12345678-1234-4678-9234-567812345679"
            movie_v2 = "12345678-1234-4678-9234-567812345680"
            publisher.publish("radarr:movie-a", movie_v1, CandidateVerifier(staging).verify("movie.mkv"))
            publisher.publish("sonarr:episode-b", episode_v1, CandidateVerifier(staging).verify("episode.mkv"))
            movie.write_bytes(b"movie-v2")
            publisher.publish("radarr:movie-a", movie_v2, CandidateVerifier(staging).verify("movie.mkv"), previous_generation_id=movie_v1)

            self.assertEqual(movie_v2, publisher.visible_generation("radarr:movie-a"))
            self.assertEqual(episode_v1, publisher.visible_generation("sonarr:episode-b"))
            self.assertEqual(b"movie-v1", publisher.generation_payload("radarr:movie-a", movie_v1).read_bytes())
            self.assertEqual(b"episode-v1", publisher.generation_payload("sonarr:episode-b", episode_v1).read_bytes())

    def test_asset_publisher_rejects_a_symlinked_private_root_before_writing_through_it(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            outside = Path(directory) / "outside"
            staging.mkdir()
            canonical.mkdir()
            outside.mkdir()
            (staging / "movie.mkv").write_bytes(b"movie")
            (canonical / ".publisher").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(CandidateSafetyError):
                AssetGenerationPublisher(staging, canonical, namespace="library").publish(
                    "radarr:movie-a", OPERATION_ID, CandidateVerifier(staging).verify("movie.mkv")
                )
            self.assertEqual([], list(outside.iterdir()))

    def test_asset_gc_releases_only_delivered_predecessor(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            candidate = staging / "movie.mkv"
            candidate.write_bytes(b"v1")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")
            first = OPERATION_ID
            second = "12345678-1234-4678-9234-567812345679"
            publisher.publish("radarr:movie-a", first, CandidateVerifier(staging).verify("movie.mkv"))
            candidate.write_bytes(b"v2")
            publisher.publish("radarr:movie-a", second, CandidateVerifier(staging).verify("movie.mkv"), previous_generation_id=first)

            publisher.garbage_collect("radarr:movie-a", {second})
            with self.assertRaises(CandidateSafetyError):
                publisher.generation_payload("radarr:movie-a", first)
            self.assertEqual(b"v2", publisher.generation_payload("radarr:movie-a", second).read_bytes())

    def test_asset_bundle_exposes_a_stable_payload_for_nested_arr_imports(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            nested = staging / "season" / "episode.mkv"
            nested.parent.mkdir(parents=True)
            canonical.mkdir()
            nested.write_bytes(b"nested-media")

            exposed = AssetGenerationPublisher(staging, canonical, namespace="library").publish(
                "sonarr:tvdb-99", OPERATION_ID, CandidateVerifier(staging).verify("season/episode.mkv")
            )

            self.assertEqual(b"nested-media", exposed.read_bytes())

    def test_asset_recovery_repairs_playback_modes_after_publication_boundary_crash(self) -> None:
        from media_interlock.publisher.generation import AssetGenerationPublisher

        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            staging.mkdir()
            canonical.mkdir()
            (staging / "movie.mkv").write_bytes(b"media")
            publisher = AssetGenerationPublisher(staging, canonical, namespace="library")
            verified = CandidateVerifier(staging).verify("movie.mkv")
            payload = publisher.publish("radarr:movie-a", OPERATION_ID, verified)
            payload.chmod(0o600)
            payload.parent.chmod(0o700)

            recovered = publisher.publish("radarr:movie-a", OPERATION_ID, verified)
            self.assertEqual(0o444, recovered.stat().st_mode & 0o777)
            private_payload = publisher.generation_payload("radarr:movie-a", OPERATION_ID)
            self.assertEqual(0o755, private_payload.parent.stat().st_mode & 0o777)
    def test_canonical_writer_lock_excludes_a_second_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "canonical"
            root.mkdir()
            first = CanonicalWriterLock.acquire(root)
            self.addCleanup(first.close)
            with self.assertRaises(CandidateSafetyError):
                CanonicalWriterLock.acquire(root)
    def test_verifies_regular_contained_candidate_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            candidate = root / "movie.mkv"
            candidate.write_bytes(b"synthetic-media")

            verified = CandidateVerifier(root).verify("movie.mkv")

            self.assertEqual("movie.mkv", verified.relative_path)
            self.assertEqual(len(b"synthetic-media"), verified.bytes_verified)

    def test_rejects_traversal_symlink_and_nonregular_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"outside")
            (root / "link.mkv").symlink_to(outside)
            (root / "folder").mkdir()
            verifier = CandidateVerifier(root)

            for relative in ("../outside.mkv", "link.mkv", "folder"):
                with self.subTest(relative=relative), self.assertRaises(CandidateSafetyError):
                    verifier.verify(relative)

    def test_rejects_a_symlinked_canonical_generations_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            outside = Path(directory) / "outside"
            staging.mkdir()
            canonical.mkdir()
            outside.mkdir()
            (staging / "movie.mkv").write_bytes(b"synthetic-media")
            (canonical / "generations").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(CandidateSafetyError):
                GenerationPublisher(staging, canonical).prepare(OPERATION_ID, CandidateVerifier(staging).verify("movie.mkv"))

    def test_prepared_nested_candidate_is_idempotently_recovered_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            canonical = Path(directory) / "canonical"
            nested = staging / "season" / "episode.mkv"
            nested.parent.mkdir(parents=True)
            canonical.mkdir()
            nested.write_bytes(b"synthetic-media")
            publisher = GenerationPublisher(staging, canonical)
            verified = CandidateVerifier(staging).verify("season/episode.mkv")
            prepared = publisher.prepare(OPERATION_ID, verified)
            nested.unlink()

            self.assertEqual(prepared, publisher.prepare(OPERATION_ID, verified))
