# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

import unittest

from searx.mesh.node import NodeProfile, NodeRegistry, parse_seed


class NodeProfileTestCase(unittest.TestCase):

    def test_never_contacted_is_optimistically_healthy(self):
        node = NodeProfile(node_id="a", host="h", port=1)
        self.assertTrue(node.is_healthy(now=1000.0))

    def test_healthy_until_failure_budget_exhausted(self):
        node = NodeProfile(node_id="a", host="h", port=1)
        for _ in range(3):
            node.observe_failure(now=1000.0)
        self.assertFalse(node.is_healthy(now=1000.0, max_failures=3))

    def test_stale_node_expires(self):
        node = NodeProfile(node_id="a", host="h", port=1)
        node.observe_success(1.0, now=1000.0)
        self.assertTrue(node.is_healthy(now=1100.0, ttl=300.0))
        self.assertFalse(node.is_healthy(now=1400.0, ttl=300.0))

    def test_success_updates_latency_and_clears_failures(self):
        node = NodeProfile(node_id="a", host="h", port=1)
        node.observe_failure(now=1.0)
        node.observe_failure(now=2.0)
        self.assertEqual(node.failures, 2)
        node.observe_success(0.5, now=3.0)
        self.assertEqual(node.failures, 0)
        self.assertEqual(node.latency_ema, 0.5)
        self.assertEqual(node.last_seen, 3.0)

    def test_latency_ema_is_smoothed(self):
        node = NodeProfile(node_id="a", host="h", port=1)
        node.observe_success(1.0, now=1.0)
        node.observe_success(5.0, now=2.0)
        self.assertAlmostEqual(node.latency_ema, 0.3 * 5.0 + 0.7 * 1.0)

    def test_endpoint_tuple(self):
        self.assertEqual(NodeProfile(node_id="a", host="h", port=9).endpoint, ("h", 9))


class ParseSeedTestCase(unittest.TestCase):

    def test_host_port_string(self):
        node = parse_seed("example.org:8000")
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.endpoint, ("example.org", 8000))
        self.assertEqual(len(node.node_id), 16)

    def test_default_port(self):
        node = parse_seed("example.org")
        assert node is not None
        self.assertEqual(node.port, 9876)

    def test_ipv6_literal(self):
        node = parse_seed("[::1]:9000")
        assert node is not None
        self.assertEqual(node.endpoint, ("::1", 9000))

    def test_mapping_entry(self):
        node = parse_seed({"host": "h", "port": "8080", "node_id": "n", "capabilities": ["search", "x"]})
        assert node is not None
        self.assertEqual(node.endpoint, ("h", 8080))
        self.assertEqual(node.node_id, "n")
        self.assertEqual(node.capabilities, frozenset({"search", "x"}))

    def test_invalid_entries(self):
        self.assertIsNone(parse_seed({"host": ""}))
        self.assertIsNone(parse_seed("   "))
        self.assertIsNone(parse_seed(123))

    def test_derived_id_is_stable(self):
        first = parse_seed("h:1")
        second = parse_seed("h:1")
        assert first is not None and second is not None
        self.assertEqual(first.node_id, second.node_id)


class NodeRegistryTestCase(unittest.TestCase):

    def test_upsert_and_list(self):
        reg = NodeRegistry()
        reg.upsert(NodeProfile(node_id="a", host="h1", port=1))
        reg.upsert(NodeProfile(node_id="b", host="h2", port=2))
        self.assertEqual(len(reg.all()), 2)
        self.assertEqual(len(reg.healthy(now=1000.0)), 2)

    def test_upsert_merges_and_preserves_observations(self):
        reg = NodeRegistry()
        known = NodeProfile(node_id="a", host="h1", port=1)
        reg.upsert(known)
        known.observe_success(2.0, now=5.0)
        reg.upsert(NodeProfile(node_id="a", host="h2", port=2))
        merged = reg.all()[0]
        self.assertEqual(merged.host, "h2")
        self.assertEqual(merged.port, 2)
        self.assertEqual(merged.last_seen, 5.0)

    def test_upsert_rejects_empty_id(self):
        reg = NodeRegistry()
        self.assertIsNone(reg.upsert(NodeProfile(node_id="", host="h", port=1)))

    def test_add_seeds(self):
        reg = NodeRegistry()
        added = reg.add_seeds(["h1:1", {"host": "h2", "port": 2}, "   "])
        self.assertEqual(added, 2)
        self.assertEqual(len(reg.all()), 2)

    def test_healthy_filters_failed_nodes(self):
        reg = NodeRegistry(max_failures=1)
        node = NodeProfile(node_id="a", host="h", port=1)
        reg.upsert(node)
        node.observe_failure(now=1.0)
        self.assertEqual(reg.healthy(now=1.0), [])
