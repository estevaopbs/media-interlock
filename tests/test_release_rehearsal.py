"""Black-box release contracts for the one-process MediaInterlock artifact."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.runtime import RuntimeState


class ReleaseRehearsalTests(unittest.TestCase):
    def test_shared_runtime_never_creates_component_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state = RuntimeState.open(state_dir)
            try:
                self.assertEqual([], list(state_dir.glob("*.sock")))
                self.assertEqual([], list(state_dir.glob("fence")))
                self.assertEqual([], list(state_dir.glob("publisher")))
                self.assertEqual([], list(state_dir.glob("reconciler")))
            finally:
                state.close()

if __name__ == "__main__":
    unittest.main()
