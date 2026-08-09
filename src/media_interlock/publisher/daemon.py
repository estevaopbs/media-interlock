"""Versioned local Unix-socket surface for Publisher."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..contracts import ContractError, Envelope, StatusCode, metrics_response, publisher_operation_status, status_response
from .observability import PublisherObservability
from .service import PublisherService


class PublisherDaemon:
    def __init__(self, service: PublisherService, observability: PublisherObservability, *, readiness: Callable[[], bool], process: Callable[[str], bool | None] | None = None, intake: Callable[[Envelope], bool] | None = None, retry: Callable[[], None] | None = None) -> None:
        self._service = service
        self._observability = observability
        self._readiness = readiness
        self._process = process
        self._intake = intake
        self._retry = retry

    def retry_once(self) -> None:
        if self._retry is not None and self._readiness():
            self._retry()

    async def retry_loop(self, interval_seconds: float = 1.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("Publisher retry interval must be positive")
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.retry_once()
            except (ContractError, KeyError, OSError, RuntimeError, ValueError):
                # Existing pending state is retained for the next bounded poll.
                continue

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            envelope = Envelope.decode(await reader.readuntil(b"\n"))
            writer.write(self._dispatch(envelope).encode())
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ContractError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # A caller may lose the response after the durable transition.
                pass

    def _dispatch(self, envelope: Envelope) -> Envelope:
        if envelope.kind == "publisher_operation_query":
            return self._service.operation_response(envelope.operation_id)
        if envelope.kind in {"publisher_bootstrap", "publisher_assisted_intent", "publisher_assisted_complete"}:
            if not self._readiness():
                return publisher_operation_status(envelope.operation_id, "unavailable")
            try:
                accepted = self._intake is not None and self._intake(envelope)
            except (ContractError, OSError, RuntimeError, ValueError):
                return self._record_conflict(envelope.operation_id)
            if not accepted:
                return self._record_conflict(envelope.operation_id)
            current = self._service.operation_response(envelope.operation_id)
            if current.kind == "publisher_operation_status" and current.body["state"] == "conflict":
                return current
            if envelope.kind != "publisher_assisted_intent" and self._process is not None:
                try:
                    self._process(envelope.operation_id)
                except (ContractError, KeyError, OSError, RuntimeError, ValueError):
                    pass
            return self._service.operation_response(envelope.operation_id)
        if envelope.kind == "terminal_acquisition":
            if not self._readiness():
                return status_response(envelope.operation_id, StatusCode.INHIBITED, "Publisher is not ready")
            try:
                receipt = self._service.accept_terminal(envelope)
                if self._process is not None:
                    completed = self._process(envelope.operation_id)
                    if completed is False:
                        return status_response(envelope.operation_id, StatusCode.INHIBITED, "publisher adoption pending")
                return receipt
            except (ContractError, OSError):
                return status_response(envelope.operation_id, StatusCode.CONFLICT, "terminal acquisition rejected")
        if envelope.kind == "status":
            status = self._observability.status(ready=self._readiness())
            return status_response(envelope.operation_id, StatusCode.OK if status["status"] == "ready" else StatusCode.INHIBITED, str(status["status"]))
        if envelope.kind == "metrics":
            return metrics_response(envelope.operation_id, self._observability.metrics())
        return status_response(envelope.operation_id, StatusCode.INVALID_CONTRACT, "unsupported Publisher message")

    def _record_conflict(self, operation_id: str) -> Envelope:
        try:
            recorded = self._service.record_operation_conflict(operation_id)
        except (ContractError, OSError, RuntimeError, ValueError):
            return publisher_operation_status(operation_id, "unavailable")
        return self._service.operation_response(operation_id) if recorded else publisher_operation_status(operation_id, "unavailable")
