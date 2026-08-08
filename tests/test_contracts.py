from __future__ import annotations

import unittest
import uuid

import _source_tree  # noqa: F401

from media_interlock.contracts import (
    ContractError,
    CustodyLedger,
    Envelope,
    StatusCode,
    custody_receipt,
    terminal_acquisition,
)


OPERATION_ID = str(uuid.UUID("12345678-1234-4678-9234-567812345678"))


class ContractTests(unittest.TestCase):
    def test_terminal_observation_round_trips_without_path_authority(self) -> None:
        envelope = terminal_acquisition(
            operation_id=OPERATION_ID,
            fence_reservation_id="fence-r-1",
            source="radarr",
            upstream_id="grab-42",
            media_id="movie-42",
            bytes_reserved=4096,
        )

        decoded = Envelope.decode(envelope.encode())

        self.assertEqual(envelope, decoded)
        self.assertNotIn("path", envelope.body)
        self.assertEqual(StatusCode.OK, StatusCode.OK)

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
