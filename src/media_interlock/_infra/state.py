"""Private exclusive SQLite state with durable transition writes."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import uuid
from pathlib import Path


class StoreBusyError(RuntimeError):
    """Raised when another process owns a component state directory."""


class StoreOwnershipError(RuntimeError):
    """Raised when a state directory belongs to another component identity."""


class SqliteStore:
    def __init__(self, connection: sqlite3.Connection, lock_descriptor: int) -> None:
        self._connection = connection
        self._lock_descriptor = lock_descriptor
        self._closed = False

    @classmethod
    def open(cls, state_dir: Path, owner: str) -> "SqliteStore":
        if not owner or "/" in owner:
            raise ValueError("store owner must be a simple non-empty identity")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_descriptor = os.open(state_dir / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_descriptor)
            raise StoreBusyError("state directory already has an exclusive writer") from exc
        try:
            database_path = state_dir / "state.sqlite3"
            marker_owner = cls._read_owner_marker(state_dir)
            if marker_owner is None:
                if database_path.exists():
                    raise StoreOwnershipError("state directory has an unrecognized existing store")
                cls._claim_owner_marker(state_dir, owner)
                marker_owner = owner
            if marker_owner != owner:
                raise StoreOwnershipError("state directory belongs to a different component identity")
            if not database_path.exists():
                cls._initialize_database(state_dir, database_path, owner)
            connection = sqlite3.connect(database_path, isolation_level=None)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "metadata" not in tables or "values_store" not in tables:
                raise StoreOwnershipError("state directory has an unrecognized existing store")
            existing = connection.execute("SELECT value FROM metadata WHERE key = 'owner'").fetchone()
            if existing is None or existing[0] != marker_owner:
                raise StoreOwnershipError("state directory belongs to a different component identity")
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal_mode is None or journal_mode[0].lower() != "wal":
                raise RuntimeError("SQLite WAL durability is unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if synchronous is None or synchronous[0] != 2:
                raise RuntimeError("SQLite full synchronous durability is unavailable")
            return cls(connection, lock_descriptor)
        except BaseException:
            try:
                connection.close()
            except UnboundLocalError:
                pass
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
            raise

    @staticmethod
    def _read_owner_marker(state_dir: Path) -> str | None:
        try:
            descriptor = os.open(state_dir / ".owner", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreOwnershipError("state directory has an unsafe owner marker") from exc
        try:
            value = os.read(descriptor, 256).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreOwnershipError("state directory has an unreadable owner marker") from exc
        finally:
            os.close(descriptor)
        if not value.endswith("\n") or not value[:-1] or "\n" in value[:-1] or "/" in value[:-1]:
            raise StoreOwnershipError("state directory has an invalid owner marker")
        return value[:-1]

    @staticmethod
    def _claim_owner_marker(state_dir: Path, owner: str) -> None:
        temporary_name = f".owner.initializing.{uuid.uuid4().hex}"
        temporary_path = state_dir / temporary_name
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, f"{owner}\n".encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary_path, state_dir / ".owner")
        except FileExistsError:
            marker_owner = SqliteStore._read_owner_marker(state_dir)
            if marker_owner != owner:
                raise StoreOwnershipError("state directory belongs to a different component identity")
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        descriptor = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _initialize_database(state_dir: Path, database_path: Path, owner: str) -> None:
        temporary_path = state_dir / f".state.initializing.{uuid.uuid4().hex}.sqlite3"
        try:
            connection = sqlite3.connect(temporary_path, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("CREATE TABLE values_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO metadata(key, value) VALUES ('owner', ?)", (owner,))
                connection.execute("COMMIT")
            finally:
                connection.close()
            descriptor = os.open(temporary_path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary_path, database_path)
            descriptor = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def put(self, key: str, value: str) -> None:
        if self._closed:
            raise RuntimeError("store is closed")
        begun = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            begun = True
            self._connection.execute("INSERT OR REPLACE INTO values_store(key, value) VALUES (?, ?)", (key, value))
            self._connection.execute("COMMIT")
        except BaseException:
            if begun:
                self._connection.execute("ROLLBACK")
            raise

    def get(self, key: str) -> str | None:
        if self._closed:
            raise RuntimeError("store is closed")
        result = self._connection.execute("SELECT value FROM values_store WHERE key = ?", (key,)).fetchone()
        return None if result is None else str(result[0])

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        os.close(self._lock_descriptor)
        self._closed = True

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
