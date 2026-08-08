from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock._infra.safe_fs import PathSafetyError, atomic_write_under, path_under
from media_interlock._infra.state import StoreBusyError, StoreOwnershipError, SqliteStore
from media_interlock._infra.unix_rpc import FrameError, decode_frame, encode_frame, read_frame, write_frame
from media_interlock.cli import render_result


class InfrastructureTests(unittest.TestCase):
    def test_store_persists_and_refuses_a_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            first = SqliteStore.open(state_dir, "fence")
            self.addCleanup(first.close)
            first.put("reservation", "r-1")

            with self.assertRaises(StoreBusyError):
                SqliteStore.open(state_dir, "fence")

            first.close()
            reopened = SqliteStore.open(state_dir, "fence")
            self.addCleanup(reopened.close)
            self.assertEqual("r-1", reopened.get("reservation"))

    def test_foreign_or_incomplete_store_is_not_mutated_during_ownership_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = state_dir / "state.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO metadata(key, value) VALUES ('owner', 'fence')")
            connection.commit()
            connection.close()
            (state_dir / ".owner").write_text("fence\n", encoding="utf-8")
            journal = state_dir / "state.sqlite3-journal"
            journal.write_bytes(b"untrusted pending journal")
            database_before = database.read_bytes()

            with self.assertRaises(StoreOwnershipError):
                SqliteStore.open(state_dir, "publisher")

            self.assertEqual(database_before, database.read_bytes())
            self.assertTrue(journal.exists())

    def test_stale_initialization_temporary_file_never_bricks_store_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            (state_dir / ".state.initializing.sqlite3").write_bytes(b"partial")

            store = SqliteStore.open(state_dir, "fence")
            self.addCleanup(store.close)
            store.put("recovered", "yes")
            self.assertEqual("yes", store.get("recovered"))

    def test_atomic_write_and_path_containment_reject_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            target = root / "generation/movie.mkv"
            atomic_write_under(root, "generation/movie.mkv", b"media")
            self.assertEqual(b"media", target.read_bytes())
            with self.assertRaises(PathSafetyError):
                path_under(root, "../escape")
            outside = Path(directory) / "outside"
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(PathSafetyError):
                path_under(root, "linked/escape")
            with self.assertRaises(OSError):
                atomic_write_under(root, "linked/escape", b"media")

    def test_unix_rpc_frame_is_bounded_canonical_json(self) -> None:
        payload = {"b": 2, "a": 1}
        self.assertEqual(payload, decode_frame(encode_frame(payload)))
        with self.assertRaises(FrameError):
            decode_frame(b'{"a":1}\ntrailing')
        with self.assertRaises(FrameError):
            encode_frame({"blob": "x" * 1024}, maximum_bytes=16)
        with self.assertRaisesRegex(FrameError, "canonical JSON"):
            decode_frame(b'{"b":2,"a":1}\n')
        with self.assertRaisesRegex(FrameError, "duplicate JSON key"):
            decode_frame(b'{"a":1,"a":2}\n')

    def test_json_cli_result_has_stable_status_without_secret_payloads(self) -> None:
        rendered = render_result("inhibited", "publisher custody is unavailable", as_json=True)

        self.assertEqual(
            {"version": "v1", "status": "inhibited", "message": "publisher custody is unavailable"},
            json.loads(rendered),
        )


class UnixSocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unix_socket_round_trips_one_bounded_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "contract.sock"

            async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                request = await read_frame(reader)
                await write_frame(writer, {"accepted": request["operation_id"]})
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handler, path=socket_path)
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                await write_frame(writer, {"operation_id": "op-1"})
                self.assertEqual({"accepted": "op-1"}, await read_frame(reader))
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()
