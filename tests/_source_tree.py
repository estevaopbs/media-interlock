"""Make the uninstalled source tree importable for standard-library test runs."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
