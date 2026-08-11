from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _source_tree  # noqa: F401

from media_interlock.config import ConfigError, load_config


VALID_CONFIG = """
[shared]
runtime_dir = "/run/media-interlock"

[fence]
state_dir = "/var/lib/media-interlock/fence"
socket_path = "/run/media-interlock/fence.sock"
staging_root = "/srv/staging"
radarr_category = "media-interlock-radarr"
sonarr_category = "media-interlock-sonarr"
capacity_bytes = 1000000
max_inflight = 2

[publisher]
state_dir = "/var/lib/media-interlock/publisher"
socket_path = "/run/media-interlock/publisher.sock"
staging_root = "/srv/staging"
canonical_root = "/srv/library"
jellyfin_library_id = "2f9e0f39-70de-4502-85ce-7ed03cd2f01f"
namespace = "library"
jellyfin_path_prefix = "/jellyfin/library"

[reconciler]
state_dir = "/var/lib/media-interlock/reconciler"
socket_path = "/run/media-interlock/reconciler.sock"

[reconciler.movie]
minimum_age_days = 30
terminal_horizon_days = 365
cooldown_seconds = 86400
max_attempts = 3
max_searches_per_run = 5

[reconciler.episode]
minimum_age_days = 7
terminal_horizon_days = 180
cooldown_seconds = 3600
max_attempts = 2
max_searches_per_run = 10

[adapters.prowlarr]
base_url = "https://prowlarr.example.invalid"
api_key = "env:PROWLARR_API_KEY"
"""


SOURCE_PROFILE_CONFIG = """
[shared]
runtime_dir = "/run/media-interlock"

[fence]
state_dir = "/var/lib/media-interlock/fence"
socket_path = "/run/media-interlock/fence.sock"
capacity_bytes = 1000000
max_inflight = 2
mutation_lock_path = "/run/media-interlock/qbittorrent-mutation.lock"
mutation_lock_version = "shared-qbittorrent-mutation/v1"
mutation_lock_timeout_ms = 500

[publisher]
state_dir = "/var/lib/media-interlock/publisher"
socket_path = "/run/media-interlock/publisher.sock"

[reconciler]
state_dir = "/var/lib/media-interlock/reconciler"
socket_path = "/run/media-interlock/reconciler.sock"

[reconciler.movie]
minimum_age_days = 30
terminal_horizon_days = 365
cooldown_seconds = 86400
max_attempts = 3
max_searches_per_run = 5

[reconciler.episode]
minimum_age_days = 7
terminal_horizon_days = 180
cooldown_seconds = 3600
max_attempts = 2
max_searches_per_run = 10

[capacity_pools.download]
probe_path = "/srv/downloads"
minimum_free_bytes = 100
safety_margin_bytes = 100

[capacity_pools.staging]
probe_path = "/srv/staging"
minimum_free_bytes = 100
safety_margin_bytes = 100

[capacity_pools.canonical]
probe_path = "/srv/library"
minimum_free_bytes = 100
safety_margin_bytes = 100

[sources.radarr]
kind = "movie"
download_client_id = 7
category = "media-interlock-radarr"
qbittorrent_save_path = "/srv/downloads/movies"
arr_import_path_prefix = "/downloads/movies"
staging_root = "/srv/staging/movies"
canonical_root = "/srv/library/movies"
download_pool = "download"
staging_pool = "staging"
canonical_pool = "canonical"
namespace = "movies"
jellyfin_library_id = "2f9e0f39-70de-4502-85ce-7ed03cd2f01f"
jellyfin_path_prefix = "/jellyfin/movies"

[sources.sonarr]
kind = "episode"
download_client_id = 8
category = "media-interlock-sonarr"
qbittorrent_save_path = "/srv/downloads/episodes"
arr_import_path_prefix = "/downloads/episodes"
staging_root = "/srv/staging/episodes"
canonical_root = "/srv/library/episodes"
download_pool = "download"
staging_pool = "staging"
canonical_pool = "canonical"
namespace = "episodes"
jellyfin_library_id = "6d3e0f39-70de-4502-85ce-7ed03cd2f01f"
jellyfin_path_prefix = "/jellyfin/episodes"

[adapters.prowlarr]
base_url = "https://prowlarr.example.invalid"
api_key = "env:PROWLARR_API_KEY"
"""

