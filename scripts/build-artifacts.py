#!/usr/bin/env python3
"""Build local wheel and OCI archives without network publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
from pathlib import Path


EXPECTED_RUNTIME_PYTHON_VERSION = "3.14.7"


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _output(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def _source_identity(root: Path) -> tuple[str, str]:
    if _output(["git", "status", "--porcelain"], cwd=root):
        raise ValueError("artifact builds require a clean checkout")
    revision = _output(["git", "rev-parse", "HEAD"], cwd=root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("project version is unavailable")
    return revision, version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _oci_archive_manifest_digest(archive: Path) -> str:
    """Return the single OCI manifest identity carried by an archive."""
    try:
        with tarfile.open(archive, "r") as contents:
            index = contents.extractfile("index.json")
            if index is None:
                raise ValueError("OCI archive has no index.json")
            payload = json.load(index)
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OCI archive index cannot be read") from exc
    manifests = payload.get("manifests") if isinstance(payload, dict) else None
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("OCI archive must contain exactly one manifest")
    digest = manifests[0].get("digest") if isinstance(manifests[0], dict) else None
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("OCI archive manifest digest is invalid")
    return digest


def _validated_runtime_python_version(oci_engine: str, tag: str, root: Path) -> str:
    rendered = _output(
        [oci_engine, "run", "--rm", "--entrypoint", "python", tag, "--version"],
        cwd=root,
    )
    expected = f"Python {EXPECTED_RUNTIME_PYTHON_VERSION}"
    if rendered != expected:
        raise RuntimeError(f"image {tag} uses {rendered!r}; expected {expected}")
    return EXPECTED_RUNTIME_PYTHON_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--oci-engine", choices=("podman",), default="podman")
    parser.add_argument("--source-date-epoch", type=int, default=0)
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    if arguments.source_date_epoch < 0:
        parser.error("--source-date-epoch must be non-negative")
    try:
        revision, version = _source_identity(root)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-interlock-build-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / "bin" / "python"
        _run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
            "--require-hashes", "--no-deps", "--requirement", str(root / "build-requirements.txt"),
        ], cwd=root)
        old_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = str(arguments.source_date_epoch)
        try:
            _run([str(python), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(output)], cwd=root)
        finally:
            if old_epoch is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old_epoch
    wheels = tuple(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("artifact build did not produce exactly one wheel")
    images: list[dict[str, str]] = []
    for component in ("media-interlock",):
        tag = f"{component}:local"
        _run([
            arguments.oci_engine, "build",
            "--build-arg", f"SOURCE_DATE_EPOCH={arguments.source_date_epoch}",
            "--build-arg", f"SOURCE_REVISION={revision}",
            "--build-arg", f"PACKAGE_VERSION={version}",
            "--target", component.replace("-", "_"), "--tag", tag, "--file", "Containerfile", ".",
        ], cwd=root)
        _validated_runtime_python_version(arguments.oci_engine, tag, root)
        archive = output / f"media-interlock-{component}.oci.tar"
        _run([arguments.oci_engine, "image", "save", "--format", "oci-archive", "--output", str(archive), tag], cwd=root)
        digest = _oci_archive_manifest_digest(archive)
        (output / f"media-interlock-{component}.manifest-digest").write_text(
            digest + "\n",
            encoding="utf-8",
        )
        images.append({"component": component, "archive": f"media-interlock-{component}.oci.tar", "manifest_digest": digest})
    (output / "artifacts.json").write_text(
        json.dumps(
            {
                "schema": "media-interlock.artifacts/v1",
                "source_revision": revision,
                "version": version,
                "runtime_python_version": EXPECTED_RUNTIME_PYTHON_VERSION,
                "wheel": {"filename": wheels[0].name, "sha256": _sha256(wheels[0])},
                "images": images,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
