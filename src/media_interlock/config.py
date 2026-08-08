"""Strict, non-secret TOML configuration for MediaInterlock."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised before side effects when configuration is invalid."""


@dataclass(frozen=True)
class SecretReference:
    source: str
    reference: str

    @classmethod
    def parse(cls, value: object, location: str) -> "SecretReference":
        if not isinstance(value, str):
            raise ConfigError(f"{location} must use env: or file: secret reference")
        if value.startswith("env:"):
            name = value.removeprefix("env:")
            if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
                raise ConfigError(f"{location} has an invalid environment reference")
            return cls("env", name)
        if value.startswith("file:"):
            location_path = Path(value.removeprefix("file:"))
            if not location_path.is_absolute() or ".." in location_path.parts:
                raise ConfigError(f"{location} has an unsafe file secret reference")
            return cls("file", str(location_path))
        raise ConfigError(f"{location} must use env: or file: secret reference")

    def resolve(self) -> str:
        if self.source == "env":
            try:
                return os.environ[self.reference]
            except KeyError as exc:
                raise ConfigError(f"secret environment reference is unavailable: {self.reference}") from exc
        try:
            descriptor = os.open(self.reference, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ConfigError("secret file reference is unavailable") from exc
        try:
            value = os.read(descriptor, 1024 * 1024).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError("secret file reference is unreadable") from exc
        finally:
            os.close(descriptor)
        return value.rstrip("\r\n")

    def redacted(self) -> str:
        return f"{self.source}:<redacted>"


@dataclass(frozen=True)
class SharedConfig:
    runtime_dir: Path


@dataclass(frozen=True)
class ComponentConfig:
    name: str
    state_dir: Path
    socket_path: Path


@dataclass(frozen=True)
class FenceConfig(ComponentConfig):
    staging_root: Path
    capacity_bytes: int
    max_inflight: int


@dataclass(frozen=True)
class PublisherConfig(ComponentConfig):
    staging_root: Path
    canonical_root: Path


@dataclass(frozen=True)
class ReconcilerConfig(ComponentConfig):
    pass


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    base_url: str
    secrets: Mapping[str, SecretReference]


@dataclass(frozen=True)
class ProductConfig:
    shared: SharedConfig
    fence: FenceConfig | None
    publisher: PublisherConfig | None
    reconciler: ReconcilerConfig | None
    adapters: Mapping[str, AdapterConfig]

    def redacted(self) -> dict[str, object]:
        adapters: dict[str, object] = {}
        for name, adapter in self.adapters.items():
            adapters[name] = {
                "base_url": adapter.base_url,
                **{key: reference.redacted() for key, reference in adapter.secrets.items()},
            }
        return {"shared": {"runtime_dir": str(self.shared.runtime_dir)}, "adapters": adapters}


_TOP_LEVEL = {"shared", "fence", "publisher", "reconciler", "adapters"}
_ADAPTERS = {"jellyfin", "radarr", "sonarr", "qbittorrent", "bazarr", "seerr", "prowlarr"}


def _table(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a TOML table")
    return value


def _require_keys(table: Mapping[str, object], allowed: set[str], location: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"unknown key in {location}: {sorted(unknown)[0]}")


def _absolute_path(value: object, location: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{location} must be a non-empty absolute path")
    path = PurePath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{location} must be a safe absolute path")
    return Path(path)


def _required_path(table: Mapping[str, object], name: str, location: str) -> Path:
    try:
        return _absolute_path(table[name], f"{location}.{name}")
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc


def _required_positive(table: Mapping[str, object], name: str, location: str, maximum: int) -> int:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ConfigError(f"{location}.{name} must be a bounded positive integer")
    return value


def _component(table: Mapping[str, object], name: str, runtime_dir: Path) -> ComponentConfig:
    socket_path = _required_path(table, "socket_path", name)
    if socket_path.parent != runtime_dir:
        raise ConfigError(f"{name}.socket_path must be directly under shared.runtime_dir")
    return ComponentConfig(name, _required_path(table, "state_dir", name), socket_path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _validate_disjoint(first: Path, second: Path, first_name: str, second_name: str) -> None:
    if _is_within(first, second) or _is_within(second, first):
        raise ConfigError(f"{first_name} and {second_name} must be disjoint")


def _adapter(name: str, table: Mapping[str, object]) -> AdapterConfig:
    _require_keys(table, {"base_url", "api_key"}, f"adapters.{name}")
    try:
        base_url = table["base_url"]
        api_key = table["api_key"]
    except KeyError as exc:
        raise ConfigError(f"missing required key: adapters.{name}.{exc.args[0]}") from exc
    if not isinstance(base_url, str):
        raise ConfigError(f"adapters.{name}.base_url must be a URL")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigError(f"adapters.{name}.base_url must be an absolute HTTP(S) URL without query")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"adapters.{name}.base_url must not contain credentials")
    try:
        if not parsed.hostname or parsed.port is None and parsed.netloc.endswith(":"):
            raise ConfigError(f"adapters.{name}.base_url must be a valid HTTP(S) authority")
        parsed.port
    except ValueError as exc:
        raise ConfigError(f"adapters.{name}.base_url must be a valid HTTP(S) authority") from exc
    return AdapterConfig(name, base_url.rstrip("/"), {"api_key": SecretReference.parse(api_key, f"adapters.{name}.api_key")})


def load_config(path: Path) -> ProductConfig:
    """Load a strict shared configuration without resolving secret values."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("configuration cannot be read as TOML") from exc
    _require_keys(document, _TOP_LEVEL, "root")
    shared_table = _table(document.get("shared"), "shared")
    _require_keys(shared_table, {"runtime_dir"}, "shared")
    shared = SharedConfig(_required_path(shared_table, "runtime_dir", "shared"))

    components: dict[str, ComponentConfig | None] = {name: None for name in ("fence", "publisher", "reconciler")}
    for name in components:
        if name in document:
            components[name] = _component(_table(document[name], name), name, shared.runtime_dir)
    fence: FenceConfig | None = None
    if components["fence"] is not None:
        table = _table(document["fence"], "fence")
        _require_keys(table, {"state_dir", "socket_path", "staging_root", "capacity_bytes", "max_inflight"}, "fence")
        base = components["fence"]
        assert base is not None
        fence = FenceConfig(base.name, base.state_dir, base.socket_path, _required_path(table, "staging_root", "fence"), _required_positive(table, "capacity_bytes", "fence", 2**63 - 1), _required_positive(table, "max_inflight", "fence", 100_000))
    publisher: PublisherConfig | None = None
    if components["publisher"] is not None:
        table = _table(document["publisher"], "publisher")
        _require_keys(table, {"state_dir", "socket_path", "staging_root", "canonical_root"}, "publisher")
        base = components["publisher"]
        assert base is not None
        publisher = PublisherConfig(base.name, base.state_dir, base.socket_path, _required_path(table, "staging_root", "publisher"), _required_path(table, "canonical_root", "publisher"))
        _validate_disjoint(publisher.staging_root, publisher.canonical_root, "publisher.staging_root", "publisher.canonical_root")
    reconciler = components["reconciler"]
    if reconciler is not None:
        _require_keys(_table(document["reconciler"], "reconciler"), {"state_dir", "socket_path"}, "reconciler")
        reconciler = ReconcilerConfig(reconciler.name, reconciler.state_dir, reconciler.socket_path)

    configured = [component for component in (fence, publisher, reconciler) if component is not None]
    if len({component.state_dir for component in configured}) != len(configured):
        raise ConfigError("component state_dir values must be unique")
    if len({component.socket_path for component in configured}) != len(configured):
        raise ConfigError("component socket_path values must be unique")
    writable_roots: list[tuple[str, Path]] = []
    if fence is not None:
        writable_roots.append(("fence.staging_root", fence.staging_root))
    if publisher is not None:
        writable_roots.extend((("publisher.staging_root", publisher.staging_root), ("publisher.canonical_root", publisher.canonical_root)))
        if fence is not None:
            _validate_disjoint(fence.staging_root, publisher.canonical_root, "fence.staging_root", "publisher.canonical_root")
    for component in configured:
        for root_name, root in writable_roots:
            _validate_disjoint(component.state_dir, root, f"{component.name}.state_dir", root_name)
    for root_name, root in writable_roots:
        _validate_disjoint(shared.runtime_dir, root, "shared.runtime_dir", root_name)
    adapters_table = _table(document.get("adapters", {}), "adapters")
    _require_keys(adapters_table, _ADAPTERS, "adapters")
    adapters = {name: _adapter(name, _table(table, f"adapters.{name}")) for name, table in adapters_table.items()}
    return ProductConfig(shared, fence, publisher, reconciler, adapters)
