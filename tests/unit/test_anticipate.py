# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name,protected-access

import os
import tempfile
import time
import unittest

from searx import anticipate, nextgen_bridge, settings as _settings
from searx.anticipate import predictor
from searx.anticipate.store import AnticipateStore, get_store, reset_store
from searx.metrics import nextgen
from searx.metrics.models import CounterStorage, HistogramStorage


def _nextgen_settings(**anticipate_overrides):
    anticipate_cfg = {
        "enabled": True,
        "session_window": 900,
        "max_rows": 5000,
        "ttl": 1800,
        "top_k_predictions": 2,
        "reward_boost": 2.0,
        "weight_decay": 0.5,
        "decay_after": 86400,
        "min_weight": 0.05,
        "db_path": "",
    }
    anticipate_cfg.update(anticipate_overrides)
    return {"search": {"nextgen": {"enabled": True, "anticipate": anticipate_cfg}}}


class AnticipateTestCase(unittest.TestCase):

    def setUp(self):
        self._saved_settings = dict(_settings)
        self._tmpdir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = os.path.join(self._tmpdir.name, "anticipate.db")
        _settings.clear()
        _settings.update(_nextgen_settings(db_path=self.db_path))
        self.addCleanup(self._restore)

    def _restore(self):
        anticipate.reset()
        reset_store()
        predictor.reset()
        _settings.clear()
        _settings.update(self._saved_settings)

    def new_store(self, **kwargs):
        store = AnticipateStore(self.db_path, **kwargs)
        self.addCleanup(store.close)
        return store

    def _get_store(self) -> AnticipateStore:
        store = get_store()
        assert store is not None
        return store


class StoreTestCase(AnticipateTestCase):

    def test_record_and_predict_normalizes(self):
        store = self.new_store()
        self.assertTrue(store.record_transition("  Alpha  Beta ", "Gamma"))
        self.assertEqual(store.predict("alpha beta"), ["gamma"])
        self.assertFalse(store.record_transition("alpha beta", "alpha beta"))

    def test_records_persist_across_instances(self):
        self.assertTrue(self.new_store().record_transition("a", "b"))
        self.assertEqual(self.new_store().predict("a"), ["b"])

    def test_artifact_roundtrip_and_absent(self):
        store = self.new_store()
        self.assertTrue(store.store_artifact("q", [1.0, 2.0, 3.0], "ctx"))
        self.assertEqual(store.get_artifact("q"), ([1.0, 2.0, 3.0], "ctx"))
        self.assertIsNone(store.get_artifact("missing"))

    def test_record_transition_rewards_with_delta(self):
        store = self.new_store()
        self.assertTrue(store.record_transition("a", "b", delta=2.5))
        self.assertAlmostEqual(store.weight("a", "b") or 0.0, 2.5, places=5)
        self.assertTrue(store.record_transition("a", "b"))
        self.assertAlmostEqual(store.weight("a", "b") or 0.0, 3.5, places=5)

    def test_record_transition_ignores_nonpositive_delta(self):
        store = self.new_store()
        self.assertFalse(store.record_transition("a", "b", delta=0.0))
        self.assertIsNone(store.weight("a", "b"))

    def test_rewarded_transition_ranks_first(self):
        store = self.new_store()
        store.record_transition("a", "b")
        store.record_transition("a", "c")
        store.record_transition("a", "c")
        store.record_transition("a", "b", delta=2.5)
        self.assertEqual(store.predict("a", limit=2), ["b", "c"])

    def test_decay_prunes_low_weights(self):
        store = self.new_store()
        store.record_transition("a", "b")
        store.record_transition("c", "d")
        pruned = store.decay(factor=0.5, before=time.time() + 10, min_weight=0.6)
        self.assertEqual(pruned, 2)
        self.assertIsNone(store.weight("a", "b"))
        self.assertEqual(store.count(), (0, 0))

    def test_decay_keeps_above_min_weight(self):
        store = self.new_store()
        store.record_transition("a", "b")
        pruned = store.decay(factor=0.5, before=time.time() + 10, min_weight=0.1)
        self.assertEqual(pruned, 0)
        self.assertAlmostEqual(store.weight("a", "b") or 0.0, 0.5, places=5)

    def test_get_store_uses_configured_db_path(self):
        store = self._get_store()
        self.assertTrue(store.record_transition("a", "b"))
        self.assertTrue(os.path.exists(self.db_path))


