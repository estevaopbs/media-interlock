"""Contained, no-follow candidate inspection for Publisher."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath


class CandidateSafetyError(ValueError):
    """Raised when a candidate cannot be safely derived under staging."""


@dataclass(frozen=True)
class VerifiedCandidate:
    relative_path: str
    bytes_verified: int
    sha256: str


class CandidateVerifier:
    def __init__(self, staging_root: Path) -> None:
        self._root = staging_root

    def verify(self, relative_path: str) -> VerifiedCandidate:
        relative = PurePath(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise CandidateSafetyError("candidate path must be a contained relative path")
        try:
            root_descriptor = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise CandidateSafetyError("staging root is unavailable or unsafe") from exc
        descriptor = root_descriptor
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            file_descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            try:
                metadata = os.fstat(file_descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size <= 0:
                    raise CandidateSafetyError("candidate is not one safe regular file")
                digest = hashlib.sha256()
                total = 0
                while chunk := os.read(file_descriptor, 1024 * 1024):
                    digest.update(chunk)
                    total += len(chunk)
                if total != metadata.st_size:
                    raise CandidateSafetyError("candidate changed while being verified")
            finally:
                os.close(file_descriptor)
        except (OSError, CandidateSafetyError) as exc:
            if isinstance(exc, CandidateSafetyError):
                raise
            raise CandidateSafetyError("candidate is unavailable or unsafe") from exc
        finally:
            os.close(descriptor)
        return VerifiedCandidate(str(relative), total, digest.hexdigest())
