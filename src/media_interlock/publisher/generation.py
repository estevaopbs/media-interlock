"""Atomic canonical generation promotion with retained prior generations."""

from __future__ import annotations

import os
import fcntl
import shutil
import stat
import uuid
import hashlib
import re
import ctypes
import errno
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path, PurePath

from .filesystem import BundleMember, CandidateSafetyError, CandidateVerifier, VerifiedBundle, VerifiedCandidate


class GenerationPublisher:
    """Promotes a verified staging file into an immutable canonical generation."""

    def __init__(self, staging_root: Path, canonical_root: Path) -> None:
        self._staging_root = staging_root
        self._canonical_root = canonical_root

    def prepare(self, generation_id: str, candidate: VerifiedCandidate) -> Path:
        if not self._valid_generation_id(generation_id):
            raise CandidateSafetyError("generation identity is unsafe")
        self._canonical_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._canonical_root.is_symlink():
            raise CandidateSafetyError("canonical root is unsafe")
        generations = self._canonical_root / "generations"
        try:
            metadata = generations.lstat()
        except FileNotFoundError:
            generations.mkdir(mode=0o755)
        else:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("canonical generations directory is unsafe")
        destination_dir = generations / generation_id
        if destination_dir.exists():
            destination = destination_dir / candidate.relative_path
            if destination.is_file() and CandidateVerifier(destination_dir).verify(candidate.relative_path) == candidate:
                return destination
            raise CandidateSafetyError("generation identity already has a different payload")
        verified = CandidateVerifier(self._staging_root).verify(candidate.relative_path)
        if verified != candidate:
            raise CandidateSafetyError("candidate changed before promotion")
        temporary = generations / f".{generation_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.mkdir(mode=0o755)
            destination = temporary / candidate.relative_path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._copy_verified(candidate.relative_path, destination, candidate)
            self._fsync_tree(destination, temporary)
            os.replace(temporary, destination_dir)
            self._fsync_directory(generations)
            return destination_dir / candidate.relative_path
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def prepare_bundle(self, generation_id: str, bundle: VerifiedBundle, *, allow_hardlinks: bool = False) -> Path:
        """Copy every pre-sealed member before exposing a complete generation."""
        if not self._valid_generation_id(generation_id) or not isinstance(bundle, VerifiedBundle) or not bundle.members:
            raise CandidateSafetyError("bundle generation identity is unsafe")
        if bundle.payload.relative_path not in {member.relative_path for member in bundle.members}:
            raise CandidateSafetyError("bundle payload is absent")
        self._canonical_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._canonical_root.is_symlink():
            raise CandidateSafetyError("canonical root is unsafe")
        generations = self._canonical_root / "generations"
        try:
            metadata = generations.lstat()
        except FileNotFoundError:
            generations.mkdir(mode=0o755)
        else:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("canonical generations directory is unsafe")
        destination_dir = generations / generation_id
        if destination_dir.exists():
            if self._copied_bundle_matches(destination_dir, bundle):
                return destination_dir / bundle.payload.relative_path
            raise CandidateSafetyError("generation identity already has a different bundle")
        if not self._source_bundle_matches(bundle, allow_hardlinks=allow_hardlinks):
            raise CandidateSafetyError("candidate bundle changed before promotion")
        temporary = generations / f".{generation_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.mkdir(mode=0o755)
            for member in bundle.members:
                destination = temporary / member.relative_path
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._copy_verified(member.relative_path, destination, VerifiedCandidate(member.relative_path, member.bytes_verified, member.sha256), allow_hardlinks=allow_hardlinks)
            if not self._source_bundle_matches(bundle, allow_hardlinks=allow_hardlinks):
                raise CandidateSafetyError("candidate bundle changed during copy")
            self._fsync_bundle(temporary, bundle)
            os.replace(temporary, destination_dir)
            self._fsync_directory(generations)
            return destination_dir / bundle.payload.relative_path
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _source_bundle_matches(self, bundle: VerifiedBundle, *, allow_hardlinks: bool) -> bool:
        verifier = CandidateVerifier(self._staging_root)
        try:
            observed = tuple(verifier.inspect(member.relative_path, allow_hardlinks=allow_hardlinks) for member in bundle.members)
        except CandidateSafetyError:
            return False
        return observed == bundle.members

    @staticmethod
    def _copied_bundle_matches(destination_dir: Path, bundle: VerifiedBundle) -> bool:
        verifier = CandidateVerifier(destination_dir)
        try:
            observed = tuple(verifier.verify(member.relative_path) for member in bundle.members)
        except CandidateSafetyError:
            return False
        return all(
            candidate.relative_path == member.relative_path
            and candidate.bytes_verified == member.bytes_verified
            and candidate.sha256 == member.sha256
            for candidate, member in zip(observed, bundle.members, strict=True)
        )

    @staticmethod
    def _valid_generation_id(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            return False
        # Fence operations use UUIDv4, while bounded Arr-history bootstrap
        # operations use a deterministic UUIDv5 derived from the history ID.
        return str(parsed) == value and parsed.version in {4, 5}

    @classmethod
    def _temporary_generation_name(cls, value: str) -> bool:
        match = re.fullmatch(r"\.([0-9a-f-]{36})\.([0-9a-f]{32})\.tmp", value)
        return match is not None and cls._valid_generation_id(match.group(1))

    def _copy_verified(self, relative_path: str, destination: Path, candidate: VerifiedCandidate, *, allow_hardlinks: bool = False) -> None:
        relative = PurePath(relative_path)
        descriptor = os.open(self._staging_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            source = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            try:
                metadata = os.fstat(source)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1 or (not allow_hardlinks and metadata.st_nlink != 1) or metadata.st_size != candidate.bytes_verified:
                    raise CandidateSafetyError("candidate changed before copy")
                target = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    digest = hashlib.sha256()
                    while chunk := os.read(source, 1024 * 1024):
                        digest.update(chunk)
                        offset = 0
                        while offset < len(chunk):
                            written = os.write(target, chunk[offset:])
                            if written <= 0:
                                raise OSError("generation copy made no progress")
                            offset += written
                    if digest.hexdigest() != candidate.sha256:
                        raise CandidateSafetyError("candidate changed during copy")
                    os.fsync(target)
                finally:
                    os.close(target)
            finally:
                os.close(source)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_tree(self, destination: Path, temporary: Path) -> None:
        self._fsync_directory(destination.parent)
        self._fsync_directory(temporary)

    def _fsync_bundle(self, temporary: Path, bundle: VerifiedBundle) -> None:
        directories = {temporary}
        for member in bundle.members:
            directories.add((temporary / member.relative_path).parent)
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            self._fsync_directory(directory)


class CanonicalWriterLock:
    """Exclusive daemon-lifetime claim for one canonical root."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    @classmethod
    def acquire(cls, canonical_root: Path) -> "CanonicalWriterLock":
        descriptor = os.open(canonical_root / ".publisher.writer.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CandidateSafetyError("canonical root already has a Publisher writer") from exc
        return cls(descriptor)

    def close(self) -> None:
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)


class AssetGenerationPublisher:
    """Seal one asset bundle and atomically advance only that asset's slot."""

    def __init__(self, staging_root: Path, canonical_root: Path, *, namespace: str) -> None:
        if not namespace or "/" in namespace or namespace in {".", ".."}:
            raise CandidateSafetyError("canonical namespace is unsafe")
        self._staging_root = staging_root
        self._canonical_root = canonical_root
        self._namespace = namespace

    def publish(self, asset_slot: str, generation_id: str, candidate: VerifiedCandidate | VerifiedBundle, *, previous_generation_id: str | None = None, hardlink_frozen: bool = False, item_type: str | None = None, provider_ids: Mapping[str, str] | None = None) -> Path:
        slot = self._slot_name(asset_slot)
        if previous_generation_id is not None and not GenerationPublisher._valid_generation_id(previous_generation_id):
            raise CandidateSafetyError("prior generation identity is unsafe")
        visible_before = self.visible_generation(asset_slot)
        if visible_before == generation_id:
            payload = self.generation_payload(asset_slot, generation_id)
            self._ensure_catalog_nfo(payload, item_type, provider_ids)
            self._seal_bundle_modes(payload.parent)
            os.chmod(payload.parent, 0o755)
            GenerationPublisher._fsync_directory(payload.parent)
            self._ensure_private_pointer(asset_slot, generation_id)
            route = self._expose_relative_routes(asset_slot, generation_id, candidate, payload)
            self._remove_legacy_slot(asset_slot, generation_id)
            return route
        if visible_before != previous_generation_id:
            raise CandidateSafetyError("asset slot changed after durable generation intent")
        private_root = self._canonical_root / ".publisher" / "assets" / slot
        visible_root = self._canonical_root / self._namespace
        self._safe_directory(self._canonical_root, mode=0o700)
        self._safe_directory(private_root, mode=0o755)
        self._safe_directory(visible_root, mode=0o755)
        try:
            payload = self.generation_payload(asset_slot, generation_id)
        except CandidateSafetyError:
            generation = GenerationPublisher(self._staging_root, private_root)
            if isinstance(candidate, VerifiedBundle):
                prepared = generation.prepare_bundle(generation_id, candidate, allow_hardlinks=hardlink_frozen)
                payload = self._normalize_bundle(private_root / "generations" / generation_id, prepared, candidate)
            else:
                prepared = generation.prepare(generation_id, candidate)
                payload = private_root / "generations" / generation_id / self._payload_name(candidate.relative_path)
                os.replace(prepared, payload)
        self._ensure_catalog_nfo(payload, item_type, provider_ids)
        # The move can be durable while the following mode repair is not. Make
        # recovery idempotently restore the read-only playback contract.
        self._seal_bundle_modes(payload.parent)
        os.chmod(payload.parent, 0o755)
        GenerationPublisher._fsync_directory(payload.parent)
        expected_payload = candidate.payload if isinstance(candidate, VerifiedBundle) else candidate
        if CandidateVerifier(payload.parent).verify(payload.name, allow_hardlinks=True) != VerifiedCandidate(payload.name, expected_payload.bytes_verified, expected_payload.sha256):
            raise CandidateSafetyError("asset generation payload differs")
        pointer_root = self._canonical_root / ".publisher" / "visible" / self._namespace
        self._safe_directory(pointer_root, mode=0o755)
        slot_path = pointer_root / slot
        target = Path("..") / ".." / "assets" / slot / "generations" / generation_id
        temporary = pointer_root / f".{slot}.{uuid.uuid4().hex}.pending"
        try:
            os.symlink(target, temporary)
            try:
                metadata = slot_path.lstat()
            except FileNotFoundError:
                if previous_generation_id is not None:
                    raise CandidateSafetyError("asset slot changed after durable generation intent")
                self._rename_no_replace(temporary, slot_path)
            else:
                if not stat.S_ISLNK(metadata.st_mode):
                    raise CandidateSafetyError("asset slot is unsafe")
                if self.visible_generation(asset_slot) != previous_generation_id:
                    raise CandidateSafetyError("asset slot changed after durable generation intent")
                self._rename_exchange(slot_path, temporary)
                # The exchanged pending name contains the prior logical slot;
                # immutable generation directories retain the predecessor.
                temporary.unlink()
            GenerationPublisher._fsync_directory(pointer_root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        route = self._expose_relative_routes(asset_slot, generation_id, candidate, payload)
        self._remove_legacy_slot(asset_slot, generation_id)
        return route

    def ensure_catalog_identity(
        self,
        asset_slot: str,
        generation_id: str,
        item_type: str,
        provider_ids: Mapping[str, str],
        *,
        candidate_relative_path: str | None = None,
    ) -> Path:
        payload = self.generation_payload(asset_slot, generation_id)
        self._ensure_catalog_nfo(payload, item_type, provider_ids)
        self._seal_bundle_modes(payload.parent)
        os.chmod(payload.parent, 0o755)
        GenerationPublisher._fsync_directory(payload.parent)
        if candidate_relative_path is None:
            return payload
        candidate = VerifiedCandidate(candidate_relative_path, payload.stat().st_size, CandidateVerifier(payload.parent).verify(payload.name, allow_hardlinks=True).sha256)
        self._ensure_private_pointer(asset_slot, generation_id)
        route = self._expose_relative_routes(asset_slot, generation_id, candidate, payload)
        self._remove_legacy_slot(asset_slot, generation_id)
        return route

    @staticmethod
    def _ensure_catalog_nfo(
        payload: Path,
        item_type: str | None,
        provider_ids: Mapping[str, str] | None,
    ) -> None:
        if item_type is None and provider_ids is None:
            return
        if item_type not in {"Movie", "Episode"} or not provider_ids:
            raise CandidateSafetyError("catalog identity is incomplete")
        if not all(
            isinstance(key, str)
            and key
            and isinstance(value, str)
            and value
            for key, value in provider_ids.items()
        ):
            raise CandidateSafetyError("catalog provider identity is unsafe")
        root = ET.Element("movie" if item_type == "Movie" else "episodedetails")
        for index, (key, value) in enumerate(
            sorted(provider_ids.items(), key=lambda item: item[0].casefold())
        ):
            node = ET.SubElement(
                root,
                "uniqueid",
                {"type": key.casefold(), "default": "true" if index == 0 else "false"},
            )
            node.text = value
        content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        destination = payload.with_suffix(".nfo")
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    offset = 0
                    while offset < len(content):
                        written = os.write(descriptor, content[offset:])
                        if written <= 0:
                            raise OSError("catalog sidecar write made no progress")
                        offset += written
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.chmod(temporary, 0o444)
                os.replace(temporary, destination)
                GenerationPublisher._fsync_directory(destination.parent)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or destination.read_bytes() != content
        ):
            raise CandidateSafetyError("catalog sidecar conflicts")
        os.chmod(destination, 0o444)

    def _normalize_bundle(self, generation_dir: Path, prepared_payload: Path, bundle: VerifiedBundle) -> Path:
        payload_name = self._payload_name(bundle.payload.relative_path)
        payload = generation_dir / payload_name
        if prepared_payload != payload:
            os.replace(prepared_payload, payload)
        source_stem = PurePath(bundle.payload.relative_path).stem
        for member in bundle.members:
            if member.relative_path == bundle.payload.relative_path:
                continue
            name = PurePath(member.relative_path).name
            if not name.startswith(source_stem + "."):
                raise CandidateSafetyError("bundle sidecar name is invalid")
            destination = generation_dir / ("payload" + name.removeprefix(source_stem))
            source = generation_dir / member.relative_path
            if source != destination:
                if destination.exists():
                    raise CandidateSafetyError("bundle sidecar name conflicts")
                os.replace(source, destination)
        self._remove_empty_bundle_directories(generation_dir, bundle)
        self._fsync_bundle_generation(generation_dir)
        return payload

    @staticmethod
    def _remove_empty_bundle_directories(generation_dir: Path, bundle: VerifiedBundle) -> None:
        parents = {generation_dir / PurePath(member.relative_path).parent for member in bundle.members}
        for directory in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            if directory != generation_dir:
                try:
                    directory.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _fsync_bundle_generation(generation_dir: Path) -> None:
        GenerationPublisher._fsync_directory(generation_dir)

    @staticmethod
    def _seal_bundle_modes(bundle_dir: Path) -> None:
        for member in bundle_dir.glob("payload.*"):
            metadata = member.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("asset generation bundle is unsafe")
            os.chmod(member, 0o444)

    def _ensure_private_pointer(self, asset_slot: str, generation_id: str) -> None:
        slot = self._slot_name(asset_slot)
        pointer_root = self._canonical_root / ".publisher" / "visible" / self._namespace
        self._safe_directory(pointer_root, mode=0o755)
        pointer = pointer_root / slot
        expected = Path("..") / ".." / "assets" / slot / "generations" / generation_id
        try:
            metadata = pointer.lstat()
        except FileNotFoundError:
            temporary = pointer_root / f".{slot}.{uuid.uuid4().hex}.pending"
            try:
                os.symlink(expected, temporary)
                self._rename_no_replace(temporary, pointer)
                GenerationPublisher._fsync_directory(pointer_root)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return
        if not stat.S_ISLNK(metadata.st_mode) or os.readlink(pointer) != str(expected):
            raise CandidateSafetyError("asset slot is unsafe")

    def _expose_relative_routes(
        self,
        asset_slot: str,
        generation_id: str,
        candidate: VerifiedCandidate | VerifiedBundle,
        payload: Path,
    ) -> Path:
        relative_path = candidate.payload.relative_path if isinstance(candidate, VerifiedBundle) else candidate.relative_path
        relative = PurePath(relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise CandidateSafetyError("candidate path is unsafe")
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", relative.suffix.lower()):
            raise CandidateSafetyError("candidate has no safe media extension")
        visible_root = self._canonical_root / self._namespace
        self._safe_directory(visible_root, mode=0o755)
        public_payload: Path | None = None
        for member in sorted(payload.parent.glob("payload.*")):
            metadata = member.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("asset generation bundle is unsafe")
            suffix = member.name.removeprefix("payload")
            destination = visible_root / relative.parent / (relative.stem + suffix)
            self._safe_directory(destination.parent, mode=0o755)
            temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.pending"
            try:
                os.link(member, temporary, follow_symlinks=False)
                try:
                    current = destination.lstat()
                except FileNotFoundError:
                    self._rename_no_replace(temporary, destination)
                else:
                    allowed_root = self._canonical_root / ".publisher" / "assets" / self._slot_name(asset_slot) / "generations"
                    if stat.S_ISLNK(current.st_mode):
                        current_path = Path(os.path.normpath(destination.parent / os.readlink(destination)))
                        try:
                            routed = current_path.relative_to(allowed_root).parts
                        except ValueError:
                            routed = ()
                        owned = (
                            len(routed) == 2
                            and GenerationPublisher._valid_generation_id(routed[0])
                            and routed[1] == member.name
                        )
                    elif stat.S_ISREG(current.st_mode):
                        owned = self._route_is_owned(current, allowed_root, member.name)
                    else:
                        owned = False
                    if not owned:
                        raise CandidateSafetyError("canonical relative route conflicts")
                    if stat.S_ISREG(current.st_mode) and os.path.samestat(current, metadata):
                        temporary.unlink()
                    else:
                        self._rename_exchange(destination, temporary)
                        temporary.unlink()
                GenerationPublisher._fsync_directory(destination.parent)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            if member == payload:
                public_payload = destination
        if public_payload is None:
            raise CandidateSafetyError("asset generation payload is unavailable")
        return public_payload

    @staticmethod
    def _route_is_owned(route: os.stat_result, generations: Path, member_name: str) -> bool:
        try:
            candidates = tuple(generations.iterdir())
        except FileNotFoundError:
            return False
        for generation in candidates:
            try:
                generation_metadata = generation.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISDIR(generation_metadata.st_mode)
                or stat.S_ISLNK(generation_metadata.st_mode)
                or not GenerationPublisher._valid_generation_id(generation.name)
            ):
                raise CandidateSafetyError("asset generation directory is unsafe")
            try:
                candidate = (generation / member_name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(candidate.st_mode) and not stat.S_ISLNK(candidate.st_mode) and os.path.samestat(route, candidate):
                return True
        return False

    def _remove_legacy_slot(self, asset_slot: str, generation_id: str) -> None:
        slot = self._slot_name(asset_slot)
        legacy = self._canonical_root / self._namespace / slot
        try:
            metadata = legacy.lstat()
        except FileNotFoundError:
            return
        expected = f"../.publisher/assets/{slot}/generations/{generation_id}"
        if not stat.S_ISLNK(metadata.st_mode) or os.readlink(legacy) != expected:
            raise CandidateSafetyError("legacy asset slot is unsafe")
        legacy.unlink()
        GenerationPublisher._fsync_directory(legacy.parent)

    def visible_generation(self, asset_slot: str) -> str | None:
        slot = self._slot_name(asset_slot)
        private = self._canonical_root / ".publisher" / "visible" / self._namespace / slot
        legacy = self._canonical_root / self._namespace / slot
        try:
            metadata = private.lstat()
        except FileNotFoundError:
            path = legacy
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return None
        else:
            path = private
        if not stat.S_ISLNK(metadata.st_mode):
            raise CandidateSafetyError("asset slot is unsafe")
        target = os.readlink(path)
        expected = (
            f"../../assets/{slot}/generations/"
            if path == private
            else f"../.publisher/assets/{slot}/generations/"
        )
        if not target.startswith(expected):
            raise CandidateSafetyError("asset slot is unsafe")
        generation_id = target.removeprefix(expected)
        if not GenerationPublisher._valid_generation_id(generation_id):
            raise CandidateSafetyError("asset slot is unsafe")
        payload = self.generation_payload(asset_slot, generation_id)
        if not payload.is_file() or payload.is_symlink():
            raise CandidateSafetyError("asset slot target is unsafe")
        return generation_id

    def generation_payload(self, asset_slot: str, generation_id: str) -> Path:
        slot = self._slot_name(asset_slot)
        if not GenerationPublisher._valid_generation_id(generation_id):
            raise CandidateSafetyError("generation identity is unsafe")
        bundle = self._canonical_root / ".publisher" / "assets" / slot / "generations" / generation_id
        try:
            candidates = tuple(
                path
                for path in bundle.glob("payload.*")
                if path.name.count(".") == 1 and path.suffix != ".nfo"
            )
        except FileNotFoundError as exc:
            raise CandidateSafetyError("asset generation payload is unavailable") from exc
        if len(candidates) != 1:
            raise CandidateSafetyError("asset generation payload is unavailable")
        payload = candidates[0]
        metadata = payload.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CandidateSafetyError("asset generation payload is unsafe")
        return payload

    @staticmethod
    def _payload_name(relative_path: str) -> str:
        suffix = PurePath(relative_path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            raise CandidateSafetyError("candidate has no safe media extension")
        return "payload" + suffix

    def garbage_collect(self, asset_slot: str, retained_generation_ids: set[str]) -> None:
        """Reclaim only complete obsolete bundles for one known asset."""
        slot = self._slot_name(asset_slot)
        retained = set(retained_generation_ids)
        visible = self.visible_generation(asset_slot)
        if visible is not None:
            retained.add(visible)
        generations = self._canonical_root / ".publisher" / "assets" / slot / "generations"
        try:
            entries = tuple(generations.iterdir())
        except FileNotFoundError:
            return
        for generation in entries:
            metadata = generation.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or not GenerationPublisher._valid_generation_id(generation.name):
                raise CandidateSafetyError("asset generation directory is unsafe")
            if generation.name not in retained:
                shutil.rmtree(generation)
        GenerationPublisher._fsync_directory(generations)

    @staticmethod
    def _safe_directory(path: Path, *, mode: int) -> None:
        if not path.is_absolute():
            raise CandidateSafetyError("canonical directory is unsafe")
        directory = Path(path.anchor)
        for part in path.parts[1:]:
            directory = directory / part
            try:
                metadata = directory.lstat()
            except FileNotFoundError:
                try:
                    directory.mkdir(mode=mode)
                except FileExistsError:
                    pass
                metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CandidateSafetyError("canonical directory is unsafe")

    @staticmethod
    def _rename_no_replace(source: Path, destination: Path) -> None:
        AssetGenerationPublisher._renameat2(source, destination, 1)

    @staticmethod
    def _rename_exchange(left: Path, right: Path) -> None:
        AssetGenerationPublisher._renameat2(left, right, 2)

    @staticmethod
    def _renameat2(source: Path, destination: Path, flags: int) -> None:
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as exc:
            raise CandidateSafetyError("atomic renameat2 is unavailable") from exc
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), flags) != 0:
            error = ctypes.get_errno()
            if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise CandidateSafetyError("filesystem lacks required atomic rename capability")
            raise OSError(error, os.strerror(error), str(destination))

    @staticmethod
    def _slot_name(value: str) -> str:
        if not re.fullmatch(r"(?:radarr|sonarr):[A-Za-z0-9._-]{1,200}", value):
            raise CandidateSafetyError("asset slot is unsafe")
        return value.replace(":", "-")
