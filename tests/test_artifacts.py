from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock import __version__


ROOT = Path(__file__).resolve().parents[1]


class ArtifactDefinitionTests(unittest.TestCase):
    def test_corrective_release_version_is_consistent(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("0.1.1", __version__)
        self.assertEqual(__version__, project["project"]["version"])

    def test_oci_targets_are_arbitrary_uid_safe_and_execute_only_declared_components(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertIn("FROM docker.io/library/python@sha256:", containerfile)
        self.assertIn("--require-hashes --no-deps --requirement build-requirements.txt", containerfile)
        self.assertIn("FROM runtime AS reconciler", containerfile)
        self.assertIn("FROM runtime AS fence", containerfile)
        self.assertIn("FROM runtime AS publisher", containerfile)
        self.assertIn("USER 65532:65532", containerfile)
        self.assertNotIn("useradd", containerfile)
        for label in (
            "org.opencontainers.image.source",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.version",
            "org.opencontainers.image.licenses",
        ):
            self.assertIn(label, containerfile)
        self.assertIn('ENTRYPOINT ["media-interlock-reconciler"]', containerfile)
        self.assertIn('ENTRYPOINT ["media-interlock-fence"]', containerfile)
        self.assertIn('ENTRYPOINT ["media-interlock-publisher"]', containerfile)

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

    def test_build_bootstrap_is_hash_locked_and_wheel_only(self) -> None:
        requirements = (ROOT / "build-requirements.txt").read_text(encoding="utf-8")
        builder = (ROOT / "scripts" / "build-artifacts.py").read_text(encoding="utf-8")

        self.assertEqual(4, requirements.count("--hash=sha256:"))
        self.assertIn('"--require-hashes", "--no-deps"', builder)
        self.assertIn('"--wheel", "--no-isolation"', builder)
        self.assertNotIn('"--sdist"', builder)
        self.assertIn('("reconciler", "fence", "publisher")', builder)
        self.assertIn('"media-interlock.artifacts/v1"', builder)
        self.assertIn('"artifacts.json"', builder)
