# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import socket
import time
import unittest

from searx.mesh import protocol
from searx.mesh.transport import MeshServer, Peer, send_hello, send_query

KEY = b"0123456789abcdef0123456789abcdef"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _echo_handler(query, opts):  # pylint: disable=unused-argument
    return {"results": [{"title": "T", "url": "https://example.com/a", "content": "c", "engine": "general"}]}


class ProtocolTestCase(unittest.TestCase):

    def test_canonical_bytes_ignores_sig_and_order(self):
        first = protocol.canonical_bytes({"b": 1, "a": 2})
        second = protocol.canonical_bytes({"a": 2, "b": 1, "sig": "deadbeef"})
        self.assertEqual(first, second)

    def test_sign_verify_roundtrip(self):
        signature = protocol.sign(b"payload", KEY)
        self.assertTrue(protocol.verify(b"payload", signature, KEY))

    def test_verify_rejects_tamper_and_wrong_key(self):
        signature = protocol.sign(b"payload", KEY)
        self.assertFalse(protocol.verify(b"tampered", signature, KEY))
        self.assertFalse(protocol.verify(b"payload", signature, b"x" * 32))

    def test_query_hash_is_stable_and_hides_query(self):
        digest = protocol.query_hash("secret", "en")
        self.assertEqual(digest, protocol.query_hash("secret", "en"))
        self.assertNotIn("secret", digest)
        self.assertNotEqual(digest, protocol.query_hash("secret", "de"))

    def test_sanitize_search_request_normalizes(self):
        clean = protocol.sanitize_search_request({"query": "  hi  ", "safesearch": 5, "pageno": 0, "max_results": 999})
        assert clean is not None
        self.assertEqual(clean["query"], "hi")
        self.assertEqual(clean["safesearch"], 2)
        self.assertEqual(clean["pageno"], 1)
        self.assertEqual(clean["max_results"], 50)

    def test_sanitize_search_request_rejects_empty(self):
        self.assertIsNone(protocol.sanitize_search_request({"query": "   "}))
        self.assertIsNone(protocol.sanitize_search_request("nope"))  # type: ignore[arg-type]

    def test_sanitize_result_requires_url(self):
        self.assertIsNone(protocol.sanitize_result({"title": "no url"}))
        clean = protocol.sanitize_result({"url": "https://e.com", "content": "x" * 5000})
        assert clean is not None
        self.assertEqual(len(clean["content"]), 2000)

    def test_sanitize_search_results_drops_invalid(self):
        clean = protocol.sanitize_search_results(
            [{"url": "https://e.com"}, {"title": "no url"}, "nope"]  # type: ignore[list-item]
        )
        self.assertEqual(len(clean), 1)

    def test_read_write_frame_roundtrip(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        protocol.write_frame(left, b"hello")
        self.assertEqual(protocol.read_frame(right, 100), b"hello")

    def test_read_frame_rejects_oversize(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.sendall(protocol._HEADER.pack(1_000_000))
        with self.assertRaises(ValueError):
            protocol.read_frame(right, 100)

    def test_encode_decode_roundtrip(self):
        message = {"v": protocol.PROTOCOL_VERSION, "kind": "x", "ts": time.time(), "nonce": "n"}
        decoded = protocol.decode_message(protocol.encode_message(message, KEY), KEY)
        self.assertEqual(decoded["kind"], "x")

    def test_decode_rejects_wrong_key_and_stale(self):
        message = {"v": protocol.PROTOCOL_VERSION, "kind": "x", "ts": time.time(), "nonce": "n"}
        frame = protocol.encode_message(message, KEY)
        with self.assertRaises(ValueError):
            protocol.decode_message(frame, b"y" * 32)
        with self.assertRaises(ValueError):
            protocol.decode_message(frame, KEY, now=time.time() + 10_000)


class MeshServerTestCase(unittest.TestCase):

    def _server(self, *, handler=_echo_handler):
        server = MeshServer(
            "127.0.0.1",
            _free_port(),
            KEY,
            "peer-1",
            None,
            allowlist=["127.0.0.1"],
            search_handler=handler,
            capabilities=["search"],
            engines=["general"],
        )
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        return server

    def test_search_request_end_to_end(self):
        server = self._server()
        peer = Peer("peer-1", "127.0.0.1", server._port)
        out = send_query(peer, KEY, "local", "hello", max_results=5, timeout=5.0)
        self.assertEqual(out["node_id"], "peer-1")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["results"][0]["url"], "https://example.com/a")
        self.assertEqual(len(out["query_hash"]), 16)

    def test_search_request_without_handler_errors(self):
        server = self._server(handler=None)
        peer = Peer("peer-1", "127.0.0.1", server._port)
        out = send_query(peer, KEY, "local", "hello", timeout=5.0)
        self.assertEqual(out["error"], "no_handler")
        self.assertEqual(out["results"], [])

    def test_hello_reports_capabilities(self):
        server = self._server()
        peer = Peer("peer-1", "127.0.0.1", server._port)
        out = send_hello(peer, KEY, "local", timeout=5.0)
        self.assertEqual(out["node_id"], "peer-1")
        self.assertEqual(out["capabilities"], ["search"])
        self.assertEqual(out["engines"], ["general"])

    def test_peer_outside_allowlist_is_rejected(self):
        server = MeshServer("127.0.0.1", _free_port(), KEY, "peer-1", None, allowlist=["10.0.0.9"])
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        peer = Peer("peer-1", "127.0.0.1", server._port)
        self.assertEqual(send_query(peer, KEY, "local", "hello", timeout=5.0), {})
