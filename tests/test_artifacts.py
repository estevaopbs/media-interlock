from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _source_tree  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


class ArtifactDefinitionTests(unittest.TestCase):
    def test_oci_targets_are_nonroot_and_execute_only_declared_daemons(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertIn("FROM docker.io/library/python@sha256:", containerfile)
        self.assertIn("build==1.5.0 setuptools==80.9.0", containerfile)
        self.assertIn("FROM runtime AS fence", containerfile)
        self.assertIn("FROM runtime AS publisher", containerfile)
        self.assertIn("USER media-interlock", containerfile)
        self.assertIn('ENTRYPOINT ["media-interlock-fence"]', containerfile)
        self.assertIn('ENTRYPOINT ["media-interlock-publisher"]', containerfile)
        self.assertNotIn("media-interlock-reconciler", containerfile)

    def test_local_artifact_builder_exposes_only_local_oci_targets(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build-artifacts.py"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--oci-engine", result.stdout)
        self.assertIn("--source-date-epoch", result.stdout)
        self.assertNotIn("push", result.stdout.lower())
