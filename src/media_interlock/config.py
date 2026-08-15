"""Strict, non-secret TOML configuration for MediaInterlock."""

from __future__ import annotations

import os
import tomllib
import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping
from urllib.parse import urlparse
import uuid


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
class RuntimeConfig:
    state_dir: Path


@dataclass(frozen=True)
class ComponentConfig:
    name: str


@dataclass(frozen=True)
class FenceConfig(ComponentConfig):
    capacity_bytes: int
    max_inflight: int
    mutation_lock: "MutationLockConfig"
    sources: Mapping[str, "FenceSourceProfile"]
    video_candidate_health: "VideoCandidateHealthConfig"


@dataclass(frozen=True)
class VideoCandidateHealthConfig:
    poll_interval_seconds: int
    metadata_timeout_seconds: int
    no_progress_timeout_seconds: int
    minimum_failure_observations: int
    replacement_initial_delay_seconds: int
    replacement_multiplier: float
    replacement_max_delay_seconds: int


@dataclass(frozen=True)
class MutationLockConfig:
    path: Path
    version: str
    timeout_ms: int


@dataclass(frozen=True)
class CapacityPool:
    name: str
    probe_path: Path
    minimum_free_bytes: int
    safety_margin_bytes: int


@dataclass(frozen=True)
class FenceSourceProfile:
    name: str
    category: str
    download_client_id: int
    qbittorrent_save_path: Path
    download_pool: str
    staging_pool: str
    canonical_pool: str


@dataclass(frozen=True)
class PublisherSourceProfile:
    name: str
    arr_import_path_prefix: str
    staging_root: Path
    canonical_root: Path
    namespace: str
    jellyfin_library_id: str
    jellyfin_path_prefix: str
    bundle_settle_seconds: int
    bundle_sidecar_extensions: tuple[str, ...]
    bundle_required_languages: tuple[str, ...]
    bundle_required_subtitle_languages: tuple[str, ...]
    bundle_language_aliases: Mapping[str, str]
    bundle_required_container_evidence: tuple[str, ...]


@dataclass(frozen=True)
class ImportReconciliationConfig:
    poll_interval_seconds: int
    initial_history_lookback_days: int
    max_imports_per_poll: int


@dataclass(frozen=True)
class ReconcilerSourceProfile:
    name: str
    kind: str
    category: str
    download_client_id: int


@dataclass(frozen=True)
class SourceProfile:
    name: str
    kind: str
    download_client_id: int
    category: str
    qbittorrent_save_path: Path
    arr_import_path_prefix: str
    staging_root: Path
    canonical_root: Path
    download_pool: str
    staging_pool: str
    canonical_pool: str
    namespace: str
    jellyfin_library_id: str
    jellyfin_path_prefix: str
    bundle_settle_seconds: int
    bundle_sidecar_extensions: tuple[str, ...]
    bundle_required_languages: tuple[str, ...]
    bundle_required_subtitle_languages: tuple[str, ...]
    bundle_language_aliases: Mapping[str, str]
    bundle_required_container_evidence: tuple[str, ...]


@dataclass(frozen=True)
class PublisherConfig(ComponentConfig):
    sources: Mapping[str, PublisherSourceProfile]
    import_reconciliation: ImportReconciliationConfig


@dataclass(frozen=True)
class ReconciliationPolicy:
    minimum_age_days: int
    terminal_horizon_days: int
    cooldown_seconds: int
    cooldown_step_days: int
    cooldown_multiplier: float
    maximum_cooldown_seconds: int
    final_search: bool
    max_attempts: int
    max_searches_per_run: int
    max_searches_per_hour: int
    max_searches_per_day: int
    max_grabs_per_run: int
    minimum_candidate_score: int
    minimum_score_gain: int
    required_candidate_formats: tuple[str, ...]
    forbidden_candidate_formats: tuple[str, ...]
    schedule_policy_revision: str = "legacy"
    release_timeout_seconds: int = 90
    max_release_response_bytes: int = 1_048_576
    transient_retry_seconds: int = 1_800
    transient_retry_multiplier: float = 2.0
    maximum_transient_retry_seconds: int = 21_600


