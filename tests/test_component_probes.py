from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock import __version__
from media_interlock.fence import cli as fence_cli
from media_interlock.publisher import cli as publisher_cli
from media_interlock.reconciler import cli as reconciler_cli


class ComponentProbeTests(unittest.TestCase):
    def test_version_probe_needs_no_configuration_or_external_service(self) -> None:
        for component in (reconciler_cli, fence_cli, publisher_cli):
            with self.subTest(component=component.__name__), contextlib.redirect_stdout(io.StringIO()) as rendered:
                self.assertEqual(0, component.main(["--version"]))
                self.assertEqual(__version__, rendered.getvalue().strip())

    def test_configuration_probe_is_local_and_does_not_require_a_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / "media-interlock.toml"
            configuration.write_text(f'[shared]\nruntime_dir = "{Path(directory) / "runtime"}"\n', encoding="utf-8")
            for component in (reconciler_cli, fence_cli, publisher_cli):
                with self.subTest(component=component.__name__), contextlib.redirect_stdout(io.StringIO()) as rendered:
                    self.assertEqual(0, component.main(["--config", str(configuration), "--check-config", "--json"]))
                    self.assertIn('"status":"ok"', rendered.getvalue())
