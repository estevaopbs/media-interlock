"""Bounded Publisher health and metrics."""

from __future__ import annotations

from .model import PublisherState


class PublisherObservability:
    def __init__(self, state: PublisherState) -> None:
        self._state = state

    def status(self, *, ready: bool) -> dict[str, object]:
        return {"version": "v1", "status": "ready" if ready else "inhibited", "publications": len(self._state.records())}

    def metrics(self) -> str:
        return f"media_interlock_publisher_publications {len(self._state.records())}\n"