@dataclass(frozen=True)
class ReconcilerConfig(ComponentConfig):
    poll_interval_seconds: int
    movie: ReconciliationPolicy
    episode: ReconciliationPolicy
    sources: Mapping[str, ReconcilerSourceProfile]


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    base_url: str
    secrets: Mapping[str, SecretReference]


@dataclass(frozen=True)
class ProductConfig:
    runtime: RuntimeConfig
    fence: FenceConfig | None
    publisher: PublisherConfig | None
    reconciler: ReconcilerConfig | None
    adapters: Mapping[str, AdapterConfig]
    sources: Mapping[str, SourceProfile]
    capacity_pools: Mapping[str, CapacityPool]

    def redacted(self) -> dict[str, object]:
        adapters: dict[str, object] = {}
        for name, adapter in self.adapters.items():
            adapters[name] = {
                "base_url": adapter.base_url,
                **{key: reference.redacted() for key, reference in adapter.secrets.items()},
            }
        return {"media_interlock": {"state_dir": str(self.runtime.state_dir)}, "adapters": adapters}


_TOP_LEVEL = {"media_interlock", "fence", "publisher", "reconciler", "adapters", "sources", "capacity_pools"}
_ADAPTERS = {"jellyfin", "radarr", "sonarr", "qbittorrent", "bazarr", "seerr", "prowlarr"}
_SOURCE_KINDS = {"radarr": "movie", "sonarr": "episode"}
_MUTATION_LOCK_VERSION = "shared-qbittorrent-mutation/v1"


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


def _required_nonnegative(table: Mapping[str, object], name: str, location: str, maximum: int) -> int:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ConfigError(f"{location}.{name} must be a bounded non-negative integer")
    return value


def _optional_nonnegative(table: Mapping[str, object], name: str, location: str, maximum: int, *, default: int) -> int:
    if name not in table:
        return default
    return _required_nonnegative(table, name, location, maximum)


def _optional_integer(table: Mapping[str, object], name: str, location: str, minimum: int, maximum: int, *, default: int) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{location}.{name} must be a bounded integer")
    return value


def _optional_positive(table: Mapping[str, object], name: str, location: str, maximum: int, *, default: int) -> int:
    if name not in table:
        return default
    return _required_positive(table, name, location, maximum)


def _optional_bool(table: Mapping[str, object], name: str, location: str, *, default: bool) -> bool:
    value = table.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{location}.{name} must be boolean")
    return value


def _optional_multiplier(table: Mapping[str, object], name: str, location: str, *, default: float) -> float:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1.0 <= float(value) <= 100.0:
        raise ConfigError(f"{location}.{name} must be a bounded multiplier")
    return float(value)


def _required_multiplier(table: Mapping[str, object], name: str, location: str) -> float:
    if name not in table:
        raise ConfigError(f"missing required key: {location}.{name}")
    return _optional_multiplier(table, name, location, default=1.0)


def _required_revision(table: Mapping[str, object], name: str, location: str) -> str:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is None:
        raise ConfigError(f"{location}.{name} must be a safe non-empty revision")
    return value


def _optional_format_names(table: Mapping[str, object], name: str, location: str) -> tuple[str, ...]:
    raw = table.get(name, [])
    if not isinstance(raw, list) or len(raw) > 64:
        raise ConfigError(f"{location}.{name} must be a bounded list")
    if any(not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value for value in raw):
        raise ConfigError(f"{location}.{name} has an invalid value")
    if len(set(raw)) != len(raw):
        raise ConfigError(f"{location}.{name} must not repeat values")
    return tuple(raw)


