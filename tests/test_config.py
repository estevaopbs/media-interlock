from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.config import ConfigError, load_config


VALID_CONFIG = """
[shared]
runtime_dir = "/run/media-interlock"

[fence]
state_dir = "/var/lib/media-interlock/fence"
socket_path = "/run/media-interlock/fence.sock"
staging_root = "/srv/staging"
qbittorrent_category = "media-interlock"
capacity_bytes = 1000000
max_inflight = 2

[publisher]
state_dir = "/var/lib/media-interlock/publisher"
socket_path = "/run/media-interlock/publisher.sock"
staging_root = "/srv/staging"
canonical_root = "/srv/library"

[adapters.prowlarr]
base_url = "https://prowlarr.example.invalid"
api_key = "env:PROWLARR_API_KEY"
"""


class ConfigurationTests(unittest.TestCase):
    def write(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "media-interlock.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_typed_component_projections_without_resolving_secrets(self) -> None:
        config = load_config(self.write(VALID_CONFIG))

        self.assertEqual("/srv/library", str(config.publisher.canonical_root))
        self.assertEqual("env", config.adapters["prowlarr"].secrets["api_key"].source)
        self.assertNotIn("PROWLARR_API_KEY", repr(config.redacted()))
        self.assertEqual("env:<redacted>", config.redacted()["adapters"]["prowlarr"]["api_key"])

    def test_rejects_unknown_and_ambiguous_configuration_before_effects(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            load_config(self.write(VALID_CONFIG.replace("max_inflight = 2", "max_inflight = 2\nunsafe = true")))
        with self.assertRaisesRegex(ConfigError, "must be disjoint"):
            load_config(self.write(VALID_CONFIG.replace('canonical_root = "/srv/library"', 'canonical_root = "/srv/staging/library"')))
        with self.assertRaisesRegex(ConfigError, "must be disjoint"):
            load_config(self.write(VALID_CONFIG.replace('staging_root = "/srv/staging"', 'staging_root = "/srv/library/downloads"', 1)))
        with self.assertRaisesRegex(ConfigError, "must use env: or file:"):
            load_config(self.write(VALID_CONFIG.replace("env:PROWLARR_API_KEY", "plaintext-secret")))
        with self.assertRaisesRegex(ConfigError, "must not contain credentials"):
            load_config(self.write(VALID_CONFIG.replace("https://prowlarr.example.invalid", "https://api:secret@prowlarr.example.invalid")))
        with self.assertRaisesRegex(ConfigError, "bounded"):
            load_config(self.write(VALID_CONFIG.replace("max_inflight = 2", "max_inflight = 100001")))
        with self.assertRaisesRegex(ConfigError, "base_url must be a valid"):
            load_config(self.write(VALID_CONFIG.replace("https://prowlarr.example.invalid", "http://host:notaport")))

    def test_rejects_existing_symlink_aliases_between_canonical_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            alias = root / "alias"
            alias.symlink_to(staging, target_is_directory=True)
            content = VALID_CONFIG.replace('staging_root = "/srv/staging"', f'staging_root = "{staging}"', 1)
            content = content.replace('staging_root = "/srv/staging"', f'staging_root = "{staging}"', 1)
            content = content.replace('canonical_root = "/srv/library"', f'canonical_root = "{alias}"')
            with self.assertRaisesRegex(ConfigError, "must be disjoint"):
                load_config(self.write(content))

    def test_rejects_private_or_runtime_roots_under_an_acquisition_root(self) -> None:
        content = VALID_CONFIG.replace('state_dir = "/var/lib/media-interlock/fence"', 'state_dir = "/srv/staging/.state"')
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
