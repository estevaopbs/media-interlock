#!/usr/bin/env python3
"""Build local wheel and OCI archives without network publication."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--oci-engine", choices=("podman",), default="podman")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = arguments.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run([sys.executable, "-m", "build", "--outdir", str(output)], cwd=root)
    for component in ("fence", "publisher"):
        tag = f"media-interlock-{component}:local"
        _run([arguments.oci_engine, "build", "--target", component, "--tag", tag, "--file", "Containerfile", "."], cwd=root)
        _run([arguments.oci_engine, "image", "save", "--format", "oci-archive", "--output", str(output / f"media-interlock-{component}.oci.tar"), tag], cwd=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
