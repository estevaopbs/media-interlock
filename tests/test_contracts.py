from __future__ import annotations

import unittest
import uuid

import _source_tree  # noqa: F401

from media_interlock.contracts import (
    ContractError,
    CustodyLedger,
    Envelope,
    StatusCode,
    acquisition_grab_binding,
    acquisition_pre_admission,
    custody_receipt,
    quiesce_request,
    publisher_assisted_complete,
    publisher_assisted_intent,
    publisher_bootstrap,
    publisher_operation_query,
    publisher_operation_receipt,
    publisher_operation_status,
    terminal_acquisition,
)


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))


class ContractTests(unittest.TestCase):
    def test_publisher_operation_contract_separates_nonterminal_status_from_terminal_receipt(self) -> None:
        query = publisher_operation_query(OPERATION_ID)
        pending = publisher_operation_status(OPERATION_ID, "pending")
        receipt = publisher_operation_receipt(
            OPERATION_ID,
            source="radarr",
            upstream_id="import-42",
            media_id="42",
            asset_slot="radarr:tmdb-42",
            generation_id=OPERATION_ID,
            generation_sha256="a" * 64,
            library_id="library-1",
            item_id="item-1",
            media_source_id="source-1",
            expected_catalog_path="/jellyfin/library/radarr-tmdb-42/payload.mkv",
        )

        self.assertEqual({}, dict(query.body))
        self.assertEqual({"state": "pending"}, dict(pending.body))
        self.assertEqual("publisher_operation_receipt", receipt.kind)
        self.assertEqual("visible-confirmed", receipt.body["state"])
        self.assertEqual(receipt, Envelope.decode(receipt.encode()))
        for state in ("accepted", "pending", "catalog-confirmed", "conflict", "unavailable"):
            self.assertEqual(state, publisher_operation_status(OPERATION_ID, state).body["state"])
        with self.assertRaises(ContractError):
            publisher_operation_status(OPERATION_ID, "visible-confirmed")
        with self.assertRaises(ContractError):
            Envelope("v1", "publisher_operation_receipt", OPERATION_ID, dict(receipt.body) | {"generation_sha256": "b"})

    def test_owner_bound_bootstrap_and_assisted_contracts_require_one_exact_manifest(self) -> None:
        manifest = {
            "source": "radarr", "upstream_id": "import-42", "media_id": "42", "asset_slot": "radarr:tmdb-42",
            "item_type": "Movie", "provider_ids": {"Tmdb": "42"}, "candidate_relative_path": "movie.mkv",
            "bundle_members": [{"path": "movie.mkv", "bytes": 5, "allocated": 4096, "device": 1, "inode": 2, "modified_ns": 3, "sha256": "a" * 64}],
            "inspection": {"audio_languages": [], "subtitle_languages": [], "container_evidence": ["container:mkv"]},
            "expected_catalog_path": "/jellyfin/library/radarr-tmdb-42/payload.mkv",
        }

        bootstrap = publisher_bootstrap(operation_id=OPERATION_ID, manifest=manifest)
        assisted = publisher_assisted_complete(operation_id=OPERATION_ID, manifest=manifest)
        intent = publisher_assisted_intent(operation_id=OPERATION_ID, source="radarr", upstream_id="import-42", media_id="42", expected_bytes=5, manifest_sha256=bootstrap.body["manifest_sha256"])

        self.assertEqual("publisher_bootstrap", bootstrap.kind)
        self.assertEqual("publisher_assisted_complete", assisted.kind)
        self.assertEqual("publisher_assisted_intent", intent.kind)
        with self.assertRaises(ContractError):
            Envelope("v1", "publisher_bootstrap", OPERATION_ID, bootstrap.body | {"manifest_sha256": "b" * 64})
        self.assertIsNone(publisher_bootstrap(operation_id=OPERATION_ID, manifest=manifest | {"provider_ids": None}).body["manifest"]["provider_ids"])
    def test_quiescence_request_is_a_versioned_empty_authority_toggle(self) -> None:
        request = quiesce_request(OPERATION_ID, enabled=True)

        self.assertEqual("quiesce", request.kind)
        self.assertEqual({"enabled": True}, dict(request.body))
        with self.assertRaises(ContractError):
            Envelope("v1", "quiesce", OPERATION_ID, {"enabled": True, "hash": "a" * 40})

    def test_arr_observed_grab_binds_real_download_identity_without_locator(self) -> None:
        binding = acquisition_grab_binding(
            operation_id=OPERATION_ID,
            download_id="0123456789ABCDEF0123456789ABCDEF01234567",
            torrent_hash="0123456789abcdef0123456789abcdef01234567",
        )

        self.assertEqual(binding, Envelope.decode(binding.encode()))
        self.assertEqual({"download_id", "torrent_hash"}, set(binding.body))
        with self.assertRaises(ContractError):
            acquisition_grab_binding(
                operation_id=OPERATION_ID,
                download_id="B" * 40,
                torrent_hash="a" * 40,
            )
    def test_pre_admission_has_no_locator_and_terminal_carries_real_arr_download_id(self) -> None:
        pre_admission = acquisition_pre_admission(
            operation_id=OPERATION_ID,
            source="radarr",
            media_id="42",
            selector_fingerprint="a" * 64,
            expected_bytes=4096,
            watermark="2026-08-08T12:00:00Z",
        )
        self.assertEqual("acquisition_pre_admission", pre_admission.kind)
        self.assertNotIn("source_locator", pre_admission.body)
        terminal = terminal_acquisition(
            operation_id=OPERATION_ID,
            fence_reservation_id="fence:12345678-1234-4678-9234-567812345678",
            source="radarr",
            upstream_id="grab-42",
            media_id="42",
            download_id="0123456789abcdef0123456789abcdef01234567",
            bytes_reserved=4096,
        )
        self.assertEqual("0123456789abcdef0123456789abcdef01234567", terminal.body["download_id"])
    def test_terminal_observation_round_trips_without_path_authority(self) -> None:
        envelope = terminal_acquisition(
            operation_id=OPERATION_ID,
            fence_reservation_id="fence-r-1",
            source="radarr",
            upstream_id="grab-42",
            media_id="movie-42",
            bytes_reserved=4096,
            download_id="a" * 40,
        )

        decoded = Envelope.decode(envelope.encode())

        self.assertEqual(envelope, decoded)
        self.assertNotIn("path", envelope.body)
        self.assertEqual(StatusCode.OK, StatusCode.OK)
        with self.assertRaises(ContractError):
            Envelope("v1", "terminal_acquisition", OPERATION_ID, {
                "bytes_reserved": 4096,
                "fence_reservation_id": "fence-r-1",
                "media_id": "movie-42",
                "source": "radarr",
                "upstream_id": "grab-42",
            })

    def test_contract_rejects_unknown_fields_incompatible_versions_and_invalid_ids(self) -> None:
        with self.assertRaisesRegex(ContractError, "unknown envelope fields"):
            Envelope.from_mapping(
                {"version": "v1", "kind": "status", "operation_id": OPERATION_ID, "body": {}, "extra": True}
            )
        with self.assertRaisesRegex(ContractError, "unsupported contract version"):
            Envelope.from_mapping(
                {"version": "v9", "kind": "status", "operation_id": OPERATION_ID, "body": {}}
            )
        with self.assertRaisesRegex(ContractError, "canonical UUID"):
            Envelope.from_mapping(
                {"version": "v1", "kind": "status", "operation_id": "not-an-id", "body": {}}
            )
        with self.assertRaisesRegex(ContractError, "unknown terminal acquisition fields"):
            Envelope.from_mapping(
                {"version": "v1", "kind": "terminal_acquisition", "operation_id": OPERATION_ID, "body": {"unknown": True}}
            )
        with self.assertRaisesRegex(ContractError, "unsupported contract version"):
            Envelope("v9", "status", OPERATION_ID, {"code": "ok", "message": "ready"})

    def test_contract_rejects_noncanonical_or_duplicate_json(self) -> None:
        duplicate = b'{"body":{},"body":{},"kind":"status","operation_id":"12345678-1234-4678-9234-567812345678","version":"v1"}\n'
        unordered = b'{"version":"v1","kind":"status","operation_id":"12345678-1234-4678-9234-567812345678","body":{"code":"ok","message":"ready"}}\n'

        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            Envelope.decode(duplicate)
        with self.assertRaisesRegex(ContractError, "canonical JSON"):
            Envelope.decode(unordered)

    def test_malformed_contract_field_types_always_raise_contract_error(self) -> None:
        for value in (
            {"version": "v1", "kind": [], "operation_id": OPERATION_ID, "body": {}},
            {"version": "v1", "kind": "status", "operation_id": OPERATION_ID, "body": {"code": [], "message": "ready"}},
            {"version": "v1", "kind": "terminal_acquisition", "operation_id": OPERATION_ID, "body": {"bytes_reserved": 1, "fence_reservation_id": "r", "media_id": "m", "source": [], "upstream_id": "u"}},
        ):
            with self.subTest(value=value), self.assertRaises(ContractError):
                Envelope.from_mapping(value)

    def test_every_custody_crash_boundary_conserves_a_reservation(self) -> None:
        ledger = CustodyLedger(OPERATION_ID, "fence-r-1")
        checkpoints = [ledger.snapshot()]
        ledger.announce_terminal()
        checkpoints.append(ledger.snapshot())
        ledger.reserve_publisher("publisher-r-9")
        checkpoints.append(ledger.snapshot())
        receipt = custody_receipt(OPERATION_ID, "fence-r-1", "publisher-r-9")
        ledger.accept_receipt(receipt)
        checkpoints.append(ledger.snapshot())

        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                self.assertGreaterEqual(checkpoint.fence_count + checkpoint.publisher_count, 1)

    def test_receipt_must_exactly_match_the_durable_custody_reservation(self) -> None:
        ledger = CustodyLedger(OPERATION_ID, "fence-r-1")
        ledger.announce_terminal()
        ledger.reserve_publisher("publisher-r-9")
        receipt = custody_receipt(OPERATION_ID, "fence-r-1", "publisher-r-other")

        with self.assertRaisesRegex(ContractError, "does not match"):
            ledger.accept_receipt(receipt)

    def test_receipt_cannot_release_a_different_operation(self) -> None:
        ledger = CustodyLedger(OPERATION_ID, "fence-r-1")
        ledger.announce_terminal()
        ledger.reserve_publisher("publisher-r-9")
        receipt = custody_receipt(str(uuid.UUID("87654321-4321-4678-9234-567812345678")), "fence-r-1", "publisher-r-9")

        with self.assertRaisesRegex(ContractError, "does not match"):
            ledger.accept_receipt(receipt)
