"""Synchronous bounded Unix client for the Fence's public contract."""

from __future__ import annotations

import socket
from pathlib import Path

from ..contracts import Envelope, StatusCode, acquisition_grab_binding, acquisition_pre_admission
from ..fence.model import AdmissionDecision


class UnixFenceClient:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 2.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout_seconds

    def _call(self, request: Envelope) -> Envelope | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(str(self._socket_path))
                client.sendall(request.encode())
                frame = bytearray()
                while len(frame) < 64 * 1024:
                    part = client.recv(4096)
                    if not part:
                        break
                    frame.extend(part)
                    if frame.endswith(b"\n"):
                        break
        except OSError:
            return None
        try:
            response = Envelope.decode(bytes(frame))
        except ValueError:
            return None
        return response if response.operation_id == request.operation_id else None

    def pre_admit(self, operation_id: str, source: str, media_id: str, selector_fingerprint: str, expected_bytes: int, watermark: str) -> AdmissionDecision:
        response = self._call(acquisition_pre_admission(operation_id=operation_id, source=source, media_id=media_id, selector_fingerprint=selector_fingerprint, expected_bytes=expected_bytes, watermark=watermark))
        if response is None or response.kind != "status":
            return AdmissionDecision(False, "fence_unavailable")
        code, message = response.body.get("code"), response.body.get("message")
        if not isinstance(message, str):
            return AdmissionDecision(False, "fence_invalid_response")
        return AdmissionDecision(code == StatusCode.OK.value, message)

    def bind_grab(self, operation_id: str, download_id: str) -> bool:
        response = self._call(acquisition_grab_binding(operation_id=operation_id, download_id=download_id))
        return response is not None and response.kind == "status" and response.body.get("code") == StatusCode.OK.value