# Every configuration test uses the replacement schema; the old singular
# Fence/Publisher roots are intentionally not a compatibility surface.
VALID_CONFIG = SOURCE_PROFILE_CONFIG


class ConfigurationTests(unittest.TestCase):
    def write(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "media-interlock.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_typed_component_projections_without_resolving_secrets(self) -> None:
        config = load_config(self.write(VALID_CONFIG))

        self.assertEqual("/srv/library/movies", str(config.sources["radarr"].canonical_root))
        self.assertEqual("movies", config.sources["radarr"].namespace)
        self.assertEqual("/jellyfin/movies", config.sources["radarr"].jellyfin_path_prefix)
        self.assertEqual(30, config.reconciler.movie.minimum_age_days)
        self.assertEqual(10, config.reconciler.episode.max_searches_per_run)
        self.assertEqual("env", config.adapters["prowlarr"].secrets["api_key"].source)
        self.assertNotIn("PROWLARR_API_KEY", repr(config.redacted()))
        self.assertEqual("env:<redacted>", config.redacted()["adapters"]["prowlarr"]["api_key"])

    def test_projects_exact_radarr_and_sonarr_source_profiles(self) -> None:
        config = load_config(self.write(SOURCE_PROFILE_CONFIG))

        self.assertEqual({"radarr", "sonarr"}, set(config.sources))
        self.assertEqual("movie", config.sources["radarr"].kind)
        self.assertEqual(7, config.sources["radarr"].download_client_id)
        self.assertEqual("/srv/downloads/episodes", str(config.sources["sonarr"].qbittorrent_save_path))
        self.assertEqual("shared-qbittorrent-mutation/v1", config.fence.mutation_lock.version)
        self.assertEqual(2, config.publisher.sources["radarr"].bundle_settle_seconds)

    def test_download_client_ids_are_scoped_to_each_arr(self) -> None:
        content = SOURCE_PROFILE_CONFIG.replace("download_client_id = 8", "download_client_id = 7")

        config = load_config(self.write(content))

        self.assertEqual(7, config.sources["radarr"].download_client_id)
        self.assertEqual(7, config.sources["sonarr"].download_client_id)

    def test_arr_visible_prefix_may_be_shared_while_publisher_stagings_are_distinct(self) -> None:
        content = SOURCE_PROFILE_CONFIG.replace(
            'arr_import_path_prefix = "/downloads/movies"',
            'arr_import_path_prefix = "/data/library"',
        ).replace(
            'arr_import_path_prefix = "/downloads/episodes"',
            'arr_import_path_prefix = "/data/library"',
        )

        config = load_config(self.write(content))

        assert config.publisher is not None
        self.assertEqual("/data/library", config.publisher.sources["radarr"].arr_import_path_prefix)
        self.assertEqual("/data/library", config.publisher.sources["sonarr"].arr_import_path_prefix)
        self.assertEqual(Path("/srv/staging/movies"), config.publisher.sources["radarr"].staging_root)
        self.assertEqual(Path("/srv/staging/episodes"), config.publisher.sources["sonarr"].staging_root)

    def test_bundle_settle_policy_is_bounded_per_source(self) -> None:
        content = VALID_CONFIG.replace('namespace = "movies"', 'namespace = "movies"\nbundle_settle_seconds = 7', 1)

        config = load_config(self.write(content))

        self.assertEqual(7, config.publisher.sources["radarr"].bundle_settle_seconds)
        with self.assertRaisesRegex(ConfigError, "bundle_settle_seconds"):
            load_config(self.write(content.replace("bundle_settle_seconds = 7", "bundle_settle_seconds = 61")))

    def test_bundle_container_evidence_policy_is_strict(self) -> None:
        content = VALID_CONFIG.replace('namespace = "movies"', 'namespace = "movies"\nbundle_required_container_evidence = ["container:mkv"]', 1)
        config = load_config(self.write(content))

        self.assertEqual(("container:mkv",), config.publisher.sources["radarr"].bundle_required_container_evidence)
        with self.assertRaisesRegex(ConfigError, "container_evidence"):
            load_config(self.write(content.replace("container:mkv", "codec:h264")))

    def test_rejects_unknown_and_ambiguous_configuration_before_effects(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(self.write(VALID_CONFIG.replace("max_inflight = 2", "max_inflight = 2\nunsafe = true")))
        with self.assertRaisesRegex(ConfigError, "must be disjoint"):
            load_config(self.write(VALID_CONFIG.replace('canonical_root = "/srv/library/movies"', 'canonical_root = "/srv/staging/movies"')))
        with self.assertRaisesRegex(ConfigError, "must be disjoint"):
            load_config(self.write(VALID_CONFIG.replace('staging_root = "/srv/staging/movies"', 'staging_root = "/srv/library/movies/staging"')))
        with self.assertRaisesRegex(ConfigError, "must use env: or file:"):
            load_config(self.write(VALID_CONFIG.replace("env:PROWLARR_API_KEY", "plaintext-secret")))
        with self.assertRaisesRegex(ConfigError, "must not contain credentials"):
            load_config(self.write(VALID_CONFIG.replace("https://prowlarr.example.invalid", "https://api:secret@prowlarr.example.invalid")))
        with self.assertRaisesRegex(ConfigError, "bounded"):
            load_config(self.write(VALID_CONFIG.replace("max_inflight = 2", "max_inflight = 100001")))
        with self.assertRaisesRegex(ConfigError, "base_url must be a valid"):
            load_config(self.write(VALID_CONFIG.replace("https://prowlarr.example.invalid", "http://host:notaport")))

    def test_publisher_catalog_binding_is_typed_and_never_item_mapped(self) -> None:
        with self.assertRaisesRegex(ConfigError, "jellyfin_library_id"):
            load_config(self.write(VALID_CONFIG.replace("2f9e0f39-70de-4502-85ce-7ed03cd2f01f", "not-a-uuid")))
        with self.assertRaisesRegex(ConfigError, "namespace"):
            load_config(self.write(VALID_CONFIG.replace('namespace = "movies"', 'namespace = "movies/nested"')))
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(self.write(VALID_CONFIG + 'jellyfin_item_id = "manual-mapping"\n'))

    def test_reconciler_policy_is_typed_bounded_and_separate_per_media_type(self) -> None:
        with self.assertRaisesRegex(ConfigError, "reconciler.movie.max_attempts"):
            load_config(self.write(VALID_CONFIG.replace("max_attempts = 3", "max_attempts = 0", 1)))
        with self.assertRaisesRegex(ConfigError, "terminal_horizon_days"):
            load_config(self.write(VALID_CONFIG.replace("terminal_horizon_days = 365", "terminal_horizon_days = 29")))
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(self.write(VALID_CONFIG.replace("max_searches_per_run = 5", "max_searches_per_run = 5\nmanual_item = 42", 1)))

    def test_fence_source_categories_are_typed_and_must_be_distinct(self) -> None:
        config = load_config(self.write(VALID_CONFIG))
        self.assertEqual("media-interlock-radarr", config.sources["radarr"].category)
        self.assertEqual("media-interlock-sonarr", config.sources["sonarr"].category)
        with self.assertRaisesRegex(ConfigError, "must be distinct"):
            load_config(self.write(VALID_CONFIG.replace('category = "media-interlock-sonarr"', 'category = "media-interlock-radarr"')))

    def test_rejects_existing_symlink_aliases_between_canonical_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            alias = root / "alias"
            alias.symlink_to(staging, target_is_directory=True)
            content = VALID_CONFIG.replace('staging_root = "/srv/staging/movies"', f'staging_root = "{staging}"')
            content = content.replace('canonical_root = "/srv/library/movies"', f'canonical_root = "{alias}"')
            with self.assertRaisesRegex(ConfigError, "must be disjoint"):
                load_config(self.write(content))

    def test_materialized_pool_alias_and_root_identity_drift_fail_before_startup(self) -> None:
        roots = {
            "/srv/downloads": "/materialized/downloads",
            "/srv/staging": "/materialized/staging",
            "/srv/library": "/materialized/library",
        }
        content = VALID_CONFIG
        for original, replacement in roots.items():
            content = content.replace(original, replacement)

        def devices(*, download_pool: int, staging_pool: int, canonical_pool: int, canonical_root: int) -> dict[str, int]:
            return {
                "/materialized/downloads": download_pool,
                "/materialized/staging": staging_pool,
                "/materialized/library": canonical_pool,
                "/materialized/downloads/movies": download_pool,
                "/materialized/downloads/episodes": download_pool,
                "/materialized/staging/movies": staging_pool,
                "/materialized/staging/episodes": staging_pool,
                "/materialized/library/movies": canonical_root,
                "/materialized/library/episodes": canonical_root,
            }

        with patch("media_interlock.config._materialized_device", side_effect=lambda path, **_: devices(download_pool=1, staging_pool=1, canonical_pool=2, canonical_root=2).get(str(path))):
            with self.assertRaisesRegex(ConfigError, "must not alias"):
                load_config(self.write(content))

        with patch("media_interlock.config._materialized_device", side_effect=lambda path, **_: devices(download_pool=1, staging_pool=2, canonical_pool=3, canonical_root=4).get(str(path))):
            with self.assertRaisesRegex(ConfigError, "does not match"):
                load_config(self.write(content))

    def test_rejects_private_or_runtime_roots_under_an_acquisition_root(self) -> None:
        content = VALID_CONFIG.replace('state_dir = "/var/lib/media-interlock/fence"', 'state_dir = "/srv/staging/movies/.state"')
        with self.assertRaisesRegex(ConfigError, "must be disjoint"):
            load_config(self.write(content))

    def test_resolves_secret_only_on_demand_and_never_serializes_value(self) -> None:
        config = load_config(self.write(VALID_CONFIG))
        previous = os.environ.get("PROWLARR_API_KEY")
        self.addCleanup(
            lambda: os.environ.__setitem__("PROWLARR_API_KEY", previous)
            if previous is not None
            else os.environ.pop("PROWLARR_API_KEY", None)
        )
        os.environ["PROWLARR_API_KEY"] = "actual-secret-value"

        secret = config.adapters["prowlarr"].secrets["api_key"]
        self.assertEqual("actual-secret-value", secret.resolve())
        self.assertNotIn("actual-secret-value", repr(config))

    def test_qbittorrent_uses_distinct_username_and_password_references(self) -> None:
        content = VALID_CONFIG + """
[adapters.qbittorrent]
base_url = "https://qbittorrent.example.invalid"
username = "env:QBITTORRENT_USERNAME"
password = "file:/run/secrets/qbittorrent-password"
"""

        config = load_config(self.write(content))

        self.assertEqual({"username", "password"}, set(config.adapters["qbittorrent"].secrets))
        self.assertEqual("env:<redacted>", config.redacted()["adapters"]["qbittorrent"]["username"])
        with self.assertRaisesRegex(ConfigError, "missing required key: adapters.qbittorrent.password"):
            load_config(self.write(content.replace('password = "file:/run/secrets/qbittorrent-password"\n', "")))

    def test_qbittorrent_may_use_one_api_key_reference(self) -> None:
        content = VALID_CONFIG + """
[adapters.qbittorrent]
base_url = "https://qbittorrent.example.invalid"
api_key = "env:QBITTORRENT_API_KEY"
"""

        config = load_config(self.write(content))

        self.assertEqual({"api_key"}, set(config.adapters["qbittorrent"].secrets))
