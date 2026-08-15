from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import _source_tree  # noqa: F401

from media_interlock import __version__
from media_interlock import cli
from media_interlock.runtime import RuntimeState


class ComponentProbeTests(unittest.TestCase):
    def test_version_probe_needs_no_configuration_or_external_service(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as rendered:
            self.assertEqual(0, cli.main(["--version"]))
            self.assertEqual(__version__, rendered.getvalue().strip())

    def test_configuration_probe_is_local_and_does_not_require_a_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / "media-interlock.toml"
            configuration.write_text(f'[media_interlock]\nstate_dir = "{Path(directory) / "state"}"\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as rendered:
                self.assertEqual(0, cli.main(["--config", str(configuration), "--check-config", "--json"]))
                self.assertIn('"status":"ok"', rendered.getvalue())

    def test_daemon_uses_the_single_composed_runtime(self) -> None:
        runtime = Mock()
        with patch("media_interlock.cli.load_config", return_value=object()), patch(
            "media_interlock.cli.MediaInterlockRuntime.from_config", return_value=runtime
        ), patch("media_interlock.cli.asyncio.run") as run:
            self.assertEqual(0, cli.main(["--config", "/synthetic/config.toml", "--daemon"]))

        run.assert_called_once_with(runtime.run())
        runtime.close.assert_called_once_with()

    def test_status_is_a_short_local_probe_not_a_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            configuration = Path(directory) / "media-interlock.toml"
            configuration.write_text(f'[media_interlock]\nstate_dir = "{state_dir}"\n', encoding="utf-8")
            runtime_state = RuntimeState.open(state_dir)
            runtime_state.close()
            with contextlib.redirect_stdout(io.StringIO()) as rendered:
                self.assertEqual(0, cli.main(["--config", str(configuration), "--status", "--json"]))
            self.assertIn('"status":"ok"', rendered.getvalue())

    def test_status_reads_an_active_runtime_store_without_competing_for_writer_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            configuration = Path(directory) / "media-interlock.toml"
            configuration.write_text(f'[media_interlock]\nstate_dir = "{state_dir}"\n', encoding="utf-8")
            runtime_state = RuntimeState.open(state_dir)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as rendered:
                    self.assertEqual(0, cli.main(["--config", str(configuration), "--status", "--json"]))
                self.assertIn('"status":"ok"', rendered.getvalue())
            finally:
                runtime_state.close()
