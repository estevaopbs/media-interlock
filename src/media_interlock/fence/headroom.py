"""Fail-closed physical free-space arithmetic for Fence-owned liabilities."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path


_MAX_BYTES = 2**63 - 1


@dataclass(frozen=True)
class HeadroomPool:
    name: str
    minimum_free_bytes: int
    safety_margin_bytes: int
    probe_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or (self.probe_path is not None and (not isinstance(self.probe_path, Path) or not self.probe_path.is_absolute())) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_BYTES for value in (self.minimum_free_bytes, self.safety_margin_bytes)):
            raise ValueError("physical headroom pool is invalid")


def statvfs_free_bytes(pool: HeadroomPool) -> int | None:
    """Read one configured probe only; no media-tree traversal is authorized."""
    if pool.probe_path is None:
        return None
    try:
        probe = os.statvfs(pool.probe_path)
        free = probe.f_bavail * probe.f_frsize
    except OSError:
        return None
    return free if isinstance(free, int) and 0 <= free <= _MAX_BYTES else None


class PhysicalHeadroom:
    """Checks future governed allocations against one observed supply per pool."""

    def __init__(self, pools: Mapping[str, HeadroomPool], *, free_bytes: Callable[[HeadroomPool], int | None]) -> None:
        self._pools = dict(pools)
        self._free_bytes = free_bytes

    @staticmethod
    def _add(current: int, value: object) -> int | None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_BYTES - current:
            return None
        return current + value

    def allows(self, records: Iterable[Mapping[str, object]], sources: Mapping[str, tuple[str, ...]]) -> bool:
        liabilities = {name: 0 for name in self._pools}
        for record in records:
            if record.get("state") == "released":
                continue
            source = record.get("source")
            requested = record.get("requested_bytes")
            if not isinstance(source, str) or source not in sources or not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
                return False
            remaining = requested if record.get("remaining_download_bytes") is None else record.get("remaining_download_bytes")
            pools = sources[source]
            if not 1 <= len(pools) <= 3:
                return False
            for index, pool_name in enumerate(pools):
                liability = remaining if index == 0 else requested
                if pool_name not in liabilities:
                    return False
                updated = self._add(liabilities[pool_name], liability)
                if updated is None:
                    return False
                liabilities[pool_name] = updated
        for name, liability in liabilities.items():
            pool = self._pools[name]
            required = self._add(pool.minimum_free_bytes, pool.safety_margin_bytes)
            required = None if required is None else self._add(required, liability)
            try:
                observed = self._free_bytes(pool)
            except Exception:
                return False
            if required is None or not isinstance(observed, int) or isinstance(observed, bool) or observed < 0 or observed > _MAX_BYTES or observed < required:
                return False
        return True
