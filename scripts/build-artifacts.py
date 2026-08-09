#!/usr/bin/env python3
"""Build local wheel and OCI archives without network publication."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _output(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(command, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--oci-engine", choices=("podman",), default="podman")
    parser.add_argument("--source-date-epoch", type=int, default=0)
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if arguments.source_date_epoch < 0:
        parser.error("--source-date-epoch must be non-negative")
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
    for component in ("fence", "publisher"):
        tag = f"media-interlock-{component}:local"
        _run([arguments.oci_engine, "build", "--build-arg", f"SOURCE_DATE_EPOCH={arguments.source_date_epoch}", "--target", component, "--tag", tag, "--file", "Containerfile", "."], cwd=root)
        _run([arguments.oci_engine, "image", "save", "--format", "oci-archive", "--output", str(output / f"media-interlock-{component}.oci.tar"), tag], cwd=root)
        (output / f"media-interlock-{component}.manifest-digest").write_text(
            _output([arguments.oci_engine, "image", "inspect", "--format", "{{.Digest}}", tag], cwd=root) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
