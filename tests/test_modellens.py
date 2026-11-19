#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ModelLens core unit tests — stdlib only, no network.

Run: python -m unittest discover -s tests -v
These cover the logic the package is built on: scoring, verdicts, p95, the
history store, recommendations, discovery/pricing parsing, and the MCP wire
protocol. Network is stubbed; nothing here calls a real gateway.
"""
import json
import math
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import modellens as M  # noqa: E402


def row(ok, status=200, lat=None, endpoint="e", model="m", probed_at=None):
    return {"endpoint": endpoint, "model": model, "ok": ok, "status": status,
            "latency_ms": lat, "probed_at": probed_at or "2026-01-01T00:00:00Z"}


class ScoreModelTest(unittest.TestCase):
    def test_empty_is_untested(self):
        s = M.score_model([])
        self.assertEqual(s["verdict"], "untested")
        self.assertEqual(s["score"], 0.0)

    def test_all_404_is_dead(self):
        s = M.score_model([row(False, 404) for _ in range(5)])
        self.assertEqual(s["verdict"], "dead")
        self.assertEqual(s["score"], 0.0)

    def test_all_429_is_quota_blocked(self):
        s = M.score_model([row(False, 429) for _ in range(5)])
        self.assertEqual(s["verdict"], "quota_blocked")
        self.assertEqual(s["score"], 0.0)

    def test_zero_success_scores_zero_even_with_latency(self):
        # Failures that are not cancellations (e.g. 500) still score 0.
        s = M.score_model([row(False, 500, lat=10.0)])
        self.assertEqual(s["score"], 0.0)
        self.assertEqual(s["success_rate"], 0.0)

    def test_cancel_rate_drives_unreliable(self):
        rows = [row(True, 200, 100.0) for _ in range(7)] + [row(False, 0) for _ in range(3)]
        s = M.score_model(rows)
        self.assertGreaterEqual(s["cancel_rate"], 30.0)
        self.assertEqual(s["verdict"], "unreliable")

    def test_healthy_threshold(self):
        s = M.score_model([row(True, 200, 2000.0) for _ in range(10)])
        self.assertEqual(s["verdict"], "healthy")

    def test_high_latency_is_mixed_not_healthy(self):
        s = M.score_model([row(True, 200, 5000.0) for _ in range(10)])
        self.assertEqual(s["success_rate"], 100.0)
        self.assertEqual(s["verdict"], "mixed")

    def test_score_formula(self):
        # success 100% (50) + latency 4000ms -> latency_score 50 (30) + cancel 0 (100) (20)
        s = M.score_model([row(True, 200, 4000.0) for _ in range(20)])
        self.assertAlmostEqual(s["score"], 85.0, places=1)

    def test_p95_nearest_rank(self):
        lats = [100 + i * 10 for i in range(21)]
        s = M.score_model([row(True, 200, l) for l in lats])
        self.assertEqual(s["p95_latency_ms"],
                         sorted(lats)[math.ceil(0.95 * len(lats)) - 1])

    def test_p95_requires_20_samples(self):
        s = M.score_model([row(True, 200, 100.0) for _ in range(19)])
        self.assertIsNone(s["p95_latency_ms"])


class StoreTest(unittest.TestCase):
    def test_add_bounds_per_model(self):
        st = M.Store("")
        st.add([row(True, 200, 100.0, model="m") for _ in range(25)])
        self.assertEqual(len(st.for_model("e", "m")), 25)  # keep default is 200; use small keep
        st2 = M.Store("", keep=5)
        st2.add([row(True, 200, 100.0, model="m") for _ in range(9)])
        self.assertEqual(len(st2.for_model("e", "m")), 5)

    def test_add_keeps_newest(self):
        st = M.Store("", keep=2)
        st.add([row(True, 200, 1.0, model="m", probed_at="2026-01-01T00:00:0%dZ" % i)
                for i in range(1, 5)])
        kept = st.for_model("e", "m")
        self.assertEqual([r["probed_at"] for r in kept],
                         ["2026-01-01T00:00:03Z", "2026-01-01T00:00:04Z"])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "history.json")
            st = M.Store(path)
            st.add([row(True, 200, 12.0, model="m")])
            st.save()
            st2 = M.Store(path)
            self.assertEqual(len(st2.rows), 1)
            self.assertEqual(st2.rows[0]["model"], "m")

    def test_load_tolerates_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            st = M.Store(path)
            self.assertEqual(st.rows, [])


class RecommendTest(unittest.TestCase):
    def _r(self, mid, score, succ, lat, verdict="healthy", pricing=None):
        return {"id": "e/" + mid, "endpoint": "e", "model": mid, "score": score,
                "success_rate": succ, "median_latency_ms": lat, "verdict": verdict,
                "attempts": 10, "pricing": pricing or {"input": None, "output": None}}

    def test_fast_enforces_success_bar(self):
        r = [self._r("bad", 40.0, 50.0, 200.0, "mixed"),
             self._r("good", 60.0, 95.0, 800.0)]
        rec = M.recommend(r, "fast")
        self.assertEqual([p["id"] for p in rec["picks"]], ["e/good"])

    def test_fast_returns_note_when_none_qualify(self):
        rec = M.recommend([self._r("bad", 40.0, 50.0, 200.0, "mixed")], "fast")
        self.assertEqual(rec["picks"], [])
        self.assertIn("90% success bar", rec["note"])

    def test_cheap_prefers_zero_priced(self):
        r = [self._r("paid", 95.0, 100.0, 300.0, pricing={"input": 1.0, "output": 2.0}),
             self._r("free", 80.0, 100.0, 500.0, pricing={"input": 0.0, "output": 0.0})]
        rec = M.recommend(r, "cheap")
        self.assertEqual(rec["picks"][0]["id"], "e/free")
        self.assertIn("zero-priced", rec["note"])

    def test_cheap_falls_back_with_note(self):
        r = [self._r("a", 90.0, 100.0, 500.0), self._r("b", 80.0, 100.0, 300.0)]
        rec = M.recommend(r, "cheap")
        self.assertTrue(rec["picks"])
        self.assertIn("fell back", rec["note"])

    def test_capable_sorts_by_score(self):
        r = [self._r("a", 70.0, 90.0, 100.0), self._r("b", 90.0, 90.0, 900.0)]
        rec = M.recommend(r, "capable")
        self.assertEqual(rec["picks"][0]["id"], "e/b")

    def test_excludes_dead_and_quota(self):
        r = [self._r("dead", 0.0, 0.0, None, "dead"),
             self._r("q", 0.0, 0.0, None, "quota_blocked"),
             self._r("ok", 50.0, 90.0, 400.0)]
        rec = M.recommend(r, "capable")
        self.assertEqual([p["id"] for p in rec["picks"]], ["e/ok"])


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._orig_get = M._get

    def tearDown(self):
        M._get = self._orig_get

    def _stub(self, status, body):
        calls = []

        def fake(url, timeout, headers=None):
            calls.append(url)
            return status, body, 1.0

        M._get = fake
        return calls

    def test_parse_pricing_reads_priced_and_pricing(self):
        self.assertEqual(M._parse_pricing({"priced": {"input": 1.5, "output": 2}})["input"], 1.5)
        self.assertEqual(M._parse_pricing({"pricing": {"input": 0, "output": 0}}),
                         {"input": 0.0, "output": 0.0})

    def test_parse_pricing_garbage_is_none_not_guess(self):
        self.assertEqual(M._parse_pricing({}), {"input": None, "output": None})
        self.assertEqual(M._parse_pricing({"priced": "free"}),
                         {"input": None, "output": None})
        self.assertEqual(M._parse_pricing({"priced": {"input": -3}})["input"], None)

    def test_discover_detailed_and_filter(self):
        self._stub(200, {"data": [
            {"id": "b", "priced": {"input": 0, "output": 0}},
            {"id": "a"},
            "c",
            {"no": "id"},
        ]})
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        detailed = M.discover_models_detailed(ep)
        self.assertEqual([d["id"] for d in detailed], ["a", "b", "c"])
        self.assertEqual(detailed[1]["pricing"], {"input": 0.0, "output": 0.0})
        self.assertEqual(detailed[0]["pricing"], {"input": None, "output": None})
        self.assertEqual(M.discover_models(ep), ["a", "b", "c"])

    def test_discover_models_filter(self):
        self._stub(200, {"data": [{"id": "gpt-4"}, {"id": "claude-3"}]})
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1", "models": ["gpt"]})
        self.assertEqual(M.discover_models(ep), ["gpt-4"])

    def test_discover_unreachable_returns_empty(self):
        self._stub(500, {})
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        self.assertEqual(M.discover_models(ep), [])


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self._orig_post = M._post

    def tearDown(self):
        M._post = self._orig_post

    def test_probe_success_and_stream(self):
        def fake(url, payload, timeout, headers=None):
            if payload.get("stream"):
                return 200, {"choices": [{"message": {"content": ""}}]}, 5.0
            return 200, {"choices": [{"message": {"content": "OK"}}]}, 12.0

        M._post = fake
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M.probe_model(ep, "m")
        self.assertTrue(out["ok"])
        self.assertEqual(out["latency_ms"], 12.0)
        self.assertTrue(out["stream_ok"])

    def test_probe_error_path(self):
        M._post = lambda *a, **k: (429, {"error": {"message": "quota"}}, 3.0)
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M.probe_model(ep, "m")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 429)
        self.assertEqual(out["error"], "quota")


class McpProtocolTest(unittest.TestCase):
    def setUp(self):
        self.ctx = M.Context("", "")

    def test_initialize(self):
        r = M.handle(self.ctx, {"id": 1, "method": "initialize"})
        self.assertEqual(r["result"]["protocolVersion"], M.PROTOCOL_VERSION)
        self.assertEqual(r["result"]["serverInfo"]["version"], M.SERVER_VERSION)

    def test_tools_list_has_annotations(self):
        r = M.handle(self.ctx, {"id": 2, "method": "tools/list"})
        tools = r["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["list_models", "probe", "ranking", "recommend", "report"])
        self.assertTrue(all(t.get("annotations") for t in tools))

    def test_annotations_are_honest_about_network(self):
        # ranking/recommend/report do a live read-only /models GET: openWorld=True.
        for name in ("ranking", "recommend", "report"):
            self.assertTrue(M.TOOL_ANNOTATIONS[name]["openWorldHint"], name)
            self.assertTrue(M.TOOL_ANNOTATIONS[name]["readOnlyHint"], name)
        self.assertFalse(M.TOOL_ANNOTATIONS["probe"]["readOnlyHint"])

    def test_unknown_tool_is_error(self):
        r = M.handle(self.ctx, {"id": 3, "method": "tools/call",
                                "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_method_is_error(self):
        r = M.handle(self.ctx, {"id": 4, "method": "bogus"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_probe_tool_no_endpoints_is_clean_message(self):
        r = M.handle(self.ctx, {"id": 5, "method": "tools/call",
                                "params": {"name": "probe", "arguments": {}}})
        self.assertNotIn("isError", r["result"])
        self.assertIn("No models matched", r["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)