def _optional_bundle_strings(table: Mapping[str, object], name: str, location: str, *, default: tuple[str, ...], extension: bool = False) -> tuple[str, ...]:
    if name not in table:
        return default
    raw = table[name]
    if not isinstance(raw, list) or not raw or len(raw) > 32:
        raise ConfigError(f"{location}.{name} must be a bounded non-empty list")
    normalized: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value or len(value) > 32:
            raise ConfigError(f"{location}.{name} has an invalid value")
        item = value.lower().replace("_", "-")
        if extension:
            if not re.fullmatch(r"\.[a-z0-9]{1,16}", item):
                raise ConfigError(f"{location}.{name} has an invalid extension")
        elif not re.fullmatch(r"[a-z0-9]{2,8}(?:-[a-z0-9]{2,8})?", item):
            raise ConfigError(f"{location}.{name} has an invalid language")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ConfigError(f"{location}.{name} must not repeat values")
    return tuple(normalized)


def _optional_bundle_aliases(table: Mapping[str, object], location: str) -> Mapping[str, str]:
    if "bundle_language_aliases" not in table:
        return {}
    raw = table["bundle_language_aliases"]
    if not isinstance(raw, dict) or len(raw) > 32:
        raise ConfigError(f"{location}.bundle_language_aliases must be a bounded table")
    aliases: dict[str, str] = {}
    for alias, canonical in raw.items():
        parsed = _optional_bundle_strings({"value": [alias]}, "value", location, default=())
        targets = _optional_bundle_strings({"value": [canonical]}, "value", location, default=())
        aliases[parsed[0]] = targets[0]
    if len(set(aliases)) != len(aliases) or any(alias == canonical for alias, canonical in aliases.items()):
        raise ConfigError(f"{location}.bundle_language_aliases is invalid")
    return aliases


def _optional_container_evidence(table: Mapping[str, object], location: str) -> tuple[str, ...]:
    if "bundle_required_container_evidence" not in table:
        return ()
    raw = table["bundle_required_container_evidence"]
    if not isinstance(raw, list) or len(raw) > 16 or any(not isinstance(value, str) or not re.fullmatch(r"container:(avi|m4v|mkv|mp4|webm)", value) for value in raw):
        raise ConfigError(f"{location}.bundle_required_container_evidence is invalid")
    if len(set(raw)) != len(raw):
        raise ConfigError(f"{location}.bundle_required_container_evidence must not repeat values")
    return tuple(raw)


def _required_category(table: Mapping[str, object], name: str, location: str) -> str:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if not isinstance(value, str) or not value or len(value) > 128 or any(char in value for char in "\\x00/\\\\"):
        raise ConfigError(f"{location}.{name} must be a safe qBittorrent category")
    return value


def _required_uuid(table: Mapping[str, object], name: str, location: str) -> str:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if not isinstance(value, str):
        raise ConfigError(f"{location}.{name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ConfigError(f"{location}.{name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ConfigError(f"{location}.{name} must be a canonical UUID")
    return value


def _required_namespace(table: Mapping[str, object], name: str, location: str) -> str:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if not isinstance(value, str) or not value or len(value) > 200 or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ConfigError(f"{location}.{name} must be a safe logical namespace")
    return value


def _required_pool_name(table: Mapping[str, object], name: str, location: str) -> str:
    value = _required_namespace(table, name, location)
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"{location}.{name} must be a safe capacity pool name")
    return value


def _required_posix_prefix(table: Mapping[str, object], name: str, location: str) -> str:
    try:
        value = table[name]
    except KeyError as exc:
        raise ConfigError(f"missing required key: {location}.{name}") from exc
    if not isinstance(value, str) or not value.startswith("/"):
        raise ConfigError(f"{location}.{name} must be an absolute Jellyfin path prefix")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]) or "\x00" in value:
        raise ConfigError(f"{location}.{name} must be an absolute Jellyfin path prefix")
    return value.rstrip("/") or "/"


