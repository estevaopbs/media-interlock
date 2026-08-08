"""Durable, contained local filesystem primitives."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePath


class PathSafetyError(ValueError):
    """Raised before a path can escape a configured root."""


def path_under(root: Path, relative: str) -> Path:
    candidate = PurePath(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise PathSafetyError("path must be a non-empty relative path without traversal")
    resolved_root = root.resolve(strict=True)
    resolved_target = (resolved_root / candidate).resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PathSafetyError("path resolves outside its configured root") from exc
    return resolved_target


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def atomic_write_under(root: Path, relative: str, data: bytes, mode: int = 0o600) -> None:
    """Durably replace one path without following a root or parent symlink."""
    if not isinstance(data, bytes):
        raise TypeError("atomic_write_under data must be bytes")
    candidate = PurePath(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise PathSafetyError("path must be a non-empty relative path without traversal")
    directory = os.open(root, _DIRECTORY_FLAGS)
    temporary_name: str | None = None
    descriptor = -1
    try:
        for part in candidate.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
                os.fsync(directory)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        temporary_name = f".{candidate.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("atomic write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, candidate.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary_name = None
        os.fsync(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(directory)
