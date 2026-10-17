# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

import unittest

from searx.mesh.node import NodeProfile
from searx.mesh.routing import aggregate, select


def _node(node_id, *, latency=0.0, capabilities=()):
    node = NodeProfile(node_id=node_id, host="h", port=1, capabilities=frozenset(capabilities))
    node.latency_ema = latency
    return node


class SelectTestCase(unittest.TestCase):

    def test_latency_prefers_fastest_then_untried(self):
        nodes = [_node("slow", latency=5.0), _node("fast", latency=1.0), _node("new")]
        chosen = select(nodes, "latency", 3)
        self.assertEqual([node.node_id for node in chosen], ["new", "fast", "slow"])

    def test_fanout_caps_the_selection(self):
        nodes = [_node("a", latency=1.0), _node("b", latency=2.0), _node("c", latency=3.0)]
        self.assertEqual([node.node_id for node in select(nodes, "latency", 2)], ["a", "b"])

    def test_round_robin_rotates_by_offset(self):
        nodes = [_node("a"), _node("b"), _node("c")]
        self.assertEqual([node.node_id for node in select(nodes, "round_robin", 3, offset=1)], ["b", "c", "a"])
        self.assertEqual([node.node_id for node in select(nodes, "round_robin", 2, offset=4)], ["b", "c"])

    def test_capability_puts_advertisers_first(self):
        nodes = [_node("plain"), _node("search", capabilities=["search"])]
        chosen = select(nodes, "capability", 2, required_capabilities=["search"])
        self.assertEqual([node.node_id for node in chosen], ["search", "plain"])

    def test_unknown_strategy_falls_back_to_latency(self):
        nodes = [_node("slow", latency=5.0), _node("fast", latency=1.0)]
        chosen = select(nodes, "bogus", 1)
        self.assertEqual([node.node_id for node in chosen], ["fast"])

    def test_invalid_fanout_defaults_to_one(self):
        self.assertEqual(len(select([_node("a"), _node("b")], "latency", 0)), 1)

    def test_fail_safe_on_garbage(self):
        self.assertEqual(select(None, "latency", 3), [])  # type: ignore[arg-type]


class AggregateTestCase(unittest.TestCase):

    def test_interleaves_and_dedups_by_normalized_url(self):
        responses = [
            {
                "node_id": "A",
                "results": [{"url": "https://x.com/a/", "title": "a1"}, {"url": "https://x.com/b", "title": "b"}],
            },
            {
                "node_id": "B",
                "results": [{"url": "https://X.com/a", "title": "a2"}, {"url": "https://x.com/c", "title": "c"}],
            },
        ]
        merged = aggregate(responses)
        urls = [item["url"] for item in merged["results"]]
        self.assertEqual(len(urls), 3)
        self.assertEqual(merged["peers"], ["A", "B"])
        self.assertEqual(merged["count"], 3)

    def test_dedup_records_all_sources(self):
        responses = [
            {"node_id": "A", "results": [{"url": "https://x.com/a", "title": "a"}]},
            {"node_id": "B", "results": [{"url": "https://x.com/a/", "title": "a"}]},
        ]
        merged = aggregate(responses)
        self.assertEqual(len(merged["results"]), 1)
        self.assertEqual(sorted(merged["results"][0]["sources"]), ["A", "B"])

    def test_max_results_limits_output(self):
        responses = [
            {"node_id": "A", "results": [{"url": "https://x.com/a"}, {"url": "https://x.com/b"}]},
            {"node_id": "B", "results": [{"url": "https://x.com/c"}]},
        ]
        merged = aggregate(responses, max_results=2)
        self.assertEqual(merged["count"], 2)

    def test_failsafe_on_garbage(self):
        self.assertEqual(aggregate(None), {"results": [], "count": 0, "peers": []})  # type: ignore[arg-type]
        self.assertEqual(
            aggregate([{"results": "not-a-list"}, "nope"]),  # type: ignore[arg-type, list-item]
            {"results": [], "count": 0, "peers": []},
        )