def _reconciliation_policy(table: Mapping[str, object], location: str) -> ReconciliationPolicy:
    _require_keys(table, {
        "minimum_age_days", "terminal_horizon_days", "cooldown_seconds",
        "cooldown_step_days", "cooldown_multiplier", "maximum_cooldown_seconds",
        "final_search", "max_attempts", "max_searches_per_run",
        "max_searches_per_hour", "max_searches_per_day", "max_grabs_per_run",
        "minimum_candidate_score", "minimum_score_gain",
        "required_candidate_formats", "forbidden_candidate_formats",
        "schedule_policy_revision", "release_timeout_seconds", "max_release_response_bytes",
        "transient_retry_seconds", "transient_retry_multiplier", "maximum_transient_retry_seconds",
    }, location)
    minimum_age_days = _required_nonnegative(table, "minimum_age_days", location, 36_500)
    terminal_horizon_days = _required_positive(table, "terminal_horizon_days", location, 36_500)
    if terminal_horizon_days < minimum_age_days:
        raise ConfigError(f"{location}.terminal_horizon_days must not precede minimum_age_days")
    policy = ReconciliationPolicy(
        minimum_age_days,
        terminal_horizon_days,
        _required_nonnegative(table, "cooldown_seconds", location, 31_536_000),
        _optional_positive(table, "cooldown_step_days", location, 36_500, default=7),
        _optional_multiplier(table, "cooldown_multiplier", location, default=1.0),
        _optional_nonnegative(table, "maximum_cooldown_seconds", location, 31_536_000, default=0),
        _optional_bool(table, "final_search", location, default=False),
        _required_positive(table, "max_attempts", location, 1000),
        _required_positive(table, "max_searches_per_run", location, 10_000),
        _optional_positive(table, "max_searches_per_hour", location, 10_000, default=10_000),
        _optional_positive(table, "max_searches_per_day", location, 100_000, default=100_000),
        _optional_positive(table, "max_grabs_per_run", location, 10_000, default=1),
        _optional_integer(table, "minimum_candidate_score", location, -(2**31), 2**31 - 1, default=-(2**31)),
        _optional_integer(table, "minimum_score_gain", location, -(2**31), 2**31 - 1, default=-(2**31)),
        _optional_format_names(table, "required_candidate_formats", location),
        _optional_format_names(table, "forbidden_candidate_formats", location),
        _required_revision(table, "schedule_policy_revision", location),
        _required_positive(table, "release_timeout_seconds", location, 3_600),
        _required_positive(table, "max_release_response_bytes", location, 64 * 1024 * 1024),
        _required_positive(table, "transient_retry_seconds", location, 31_536_000),
        _required_multiplier(table, "transient_retry_multiplier", location),
        _required_positive(table, "maximum_transient_retry_seconds", location, 31_536_000),
    )
    if policy.maximum_transient_retry_seconds < policy.transient_retry_seconds:
        raise ConfigError(f"{location}.maximum_transient_retry_seconds must not be less than transient_retry_seconds")
    return policy


def _component(table: Mapping[str, object], name: str) -> ComponentConfig:
    if not table:
        return ComponentConfig(name)
    return ComponentConfig(name)


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
    secret_fields = (
        (("api_key",) if "api_key" in table else ("username", "password"))
        if name == "qbittorrent"
        else ("api_key",)
    )
    _require_keys(table, {"base_url", *secret_fields}, f"adapters.{name}")
    try:
        base_url = table["base_url"]
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
    secrets: dict[str, SecretReference] = {}
    for field in secret_fields:
        try:
            secrets[field] = SecretReference.parse(table[field], f"adapters.{name}.{field}")
        except KeyError as exc:
            raise ConfigError(f"missing required key: adapters.{name}.{field}") from exc
    return AdapterConfig(name, base_url.rstrip("/"), secrets)


def _capacity_pools(value: object) -> dict[str, CapacityPool]:
    table = _table(value, "capacity_pools")
    if not table:
        raise ConfigError("capacity_pools must not be empty")
    pools: dict[str, CapacityPool] = {}
    for name, raw_pool in table.items():
        if not isinstance(name, str) or not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ConfigError("capacity_pools has an unsafe pool name")
        pool = _table(raw_pool, f"capacity_pools.{name}")
        _require_keys(pool, {"probe_path", "minimum_free_bytes", "safety_margin_bytes"}, f"capacity_pools.{name}")
        pools[name] = CapacityPool(
            name,
            _required_path(pool, "probe_path", f"capacity_pools.{name}"),
            _required_nonnegative(pool, "minimum_free_bytes", f"capacity_pools.{name}", 2**63 - 1),
            _required_nonnegative(pool, "safety_margin_bytes", f"capacity_pools.{name}", 2**63 - 1),
        )
    return pools


