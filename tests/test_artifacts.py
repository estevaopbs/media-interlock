from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import _source_tree  # noqa: F401

from media_interlock import __version__


ROOT = Path(__file__).resolve().parents[1]


def _artifact_builder() -> object:
    specification = importlib.util.spec_from_file_location("build_artifacts", ROOT / "scripts" / "build-artifacts.py")
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ArtifactDefinitionTests(unittest.TestCase):
    def test_artifact_manifest_uses_its_oci_archive_manifest_digest(self) -> None:
        archive = ROOT / ".test-artifact.oci.tar"
        payload = json.dumps({"manifests": [{"digest": "sha256:" + "a" * 64}]}).encode("utf-8")
        try:
            with tarfile.open(archive, "w") as written:
                entry = tarfile.TarInfo("index.json")
                entry.size = len(payload)
                written.addfile(entry, io.BytesIO(payload))

            builder = _artifact_builder()
            self.assertEqual("sha256:" + "a" * 64, builder._oci_archive_manifest_digest(archive))
        finally:
            archive.unlink(missing_ok=True)

    def test_corrective_release_version_is_consistent(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("0.1.9", __version__)
        self.assertEqual(__version__, project["project"]["version"])

    def test_packaged_readme_declares_the_current_immutable_release(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = (ROOT / project["project"]["readme"]).read_text(encoding="utf-8")
        state = (ROOT / "docs" / "current" / "state.md").read_text(encoding="utf-8")

        self.assertIn("MediaInterlock 0.1.9 is the current immutable public", readme)
        self.assertIn("Version 0.1.8 remains preserved as its immutable predecessor.", readme)
        self.assertIn("MediaInterlock 0.1.9 is the immutable public downstream-consumption release.", state)
        self.assertIn("Version 0.1.8 remains preserved", state)
        self.assertNotIn("local corrective candidate", readme.lower())
        self.assertNotIn("not been tagged, pushed, or published", readme.lower())

    def test_artifact_builder_rejects_a_runtime_outside_the_python_profile(self) -> None:
        builder = _artifact_builder()
        with mock.patch.object(builder, "_output", return_value="Python 3.14.6"):
            with self.assertRaisesRegex(RuntimeError, "expected Python 3.14.7"):
                builder._validated_runtime_python_version("podman", "fixture:local", ROOT)
        with mock.patch.object(builder, "_output", return_value="Python 3.14.7"):
            self.assertEqual(
                "3.14.7",
                builder._validated_runtime_python_version("podman", "fixture:local", ROOT),
            )

    def test_oci_targets_are_arbitrary_uid_safe_and_execute_only_declared_components(self) -> None:
        containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")

        self.assertEqual(
            2,
            containerfile.count(
                "FROM docker.io/library/python@sha256:"
                "b5998102f95c4b44edf1e7cb5cecbe1f49e0bf054f345c1db5b854e166e6e17a"
            ),
        )
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
        self.assertIn('"runtime_python_version"', builder)
        self.assertIn('"artifacts.json"', builder)
