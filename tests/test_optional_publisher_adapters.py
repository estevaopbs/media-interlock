from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _source_tree  # noqa: F401

from media_interlock.adapters.bazarr import BazarrAdapter
from media_interlock.adapters.seerr import SeerrAdapter
from media_interlock.config import SecretReference


class OptionalPublisherAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bazarr_payload: bytes = b'{"data":{"bazarr_version":"1.6.0"}}'
        self.seerr_payload: bytes = b'{"applicationTitle":"Seerr"}'
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append((self.path, self.headers.get("X-Api-Key")))
                if self.path == "/api/system/status":
                    payload = outer.bazarr_payload
                elif self.path == "/api/v1/settings/main":
                    payload = outer.seerr_payload
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def _adapter(self, type_: type[BazarrAdapter] | type[SeerrAdapter]):
        host, port = self.server.server_address
        return type_(
            f"http://{host}:{port}",
            SecretReference("env", "OPTIONAL_KEY"),
            secret_resolver=lambda _: "fixture-key",
        )

    def test_bazarr_and_seerr_readiness_use_their_narrow_authenticated_public_endpoints(self) -> None:
        self.assertTrue(self._adapter(BazarrAdapter).ready())
        self.assertTrue(self._adapter(SeerrAdapter).ready())

        self.assertEqual(
            [("/api/system/status", "fixture-key"), ("/api/v1/settings/main", "fixture-key")],
            self.requests,
        )

    def test_invalid_or_ambiguous_payload_is_unready(self) -> None:
        self.bazarr_payload = b'{"data":{"bazarr_version":"1.5.9"}}'
        self.assertFalse(self._adapter(BazarrAdapter).ready())
        self.seerr_payload = b'{}'
        self.assertFalse(self._adapter(SeerrAdapter).ready())
