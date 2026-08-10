"""Versioned local Unix-socket surface for the Fence daemon."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..contracts import ContractError, Envelope, StatusCode, metrics_response, status_response
from .model import PreAdmissionIntent
from .observability import FenceObservability
from .service import FenceService


class FenceDaemon:
    def __init__(self, service: FenceService, observability: FenceObservability, *, readiness: Callable[[], tuple[bool, bool, bool]]) -> None:
        self._service = service
        self._observability = observability
        self._readiness = readiness

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            envelope = Envelope.decode(await reader.readuntil(b"\n"))
            response = self._dispatch(envelope)
            writer.write(response.encode())
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ContractError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def _dispatch(self, envelope: Envelope) -> Envelope:
        if envelope.kind == "acquisition_pre_admission":
            body = envelope.body
            _, _, publisher_ready = self._readiness()
            intent = PreAdmissionIntent(envelope.operation_id, str(body["source"]), str(body["media_id"]), str(body["selector_fingerprint"]), int(body["expected_bytes"]), str(body["watermark"]))
            decision = self._service.pre_admit(intent, publisher_ready=publisher_ready)
            return status_response(envelope.operation_id, StatusCode.OK if decision.admitted else StatusCode.INHIBITED if decision.reason != "conflict" else StatusCode.CONFLICT, decision.reason)
        if envelope.kind == "acquisition_grab_binding":
            bound = self._service.bind_grab(envelope.operation_id, str(envelope.body["download_id"]), str(envelope.body["torrent_hash"]))
            return status_response(envelope.operation_id, StatusCode.OK if bound else StatusCode.INHIBITED, "grab bound" if bound else "grab pending")
        if envelope.kind == "post_pnr_adoption":
            body = envelope.body
            decision = self._service.post_pnr_adopt(
                operation_id=envelope.operation_id, source=str(body["source"]), download_client_id=int(body["download_client_id"]),
                entity_id=str(body["entity_id"]), torrent_hash=str(body["torrent_hash"]), category=str(body["category"]), save_path=str(body["save_path"]),
            )
            if not decision.admitted:
                return status_response(envelope.operation_id, StatusCode.CONFLICT if decision.reason in {"conflict", "identity_drift", "identity_ambiguous"} else StatusCode.INHIBITED, decision.reason)
            receipt = self._service.post_pnr_receipt(envelope.operation_id)
            if receipt is not None:
                return receipt
            return status_response(envelope.operation_id, StatusCode.INHIBITED, decision.reason)
        if envelope.kind == "post_pnr_adoption_query":
            receipt = self._service.post_pnr_receipt(envelope.operation_id)
            return receipt if receipt is not None else status_response(envelope.operation_id, StatusCode.UNAVAILABLE, "post-PNR adoption unavailable")
        if envelope.kind == "post_pnr_historical_adoption":
            body = envelope.body
            decision = self._service.post_pnr_historical_adopt(
                operation_id=envelope.operation_id, source=str(body["source"]), download_client_id=int(body["download_client_id"]),
                entity_ids=tuple(body["entity_ids"]), torrent_hash=str(body["torrent_hash"]), category=str(body["category"]), save_path=str(body["save_path"]),
            )
            if not decision.admitted:
                return status_response(envelope.operation_id, StatusCode.CONFLICT if decision.reason in {"conflict", "identity_drift", "identity_ambiguous"} else StatusCode.INHIBITED, decision.reason)
            receipt = self._service.post_pnr_historical_receipt(envelope.operation_id)
            if receipt is not None:
                return receipt
            return status_response(envelope.operation_id, StatusCode.INHIBITED, decision.reason)
        if envelope.kind == "post_pnr_historical_adoption_query":
            receipt = self._service.post_pnr_historical_receipt(envelope.operation_id)
            return receipt if receipt is not None else status_response(envelope.operation_id, StatusCode.UNAVAILABLE, "historical post-PNR adoption unavailable")
        if envelope.kind == "acquisition_freeze":
            frozen = self._service.freeze(envelope.operation_id)
            return status_response(envelope.operation_id, StatusCode.OK if frozen else StatusCode.INHIBITED, "acquisition frozen" if frozen else "acquisition freeze pending")
        if envelope.kind == "observe":
            terminal = self._service.observe(envelope.operation_id)
            return terminal if terminal is not None else status_response(envelope.operation_id, StatusCode.OK, "no terminal acquisition")
        if envelope.kind == "custody_receipt":
            accepted = self._service.accept_custody(envelope)
            return status_response(envelope.operation_id, StatusCode.OK if accepted else StatusCode.CONFLICT, "custody receipt accepted" if accepted else "custody receipt rejected")
        if envelope.kind == "quiesce":
            changed = self._service.quiesce(enabled=bool(envelope.body["enabled"]))
            return status_response(envelope.operation_id, StatusCode.OK if changed else StatusCode.INHIBITED, "quiescence updated" if changed else "quiescence unresolved")
        if envelope.kind == "status":
            qbittorrent_ready, prowlarr_ready, publisher_ready = self._readiness()
            status = self._observability.status(qbittorrent_ready=qbittorrent_ready, prowlarr_ready=prowlarr_ready, publisher_ready=publisher_ready)
            return status_response(envelope.operation_id, StatusCode.OK if status["status"] == "ready" else StatusCode.INHIBITED, str(status["status"]))
        if envelope.kind == "metrics":
            return metrics_response(envelope.operation_id, self._observability.metrics())
        return status_response(envelope.operation_id, StatusCode.INVALID_CONTRACT, "unsupported Fence message")

    def status(self) -> dict[str, object]:
        qbittorrent_ready, prowlarr_ready, publisher_ready = self._readiness()
        return self._observability.status(qbittorrent_ready=qbittorrent_ready, prowlarr_ready=prowlarr_ready, publisher_ready=publisher_ready)

    def recover(self) -> None:
        self._service.recover()

    def tick(self) -> bool:
        """Run one bounded observer pass without extending the socket contract."""
        try:
            _, _, publisher_ready = self._readiness()
            return self._service.poll_external(publisher_ready=publisher_ready)
        except Exception:
            return False
