from __future__ import annotations

import unittest
import uuid
import tempfile
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

    def test_catalog_submission_and_observation_are_distinct_durable_boundaries(self) -> None:
        state = PublisherState()
        state.adopt_terminal(self.terminal())
        state.mark_candidate_verified(OPERATION_ID, "movie.mkv", 5, "a" * 64)
        state.bind_asset_identity(OPERATION_ID, "radarr:tmdb-42", "Movie", {"Tmdb": "42"})
        state.record_generation_intent(OPERATION_ID, None)
        state.mark_generation_committed(OPERATION_ID)

        state.mark_notification_attempted(OPERATION_ID)
        pending = state.publication(OPERATION_ID)
        self.assertEqual(PublicationState.CATALOG_PENDING, pending.state)
        self.assertTrue(pending.notification_attempted)
        state.mark_catalog_observed(OPERATION_ID, "jellyfin-item", "media-source")
        self.assertEqual(PublicationState.CATALOG_PENDING, state.publication(OPERATION_ID).state)
        self.assertEqual("jellyfin-item", state.publication(OPERATION_ID).catalog_item_id)
        state.mark_catalog_delivered(OPERATION_ID)
        self.assertEqual(PublicationState.DELIVERED, state.publication(OPERATION_ID).state)

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
        self.assertEqual(("/jellyfin/library/radarr-tmdb-42/payload.mkv", "created"), catalog.submitted)
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

            self.assertEqual(b"a-v2", (canonical / "library" / "radarr-movie-a" / "payload.mkv").read_bytes())
            self.assertEqual(b"b-v1", (canonical / "library" / "radarr-movie-b" / "payload.mkv").read_bytes())

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
            self.assertEqual(0o755, recovered.parent.stat().st_mode & 0o777)
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