def _import_reconciliation(value: object) -> ImportReconciliationConfig:
    table = _table(value, "publisher.import_reconciliation")
    _require_keys(
        table,
        {"poll_interval_seconds", "initial_history_lookback_days", "max_imports_per_poll"},
        "publisher.import_reconciliation",
    )
    return ImportReconciliationConfig(
        _required_positive(table, "poll_interval_seconds", "publisher.import_reconciliation", 86_400),
        _required_nonnegative(table, "initial_history_lookback_days", "publisher.import_reconciliation", 36_500),
        _required_positive(table, "max_imports_per_poll", "publisher.import_reconciliation", 256),
    )


def _video_candidate_health(value: object) -> VideoCandidateHealthConfig:
    table = _table(value, "fence.video_candidate_health")
    _require_keys(table, {"poll_interval_seconds", "metadata_timeout_seconds", "no_progress_timeout_seconds", "minimum_failure_observations", "replacement_initial_delay_seconds", "replacement_multiplier", "replacement_max_delay_seconds"}, "fence.video_candidate_health")
    policy = VideoCandidateHealthConfig(
        _required_positive(table, "poll_interval_seconds", "fence.video_candidate_health", 86_400),
        _required_positive(table, "metadata_timeout_seconds", "fence.video_candidate_health", 31_536_000),
        _required_positive(table, "no_progress_timeout_seconds", "fence.video_candidate_health", 31_536_000),
        _required_positive(table, "minimum_failure_observations", "fence.video_candidate_health", 100),
        _required_positive(table, "replacement_initial_delay_seconds", "fence.video_candidate_health", 31_536_000),
        _required_multiplier(table, "replacement_multiplier", "fence.video_candidate_health"),
        _required_positive(table, "replacement_max_delay_seconds", "fence.video_candidate_health", 31_536_000),
    )
    if policy.replacement_max_delay_seconds < policy.replacement_initial_delay_seconds:
        raise ConfigError("fence.video_candidate_health.replacement_max_delay_seconds must not be less than replacement_initial_delay_seconds")
    return policy


def _sources(value: object, pools: Mapping[str, CapacityPool]) -> dict[str, SourceProfile]:
    table = _table(value, "sources")
    if set(table) != set(_SOURCE_KINDS):
        raise ConfigError("sources must contain exactly radarr and sonarr")
    profiles: dict[str, SourceProfile] = {}
    allowed = {
        "kind", "download_client_id", "category", "qbittorrent_save_path", "arr_import_path_prefix",
        "staging_root", "canonical_root", "download_pool", "staging_pool", "canonical_pool",
        "namespace", "jellyfin_library_id", "jellyfin_path_prefix", "bundle_settle_seconds", "bundle_sidecar_extensions", "bundle_required_languages", "bundle_required_subtitle_languages", "bundle_language_aliases", "bundle_required_container_evidence",
    }
    for name, expected_kind in _SOURCE_KINDS.items():
        profile = _table(table[name], f"sources.{name}")
        _require_keys(profile, allowed, f"sources.{name}")
        kind = profile.get("kind")
        if kind != expected_kind:
            raise ConfigError(f"sources.{name}.kind must be {expected_kind}")
        download_pool = _required_pool_name(profile, "download_pool", f"sources.{name}")
        staging_pool = _required_pool_name(profile, "staging_pool", f"sources.{name}")
        canonical_pool = _required_pool_name(profile, "canonical_pool", f"sources.{name}")
        if any(pool not in pools for pool in (download_pool, staging_pool, canonical_pool)):
            raise ConfigError(f"sources.{name} references a missing capacity pool")
        profiles[name] = SourceProfile(
            name,
            kind,
            _required_positive(profile, "download_client_id", f"sources.{name}", 2**31 - 1),
            _required_category(profile, "category", f"sources.{name}"),
            _required_path(profile, "qbittorrent_save_path", f"sources.{name}"),
            _required_posix_prefix(profile, "arr_import_path_prefix", f"sources.{name}"),
            _required_path(profile, "staging_root", f"sources.{name}"),
            _required_path(profile, "canonical_root", f"sources.{name}"),
            download_pool,
            staging_pool,
            canonical_pool,
            _required_namespace(profile, "namespace", f"sources.{name}"),
            _required_uuid(profile, "jellyfin_library_id", f"sources.{name}"),
            _required_posix_prefix(profile, "jellyfin_path_prefix", f"sources.{name}"),
            _optional_nonnegative(profile, "bundle_settle_seconds", f"sources.{name}", 60, default=2),
            _optional_bundle_strings(profile, "bundle_sidecar_extensions", f"sources.{name}", default=(".ass", ".ssa", ".srt", ".vtt"), extension=True),
            _optional_bundle_strings(profile, "bundle_required_languages", f"sources.{name}", default=()),
            _optional_bundle_strings(profile, "bundle_required_subtitle_languages", f"sources.{name}", default=()),
            _optional_bundle_aliases(profile, f"sources.{name}"),
            _optional_container_evidence(profile, f"sources.{name}"),
        )
    if len({profile.category for profile in profiles.values()}) != len(profiles):
        raise ConfigError("source categories must be distinct")
    if len({profile.namespace for profile in profiles.values()}) != len(profiles):
        raise ConfigError("source namespaces must be distinct")
    if len({profile.jellyfin_library_id for profile in profiles.values()}) != len(profiles):
        raise ConfigError("source Jellyfin library identities must be distinct")
    return profiles


