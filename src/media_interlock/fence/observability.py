"""Bounded Fence health and Prometheus text metrics."""

from __future__ import annotations

from collections.abc import Callable

from .model import FenceState, ReservationState


class FenceObservability:
    def __init__(self, state: FenceState, *, lease_probe: Callable[[], tuple[bool, int | None, int | None]] | None = None) -> None:
        self._state = state
        self._lease_probe = lease_probe

    def status(self, *, qbittorrent_ready: bool, prowlarr_ready: bool, publisher_ready: bool) -> dict[str, object]:
        unresolved = any(record["state"] in {ReservationState.TAG_INTENT_RECORDED.value, ReservationState.RESUME_INTENT_RECORDED.value} for record in self._state.records())
        quiescence_unresolved = sum(record["state"] == ReservationState.PAUSE_INTENT_RECORDED.value for record in self._state.records())
        status = "ready" if qbittorrent_ready and prowlarr_ready and publisher_ready and self._state.within_capacity and not unresolved and not self._state.quiescing else "inhibited"
        return {"version": "v1", "status": status, "reserved_bytes": self._state.reserved_bytes, "inflight": self._inflight(), "quiescing": self._state.quiescing, "quiescence_unresolved": quiescence_unresolved}

    def metrics(self) -> str:
        unresolved = sum(record["state"] == ReservationState.PAUSE_INTENT_RECORDED.value for record in self._state.records())
        lines = [
            f"media_interlock_fence_reserved_bytes {self._state.reserved_bytes}",
            f"media_interlock_fence_inflight {self._inflight()}",
            f"media_interlock_fence_quiescence_unresolved {unresolved}",
        ]
        if self._lease_probe is not None:
            try:
                available, device, inode = self._lease_probe()
            except Exception:
                available, device, inode = False, None, None
            lines.append(f"media_interlock_fence_shared_mutation_lease_available {1 if available is True else 0}")
            if available is True and isinstance(device, int) and not isinstance(device, bool) and device >= 0 and isinstance(inode, int) and not isinstance(inode, bool) and inode >= 0:
                lines.extend((f"media_interlock_fence_shared_mutation_lease_device {device}", f"media_interlock_fence_shared_mutation_lease_inode {inode}"))
        return "\n".join(lines) + "\n"

    def _inflight(self) -> int:
        return sum(record["state"] != ReservationState.RELEASED.value for record in self._state.records())
