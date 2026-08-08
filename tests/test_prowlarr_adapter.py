from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _source_tree  # noqa: F401

from media_interlock.adapters.prowlarr import ProwlarrAdapter
from media_interlock.config import SecretReference


class ProwlarrAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = b"[]"
        self.indexers = b'[{"enable":true}]'
        self.header: str | None = None
        self.redirect_health = False
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.paths.append(self.path)
                outer.header = self.headers.get("X-Api-Key")
                if outer.redirect_health and self.path == "/api/v1/health":
                    self.send_response(302)
                    self.send_header("Location", "/api/v1/indexer")
                    self.end_headers()
                    return
                if self.path == "/api/v1/indexer":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(outer.indexers)
                    return
                if self.path != "/api/v1/health":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer.payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def adapter(self) -> ProwlarrAdapter:
        host, port = self.server.server_address
        return ProwlarrAdapter(f"http://{host}:{port}", SecretReference("env", "PROWLARR_KEY"), secret_resolver=lambda _: "fixture-key")

    def test_healthy_enabled_indexer_is_ready_with_redacted_api_header(self) -> None:
        self.assertTrue(self.adapter().ready())
        self.assertEqual("fixture-key", self.header)

    def test_error_or_ambiguous_health_response_is_unready(self) -> None:
        self.payload = b'[{"type":"error"}]'
        self.assertFalse(self.adapter().ready())
        self.payload = b'[{"message":"missing type"}]'
        self.assertFalse(self.adapter().ready())
        self.payload = b"[]"
        self.indexers = b"[]"
        self.assertFalse(self.adapter().ready())
        self.indexers = b'[{"enable":true}]'
        self.payload = b'[{"type":"warning","message":"indexer unavailable"}]'
        self.assertFalse(self.adapter().ready())

    def test_authenticated_redirect_is_not_followed(self) -> None:
        self.redirect_health = True
        self.assertFalse(self.adapter().ready())
        self.assertEqual(["/api/v1/health"], self.paths)