def _materialized_device(path: Path, *, reject_symlink: bool = False) -> int | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError("configured capacity path is unreadable") from exc
    if reject_symlink and os.path.islink(path):
        raise ConfigError("configured capacity path must not be a symlink")
    if os.path.islink(path):
        return None
    return metadata.st_dev


def _validate_materialized_pool_bindings(sources: Mapping[str, SourceProfile], pools: Mapping[str, CapacityPool]) -> None:
    """Bind existing roots to their declared probe without creating any path."""
    pool_devices = {name: _materialized_device(pool.probe_path, reject_symlink=True) for name, pool in pools.items()}
    present = [(name, device) for name, device in pool_devices.items() if device is not None]
    if len({device for _, device in present}) != len(present):
        raise ConfigError("materialized capacity pools must not alias one filesystem")
    for source in sources.values():
        for root, pool_name in ((source.qbittorrent_save_path, source.download_pool), (source.staging_root, source.staging_pool), (source.canonical_root, source.canonical_pool)):
            root_device = _materialized_device(root)
            probe_device = pool_devices[pool_name]
            if root_device is not None and probe_device is not None and root_device != probe_device:
                raise ConfigError("source root filesystem does not match its capacity pool")


def load_config(path: Path) -> ProductConfig:
    """Load a strict shared configuration without resolving secret values."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("configuration cannot be read as TOML") from exc
    _require_keys(document, _TOP_LEVEL, "root")
    runtime_table = _table(document.get("media_interlock"), "media_interlock")
    _require_keys(runtime_table, {"state_dir"}, "media_interlock")
    runtime = RuntimeConfig(_required_path(runtime_table, "state_dir", "media_interlock"))

    has_component = any(name in document for name in ("fence", "publisher", "reconciler"))
    if has_component and ("sources" not in document or "capacity_pools" not in document):
        raise ConfigError("configured components require sources and capacity_pools")
    capacity_pools = _capacity_pools(document["capacity_pools"]) if "capacity_pools" in document else {}
    sources = _sources(document["sources"], capacity_pools) if "sources" in document else {}
    _validate_materialized_pool_bindings(sources, capacity_pools)

    components: dict[str, ComponentConfig | None] = {name: None for name in ("fence", "publisher", "reconciler")}
    for name in components:
        if name in document:
            components[name] = _component(_table(document[name], name), name)
    fence: FenceConfig | None = None
    if components["fence"] is not None:
        table = _table(document["fence"], "fence")
        _require_keys(table, {"capacity_bytes", "max_inflight", "mutation_lock_path", "mutation_lock_version", "mutation_lock_timeout_ms", "video_candidate_health"}, "fence")
        base = components["fence"]
        assert base is not None
        version = table.get("mutation_lock_version")
        if version != _MUTATION_LOCK_VERSION:
            raise ConfigError(f"fence.mutation_lock_version must be {_MUTATION_LOCK_VERSION}")
        fence = FenceConfig(
            base.name,
            _required_positive(table, "capacity_bytes", "fence", 2**63 - 1),
            _required_positive(table, "max_inflight", "fence", 100_000),
            MutationLockConfig(
                _required_path(table, "mutation_lock_path", "fence"),
                version,
                _required_positive(table, "mutation_lock_timeout_ms", "fence", 60_000),
            ),
            {
                name: FenceSourceProfile(name, source.category, source.download_client_id, source.qbittorrent_save_path, source.download_pool, source.staging_pool, source.canonical_pool)
                for name, source in sources.items()
            },
            _video_candidate_health(_table(table.get("video_candidate_health"), "fence.video_candidate_health")),
        )
    publisher: PublisherConfig | None = None
    if components["publisher"] is not None:
        table = _table(document["publisher"], "publisher")
        _require_keys(table, {"import_reconciliation"}, "publisher")
        base = components["publisher"]
        assert base is not None
        publisher = PublisherConfig(
            base.name,
            {
                name: PublisherSourceProfile(name, source.arr_import_path_prefix, source.staging_root, source.canonical_root, source.namespace, source.jellyfin_library_id, source.jellyfin_path_prefix, source.bundle_settle_seconds, source.bundle_sidecar_extensions, source.bundle_required_languages, source.bundle_required_subtitle_languages, source.bundle_language_aliases, source.bundle_required_container_evidence)
                for name, source in sources.items()
            },
            _import_reconciliation(_table(table.get("import_reconciliation"), "publisher.import_reconciliation")),
        )
    reconciler = components["reconciler"]
    if reconciler is not None:
        table = _table(document["reconciler"], "reconciler")
        _require_keys(table, {"poll_interval_seconds", "movie", "episode"}, "reconciler")
        reconciler = ReconcilerConfig(
            reconciler.name,
            _optional_positive(table, "poll_interval_seconds", "reconciler", 86_400, default=300),
            _reconciliation_policy(_table(table.get("movie"), "reconciler.movie"), "reconciler.movie"),
            _reconciliation_policy(_table(table.get("episode"), "reconciler.episode"), "reconciler.episode"),
            {name: ReconcilerSourceProfile(name, source.kind, source.category, source.download_client_id) for name, source in sources.items()},
        )

    writable_roots: list[tuple[str, Path]] = []
    for source in sources.values():
        writable_roots.extend(((f"sources.{source.name}.qbittorrent_save_path", source.qbittorrent_save_path), (f"sources.{source.name}.staging_root", source.staging_root), (f"sources.{source.name}.canonical_root", source.canonical_root)))
        _validate_disjoint(source.qbittorrent_save_path, source.staging_root, f"sources.{source.name}.qbittorrent_save_path", f"sources.{source.name}.staging_root")
        _validate_disjoint(source.qbittorrent_save_path, source.canonical_root, f"sources.{source.name}.qbittorrent_save_path", f"sources.{source.name}.canonical_root")
        _validate_disjoint(source.staging_root, source.canonical_root, f"sources.{source.name}.staging_root", f"sources.{source.name}.canonical_root")
    for index, (left_name, left_root) in enumerate(writable_roots):
        for right_name, right_root in writable_roots[index + 1:]:
            _validate_disjoint(left_root, right_root, left_name, right_name)
    for root_name, root in writable_roots:
        _validate_disjoint(runtime.state_dir, root, "media_interlock.state_dir", root_name)
    adapters_table = _table(document.get("adapters", {}), "adapters")
    _require_keys(adapters_table, _ADAPTERS, "adapters")
    adapters = {name: _adapter(name, _table(table, f"adapters.{name}")) for name, table in adapters_table.items()}
    return ProductConfig(runtime, fence, publisher, reconciler, adapters, sources, capacity_pools)
