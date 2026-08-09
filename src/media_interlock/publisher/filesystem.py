"""Contained, no-follow candidate inspection for Publisher."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol


class CandidateSafetyError(ValueError):
    """Raised when a candidate cannot be safely derived under staging."""


@dataclass(frozen=True)
class VerifiedCandidate:
    relative_path: str
    bytes_verified: int
    sha256: str


@dataclass(frozen=True)
class BundleMember:
    relative_path: str
    bytes_verified: int
    allocated_bytes: int
    device: int
    inode: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True)
class MediaInspection:
    """Bounded immutable evidence from the selected media payload."""

    audio_languages: tuple[str, ...]
    subtitle_languages: tuple[str, ...]
    container_evidence: tuple[str, ...]


class MediaInspector(Protocol):
    def inspect(self, payload: BundleMember) -> MediaInspection: ...


class ExtensionMediaInspector:
    """Portable bounded baseline; richer decoders can implement MediaInspector."""

    def inspect(self, payload: BundleMember) -> MediaInspection:
        suffix = PurePath(payload.relative_path).suffix.lower()
        if suffix not in BundleVerifier._VIDEO_EXTENSIONS:
            raise CandidateSafetyError("media inspection cannot identify a video container")
        return MediaInspection((), (), (f"container:{suffix[1:]}",))


@dataclass(frozen=True)
class VerifiedBundle:
    payload: VerifiedCandidate
    members: tuple[BundleMember, ...]
    bytes_verified: int
    inspection: MediaInspection = MediaInspection((), (), ())


class CandidateVerifier:
    def __init__(self, staging_root: Path) -> None:
        self._root = staging_root

    def verify(self, relative_path: str, *, allow_hardlinks: bool = False) -> VerifiedCandidate:
        member = self.inspect(relative_path, allow_hardlinks=allow_hardlinks)
        return VerifiedCandidate(member.relative_path, member.bytes_verified, member.sha256)

    def inspect(self, relative_path: str, *, allow_hardlinks: bool = False) -> BundleMember:
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
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1 or (not allow_hardlinks and metadata.st_nlink != 1) or metadata.st_size <= 0:
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
        allocated = metadata.st_blocks * 512
        if not all(isinstance(value, int) and value >= 0 for value in (allocated, metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns)):
            raise CandidateSafetyError("candidate metadata is invalid")
        return BundleMember(str(relative), total, allocated, metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, digest.hexdigest())


class BundleVerifier:
    """Seal one selected video and its matching, contained subtitle sidecars."""

    _VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mp4", ".webm"}
    _SIDECAR_EXTENSIONS = frozenset({".ass", ".ssa", ".srt", ".vtt"})

    def __init__(self, staging_root: Path, *, settle_seconds: float, sidecar_extensions: frozenset[str] | tuple[str, ...] = _SIDECAR_EXTENSIONS, required_languages: frozenset[str] | tuple[str, ...] = (), language_aliases: dict[str, str] | None = None, required_container_evidence: frozenset[str] | tuple[str, ...] = (), media_inspector: MediaInspector | None = None) -> None:
        if not isinstance(staging_root, Path) or not staging_root.is_absolute() or isinstance(settle_seconds, bool) or not isinstance(settle_seconds, (int, float)) or settle_seconds < 0 or settle_seconds > 60:
            raise CandidateSafetyError("bundle verification policy is invalid")
        extensions = frozenset(sidecar_extensions)
        required = frozenset(language.lower().replace("_", "-") for language in required_languages)
        aliases = {} if language_aliases is None else {alias.lower().replace("_", "-"): language.lower().replace("_", "-") for alias, language in language_aliases.items()}
        containers = frozenset(required_container_evidence)
        if not extensions or any(not isinstance(extension, str) or extension not in self._SIDECAR_EXTENSIONS for extension in extensions) or any(not isinstance(language, str) or not language for language in required) or any(not isinstance(alias, str) or not alias or not isinstance(language, str) or not language for alias, language in aliases.items()) or any(not isinstance(value, str) or not value.startswith("container:") for value in containers):
            raise CandidateSafetyError("bundle verification policy is invalid")
        self._root = staging_root
        self._settle_seconds = float(settle_seconds)
        self._sidecar_extensions = extensions
        self._required_languages = required
        self._language_aliases = aliases
        self._required_container_evidence = containers
        self._candidate = CandidateVerifier(staging_root)
        self._media_inspector = ExtensionMediaInspector() if media_inspector is None else media_inspector

    def verify(self, relative_payload: str, *, allow_hardlinks: bool = False) -> VerifiedBundle:
        first_members, first_inspection = self._observe(relative_payload, allow_hardlinks=allow_hardlinks)
        if self._settle_seconds:
            time.sleep(self._settle_seconds)
        second_members, second_inspection = self._observe(relative_payload, allow_hardlinks=allow_hardlinks)
        if first_members != second_members or first_inspection != second_inspection:
            raise CandidateSafetyError("candidate bundle changed during the settle interval")
        payload = next(member for member in second_members if member.relative_path == relative_payload)
        return VerifiedBundle(
            VerifiedCandidate(payload.relative_path, payload.bytes_verified, payload.sha256),
            second_members,
            sum(member.bytes_verified for member in second_members),
            second_inspection,
        )

    def requires_freeze(self, relative_payload: str) -> bool:
        """Conservatively identify a bundle that cannot be copied unfrozen."""
        members, _ = self._observe(relative_payload, allow_hardlinks=True)
        for member in members:
            try:
                metadata = os.stat(self._root / member.relative_path, follow_symlinks=False)
            except OSError as exc:
                raise CandidateSafetyError("bundle member is unavailable") from exc
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("bundle member is unsafe")
            if metadata.st_nlink > 1:
                return True
        return False

    def _observe(self, relative_payload: str, *, allow_hardlinks: bool) -> tuple[tuple[BundleMember, ...], MediaInspection]:
        relative = PurePath(relative_payload)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.suffix.lower() not in self._VIDEO_EXTENSIONS:
            raise CandidateSafetyError("bundle payload path is invalid")
        directory = self._open_parent(relative)
        try:
            entries = os.listdir(directory)
        except OSError as exc:
            os.close(directory)
            raise CandidateSafetyError("bundle directory is unavailable") from exc
        try:
            stem = relative.stem
            selected: list[str] = []
            sidecar_languages: set[str] = set()
            for name in entries:
                if not isinstance(name, str) or name in {".", ".."}:
                    raise CandidateSafetyError("bundle directory entry is invalid")
                if name != relative.name and not name.startswith(stem + "."):
                    continue
                try:
                    metadata = os.lstat(name, dir_fd=directory)
                except OSError as exc:
                    raise CandidateSafetyError("bundle directory changed during enumeration") from exc
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise CandidateSafetyError("bundle member is unsafe")
                suffix = PurePath(name).suffix.lower()
                if name == relative.name:
                    if suffix not in self._VIDEO_EXTENSIONS:
                        raise CandidateSafetyError("bundle payload is not supported")
                elif suffix not in self._sidecar_extensions:
                    raise CandidateSafetyError("bundle has an unsupported matching member")
                else:
                    language = name.removeprefix(stem + ".").removesuffix(suffix).split(".", 1)[0].lower().replace("_", "-")
                    if not language:
                        raise CandidateSafetyError("bundle sidecar has no language evidence")
                    sidecar_languages.add(self._language_aliases.get(language, language))
                selected.append(name)
            if relative.name not in selected or len(selected) > 129:
                raise CandidateSafetyError("bundle selection is invalid")
            prefix = "" if str(relative.parent) == "." else str(relative.parent) + "/"
            members = tuple(sorted((self._candidate.inspect(prefix + name, allow_hardlinks=allow_hardlinks) for name in selected), key=lambda member: member.relative_path))
            payload = next(member for member in members if member.relative_path == relative_payload)
            try:
                inspection = self._media_inspector.inspect(payload)
            except Exception as exc:
                raise CandidateSafetyError("media inspection failed") from exc
            if not isinstance(inspection, MediaInspection) or not _inspection_valid(inspection):
                raise CandidateSafetyError("media inspection evidence is invalid")
            if not self._required_container_evidence.issubset(inspection.container_evidence):
                raise CandidateSafetyError("media inspection does not meet required container evidence")
            observed_languages = sidecar_languages | set(inspection.audio_languages) | set(inspection.subtitle_languages)
            if not self._required_languages.issubset(observed_languages):
                raise CandidateSafetyError("bundle is missing required language evidence")
        finally:
            os.close(directory)
        return members, inspection

    def _open_parent(self, relative: PurePath) -> int:
        try:
            descriptor = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except OSError as exc:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            raise CandidateSafetyError("bundle directory is unavailable or unsafe") from exc


def _inspection_valid(inspection: MediaInspection) -> bool:
    groups = (inspection.audio_languages, inspection.subtitle_languages, inspection.container_evidence)
    return all(
        isinstance(group, tuple)
        and len(group) <= 64
        and all(isinstance(value, str) and value and len(value) <= 64 for value in group)
        for group in groups
    ) and bool(inspection.container_evidence)
