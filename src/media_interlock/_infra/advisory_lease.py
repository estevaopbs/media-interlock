"""A narrowly scoped, deployment-supplied qBittorrent mutation lease."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path


class LeaseUnavailable(RuntimeError):
    """The configured lease cannot safely serialize a mutation."""


@dataclass(frozen=True)
class LeaseIdentity:
    device: int
    inode: int


class _HeldLease:
    def __init__(self, lease: "AdvisoryLease") -> None:
        self._lease = lease

    def __enter__(self) -> "AdvisoryLease":
        return self._lease

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._lease._release()


class AdvisoryLease:
    """Pins one regular, singly linked inode and takes bounded ``flock`` holds."""

    def __init__(self, path: Path, descriptor: int, identity: LeaseIdentity, timeout_ms: int) -> None:
        self._path = path
        self._descriptor = descriptor
        self.identity = identity
        self._timeout_ms = timeout_ms
        self._held = False

    @classmethod
    def open(cls, path: Path, *, timeout_ms: int) -> "AdvisoryLease":
        if not isinstance(path, Path) or not path.is_absolute() or not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0 or timeout_ms > 60_000:
            raise LeaseUnavailable("shared mutation lease configuration is invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise LeaseUnavailable("shared mutation lease is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LeaseUnavailable("shared mutation lease is not a single regular file")
            identity = LeaseIdentity(metadata.st_dev, metadata.st_ino)
            result = cls(path, descriptor, identity, timeout_ms)
            result._verify_identity()
            return result
        except Exception:
            os.close(descriptor)
            raise

    def _verify_identity(self) -> None:
        try:
            descriptor_metadata = os.fstat(self._descriptor)
            path_metadata = os.lstat(self._path)
        except OSError as exc:
            raise LeaseUnavailable("shared mutation lease identity is unavailable") from exc
        if (
            not stat.S_ISREG(descriptor_metadata.st_mode)
            or descriptor_metadata.st_nlink != 1
            or not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (self.identity.device, self.identity.inode)
            or (path_metadata.st_dev, path_metadata.st_ino) != (self.identity.device, self.identity.inode)
        ):
            raise LeaseUnavailable("shared mutation lease identity drifted")

    def acquire(self) -> _HeldLease:
        if self._held:
            raise LeaseUnavailable("shared mutation lease is already held")
        self._verify_identity()
        deadline = time.monotonic() + self._timeout_ms / 1000
        while True:
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LeaseUnavailable("shared mutation lease is busy") from exc
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            except OSError as exc:
                raise LeaseUnavailable("shared mutation lease cannot be acquired") from exc
        try:
            self._verify_identity()
        except Exception:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            raise
        self._held = True
        return _HeldLease(self)

    def probe(self) -> tuple[bool, int | None, int | None]:
        """Boundedly prove the configured inode is available without an effect."""
        try:
            with self.acquire():
                return True, self.identity.device, self.identity.inode
        except LeaseUnavailable:
            return False, None, None

    def _release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            raise LeaseUnavailable("shared mutation lease cannot be released") from exc

    def close(self) -> None:
        self._release()
        if self._descriptor >= 0:
            descriptor, self._descriptor = self._descriptor, -1
            os.close(descriptor)
