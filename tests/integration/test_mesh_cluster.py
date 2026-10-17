# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import socket
import unittest

from searx.mesh.node import NodeProfile, NodeRegistry
from searx.mesh.transport import MeshServer, Peer, send_hello, send_query, send_sync

KEY = b"fedcba9876543210fedcba9876543210"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _search_handler_node_a(query: str, opts: dict) -> dict:  # pylint: disable=unused-argument
    return {
        "results": [
            {
                "title": "Node A Result",
                "url": "https://node-a.local/result",
                "content": "Content from node A",
                "engine": "general",
            }
        ],
        "count": 1,
    }


def _search_handler_node_b(query: str, opts: dict) -> dict:  # pylint: disable=unused-argument
    return {
        "results": [
            {
                "title": "Node B Result",
                "url": "https://node-b.local/result",
                "content": "Content from node B",
                "engine": "general",
            }
        ],
        "count": 1,
    }


class MeshClusterIntegrationTestCase(unittest.TestCase):

    def setUp(self):
        self.port_a = _free_port()
        self.port_b = _free_port()

        self.server_a = MeshServer(
            "127.0.0.1",
            self.port_a,
            KEY,
            "node-alpha",
            lambda: [{"s": "node-alpha", "r": "links_to", "t": "node-beta", "w": 0.9}],
            allowlist=["127.0.0.1"],
            search_handler=_search_handler_node_a,
            capabilities=["search"],
            engines=["general"],
        )

        self.server_b = MeshServer(
            "127.0.0.1",
            self.port_b,
            KEY,
            "node-beta",
            lambda: [{"s": "node-beta", "r": "links_to", "t": "node-alpha", "w": 0.8}],
            allowlist=["127.0.0.1"],
            search_handler=_search_handler_node_b,
            capabilities=["search"],
            engines=["general"],
        )

        self.assertTrue(self.server_a.start())
        self.assertTrue(self.server_b.start())
        self.addCleanup(self.server_a.stop)
        self.addCleanup(self.server_b.stop)

    def test_two_node_hello_exchange(self):
        peer_b = Peer("node-beta", "127.0.0.1", self.port_b)
        hello = send_hello(peer_b, KEY, "node-alpha", capabilities=["search"], engines=["general"], timeout=3.0)
        self.assertEqual(hello["node_id"], "node-beta")
        self.assertIn("search", hello["capabilities"])
        self.assertIn("general", hello["engines"])

    def test_two_node_sync_exchange(self):
        peer_b = Peer("node-beta", "127.0.0.1", self.port_b)
        fragments = send_sync(
            peer_b,
            KEY,
            "node-alpha",
            [{"s": "node-alpha", "r": "related", "t": "node-beta", "w": 0.95}],
            timeout=3.0,
        )
        self.assertIsInstance(fragments, list)
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]["s"], "node-beta")

    def test_federated_distributed_search(self):
        peer_a = Peer("node-alpha", "127.0.0.1", self.port_a)
        res = send_query(peer_a, KEY, "node-beta", "distributed search query", timeout=3.0)
        self.assertEqual(res["node_id"], "node-alpha")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["url"], "https://node-a.local/result")
        self.assertIn("query_hash", res)
