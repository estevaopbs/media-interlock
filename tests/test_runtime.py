from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import _source_tree  # noqa: F401

from media_interlock._infra.state import SqliteStore
from media_interlock.config import ConfigError, load_config
from media_interlock.runtime import MediaInterlockRuntime, RuntimeState


class RuntimeStateTests(unittest.TestCase):
    def test_music_reconciliation_runs_when_video_reconciliation_is_unavailable(self) -> None:
        runtime = MediaInterlockRuntime.__new__(MediaInterlockRuntime)
        runtime.scheduler = Mock()
        runtime.scheduler.run.side_effect = OSError("Radarr unavailable")
        runtime.music_scheduler = Mock()

        runtime._reconciliation_tick()

        runtime.scheduler.run.assert_called_once()
        runtime.music_scheduler.run.assert_called_once()

    def test_startup_recovers_then_reopens_a_persisted_quiescent_fence(self) -> None:
        runtime = MediaInterlockRuntime.__new__(MediaInterlockRuntime)
        runtime.fence = Mock()

        runtime._recover_fence_on_start()

        runtime.fence.recover.assert_called_once_with()
        runtime.fence.reopen.assert_called_once_with()

    def test_runtime_requires_all_three_video_roles_before_opening_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / "media-interlock.toml"
            configuration.write_text(
                f'[media_interlock]\nstate_dir = "{Path(directory) / "state"}"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "Fence, Publisher, and Reconciler"):
                MediaInterlockRuntime.from_config(load_config(configuration))

    def test_components_share_one_owned_sqlite_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "media-interlock"

            state = RuntimeState.open(state_dir)
            try:
                self.assertIs(state.fence.store, state.publisher.store)
                self.assertIs(state.publisher.store, state.reconciler.store)
                self.assertEqual(state_dir / "state.sqlite3", state.database_path)
            finally:
                state.close()

    def test_adopts_legacy_component_records_once_without_rewriting_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "media-interlock"
            legacy_fence = SqliteStore.open(state_dir / "fence", "fence")
            try:
                legacy_fence.put(
                    "fence.reservations.v2",
                    json.dumps({"reservations": [], "watermarks": {}, "quiescing": False}),
                )
            finally:
                legacy_fence.close()

            state = RuntimeState.open(state_dir)
            try:
                self.assertEqual(
                    json.dumps({"reservations": [], "watermarks": {}, "quiescing": False}),
                    state.fence.store.get("fence.reservations.v2"),
                )
                self.assertEqual("complete", state.fence.store.get("media-interlock.legacy-adoption.v1"))
            finally:
                state.close()

            reopened = RuntimeState.open(state_dir)
            try:
                self.assertEqual(
                    json.dumps({"reservations": [], "watermarks": {}, "quiescing": False}),
                    reopened.fence.store.get("fence.reservations.v2"),
                )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
