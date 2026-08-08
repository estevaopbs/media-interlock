from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path

import _source_tree  # noqa: F401

from media_interlock.contracts import Envelope, StatusCode, status_response
from media_interlock.fence.model import PreAdmissionIntent
from media_interlock.reconciler.fence_client import UnixFenceClient


class UnixFenceClientTests(unittest.TestCase):
    def test_canonical_pre_admission_and_binding_use_the_real_unix_contract(self) -> None:
        seen: list[Envelope] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fence.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(2)

            def serve() -> None:
                for _ in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        raw = bytearray()
                        while not raw.endswith(b"\n"):
                            raw.extend(connection.recv(4096))
                        request = Envelope.decode(bytes(raw))
                        seen.append(request)
                        connection.sendall(status_response(request.operation_id, StatusCode.OK, "accepted").encode())

            thread = threading.Thread(target=serve)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(listener.close)
            client = UnixFenceClient(path)

            self.assertTrue(client.pre_admit(PreAdmissionIntent("12345678-1234-4678-9234-567812345678", "radarr", "42", "a" * 64, 400, "7")).admitted)
            self.assertTrue(client.bind_grab("12345678-1234-4678-9234-567812345678", "A" * 40, "a" * 40))

        self.assertEqual(["acquisition_pre_admission", "acquisition_grab_binding"], [envelope.kind for envelope in seen])
        self.assertEqual({"download_id": "A" * 40, "torrent_hash": "a" * 40}, seen[-1].body)
