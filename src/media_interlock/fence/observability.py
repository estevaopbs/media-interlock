"""Bounded Fence health and Prometheus text metrics."""

from __future__ import annotations

from .model import FenceState, ReservationState


class FenceObservability:
    def __init__(self, state: FenceState) -> None:
        self._state = state

    def status(self, *, qbittorrent_ready: bool, prowlarr_ready: bool, publisher_ready: bool) -> dict[str, object]:
        unresolved = any(record["state"] in {ReservationState.TAG_INTENT_RECORDED.value, ReservationState.RESUME_INTENT_RECORDED.value} for record in self._state.records())
        status = "ready" if qbittorrent_ready and prowlarr_ready and publisher_ready and self._state.within_capacity and not unresolved else "inhibited"
        return {"version": "v1", "status": status, "reserved_bytes": self._state.reserved_bytes, "inflight": self._inflight()}

    def metrics(self) -> str:
        return f"media_interlock_fence_reserved_bytes {self._state.reserved_bytes}\nmedia_interlock_fence_inflight {self._inflight()}\n"

    def _inflight(self) -> int:
        return sum(record["state"] != ReservationState.RELEASED.value for record in self._state.records())