class PredictorTestCase(AnticipateTestCase):

    def test_advance_chains_within_window(self):
        predictor.reset()
        self.assertEqual(predictor.advance("alpha"), (None, "alpha", False))
        prev, current, rewarded = predictor.advance("beta")
        self.assertEqual((prev, current), ("alpha", "beta"))
        self.assertFalse(rewarded)

    def test_advance_reports_reward_on_hit(self):
        predictor.reset()
        predictor.advance("alpha")
        predictor.remember_predictions(["beta"])
        prev, current, rewarded = predictor.advance("beta")
        self.assertEqual((prev, current), ("alpha", "beta"))
        self.assertTrue(rewarded)

    def test_advance_reports_miss(self):
        predictor.reset()
        predictor.advance("alpha")
        predictor.remember_predictions(["gamma"])
        _, _, rewarded = predictor.advance("beta")
        self.assertFalse(rewarded)

    def test_advance_consumes_predictions_once(self):
        predictor.reset()
        predictor.advance("alpha")
        predictor.remember_predictions(["beta"])
        predictor.advance("beta")
        _, _, rewarded = predictor.advance("gamma")
        self.assertFalse(rewarded)

    def test_advance_drops_prev_outside_window(self):
        _settings["search"]["nextgen"]["anticipate"]["session_window"] = 0
        predictor.reset()
        predictor.advance("alpha")
        prev, current, _ = predictor.advance("beta")
        self.assertIsNone(prev)
        self.assertEqual(current, "beta")

    def test_record_transition_uses_reward_boost(self):
        predictor.reset()
        predictor.record_transition("a", "b", reward=False)
        predictor.record_transition("a", "c", reward=True)
        store = self._get_store()
        self.assertAlmostEqual(store.weight("a", "b") or 0.0, 1.0, places=5)
        self.assertAlmostEqual(store.weight("a", "c") or 0.0, 2.0, places=5)


class FacadeTestCase(AnticipateTestCase):

    def test_disabled_pillar_is_inert(self):
        _settings["search"]["nextgen"]["enabled"] = False
        self.assertFalse(anticipate.is_enabled())
        self.assertIsNone(anticipate.recall("q"))
        self.assertFalse(anticipate.store_artifact("q", [1.0]))
        anticipate.observe("q")
        self.assertEqual(anticipate.predict("q"), [])

    def test_recall_returns_artifact(self):
        self.assertTrue(anticipate.store_artifact("q", [1.0, 2.0], "ctx"))
        self.assertEqual(anticipate.recall("q"), ([1.0, 2.0], "ctx"))

    def test_observe_warms_prediction(self):
        self._get_store().record_transition("seed", "beta")
        anticipate.set_warmer(lambda query: ([0.0], "warmed:" + query))
        anticipate.observe("seed")
        deadline = time.time() + 5.0
        while time.time() < deadline and anticipate.recall("beta") is None:
            time.sleep(0.02)
        self.assertEqual(anticipate.recall("beta"), ([0.0], "warmed:beta"))

    def test_observe_reinforces_hit(self):
        store = self._get_store()
        store.record_transition("alpha", "beta")
        predictor.advance("alpha")
        predictor.remember_predictions(["beta"])
        anticipate.observe("beta")
        deadline = time.time() + 5.0
        while time.time() < deadline and (store.weight("alpha", "beta") or 0.0) < 3.0:
            time.sleep(0.02)
        self.assertAlmostEqual(store.weight("alpha", "beta") or 0.0, 3.0, places=5)


class MetricsTestCase(unittest.TestCase):

    def setUp(self):
        self.nextgen = nextgen
        self.nextgen.configure(CounterStorage(), HistogramStorage(), True)

    def test_anticipate_counters_are_aggregated(self):
        self.nextgen.anticipate_hit()
        self.nextgen.anticipate_hit()
        self.nextgen.anticipate_miss()
        self.nextgen.anticipate_transition()
        self.nextgen.anticipate_artifact()
        stats = self.nextgen.get_nextgen_stats()["anticipate"]
        self.assertEqual(stats["hit"], 2)
        self.assertEqual(stats["miss"], 1)
        self.assertEqual(stats["transition"], 1)
        self.assertEqual(stats["artifact"], 1)
        self.assertEqual(stats["total"], 3)
        self.assertAlmostEqual(stats["hit_ratio"], round(2 / 3, 4))

    def test_stats_always_expose_anticipate(self):
        self.assertIn("anticipate", self.nextgen.get_nextgen_stats())


class BridgeTestCase(AnticipateTestCase):

    def test_wire_anticipate_is_idempotent(self):
        self.assertTrue(nextgen_bridge.wire_anticipate())
        self.assertTrue(nextgen_bridge.wire_anticipate())

    def test_wire_anticipate_disabled(self):
        _settings["search"]["nextgen"]["anticipate"]["enabled"] = False
        self.assertFalse(nextgen_bridge.wire_anticipate())

    def test_accessors_fail_safe_when_disabled(self):
        _settings["search"]["nextgen"]["enabled"] = False
        self.assertIsNone(nextgen_bridge.anticipate_recall("q"))
        self.assertEqual(nextgen_bridge.anticipate_predict("q"), [])

    def test_anticipate_predict_reads_transitions(self):
        self._get_store().record_transition("alpha", "beta")
        self.assertEqual(nextgen_bridge.anticipate_predict("alpha"), ["beta"])

    def test_bridge_warmer_is_fail_safe(self):
        self.assertIsNone(nextgen_bridge._anticipate_warmer("nothing"))
