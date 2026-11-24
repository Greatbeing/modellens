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
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import modellens as M  # noqa: E402


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def row(ok, status=200, lat=None, endpoint="e", model="m", probed_at=None):
    # Default probed_at is *now*: a row with no explicit timestamp is fresh
    # evidence. Tests about staleness pass explicit old timestamps + now.
    return {"endpoint": endpoint, "model": model, "ok": ok, "status": status,
            "latency_ms": lat, "probed_at": probed_at or _now_iso()}


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


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
        # success is scored on the Wilson lower bound of 20/20, not the raw 100%
        expected = (M.wilson_lower(20, 20) * 100 * 50 + 50 * 30 + 100 * 20) / 100
        self.assertAlmostEqual(s["score"], round(expected, 1), places=1)

    def test_p95_nearest_rank(self):
        lats = [100 + i * 10 for i in range(21)]
        s = M.score_model([row(True, 200, l) for l in lats])
        self.assertEqual(s["p95_latency_ms"],
                         sorted(lats)[math.ceil(0.95 * len(lats)) - 1])

    def test_p95_requires_20_samples(self):
        s = M.score_model([row(True, 200, 100.0) for _ in range(19)])
        self.assertIsNone(s["p95_latency_ms"])


class StaleTest(unittest.TestCase):
    NOW = 1790000000.0  # fixed "now" so age math is deterministic

    def _rows(self, age_hours, ok=True, status=200, lat=200.0, n=10):
        ts = _iso(self.NOW - age_hours * 3600)
        return [row(ok, status, lat, probed_at=ts) for _ in range(n)]

    def test_fresh_success_stays_healthy(self):
        s = M.score_model(self._rows(0.5), now=self.NOW)
        self.assertEqual(s["verdict"], "healthy")
        self.assertAlmostEqual(s["age_hours"], 0.5, places=1)

    def test_old_success_becomes_stale_score_kept(self):
        # Staleness is a verdict, not a score penalty: the evidence is old,
        # not bad. The score stays so ranking still shows how good it *was*.
        s = M.score_model(self._rows(7), now=self.NOW)
        self.assertEqual(s["verdict"], "stale")
        self.assertGreater(s["score"], 0)
        self.assertAlmostEqual(s["age_hours"], 7.0, places=1)
        self.assertEqual(s["last_probed_at"], _iso(self.NOW - 7 * 3600))

    def test_stale_boundary(self):
        self.assertEqual(M.score_model(self._rows(5.9), now=self.NOW)["verdict"],
                         "healthy")
        self.assertEqual(M.score_model(self._rows(6.1), now=self.NOW)["verdict"],
                         "stale")

    def test_dead_stays_dead_when_old(self):
        s = M.score_model(self._rows(48, ok=False, status=404), now=self.NOW)
        self.assertEqual(s["verdict"], "dead")

    def test_quota_blocked_stays_quota_blocked_when_old(self):
        s = M.score_model(self._rows(48, ok=False, status=429), now=self.NOW)
        self.assertEqual(s["verdict"], "quota_blocked")

    def test_unreliable_stays_unreliable_when_old(self):
        rows = (self._rows(48, n=7)
                + [row(False, 0, probed_at=_iso(self.NOW - 48 * 3600))
                   for _ in range(3)])
        self.assertEqual(M.score_model(rows, now=self.NOW)["verdict"], "unreliable")

    def test_missing_timestamp_is_never_stale(self):
        # Age unknown means "not proven stale": old history files may lack
        # probed_at, and they must not flip to stale on a guess.
        rows = [{k: v for k, v in r.items() if k != "probed_at"}
                for r in self._rows(0)]
        s = M.score_model(rows, now=self.NOW)
        self.assertEqual(s["verdict"], "healthy")
        self.assertIsNone(s["age_hours"])

    def test_format_age(self):
        self.assertEqual(M._format_age(None), "-")
        self.assertEqual(M._format_age(0.25), "15m")
        self.assertEqual(M._format_age(3.0), "3h")
        self.assertEqual(M._format_age(72.0), "3d")


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

    def test_trim_without_timestamps_keeps_the_newest_in_order(self):
        # Rows pushed by the worker / imported by hand have no probed_at.
        # Sorting a newest-first bucket on a missing key kept [newest, oldest]
        # and silently dropped the middle of the history.
        st = M.Store("", keep=2)
        for i in range(10):
            st.add([{"endpoint": "e", "model": "m", "ok": True,
                     "status": 200, "latency_ms": 1.0, "n": i}])
        self.assertEqual([r["n"] for r in st.for_model("e", "m")], [8, 9])

    def test_trim_keeps_each_model_separately(self):
        st = M.Store("", keep=2)
        for i in range(5):
            st.add([{"endpoint": "e", "model": "noisy", "ok": True, "n": i},
                    {"endpoint": "e", "model": "quiet", "ok": True, "n": i}])
        self.assertEqual([r["n"] for r in st.for_model("e", "noisy")], [3, 4])
        self.assertEqual([r["n"] for r in st.for_model("e", "quiet")], [3, 4])

    def test_trim_keys_on_the_pair_not_a_joined_string(self):
        # A tuple key cannot conflate the sentinel a joined string invents:
        # f"{endpoint}/{model}" renders a missing endpoint as "None", which is
        # the same key as an endpoint actually named "None".
        st = M.Store("", keep=1)
        st.add([{"model": "m", "ok": True, "n": 1},                 # no endpoint
                {"endpoint": "None", "model": "m", "ok": True, "n": 2}])
        self.assertEqual(len(st.rows), 2)
        self.assertEqual(sorted(r["n"] for r in st.rows), [1, 2])

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

    def test_excludes_stale_even_with_top_score(self):
        r = [self._r("stale", 99.0, 100.0, 200.0, "stale"),
             self._r("ok", 50.0, 90.0, 400.0)]
        rec = M.recommend(r, "capable")
        self.assertEqual([p["id"] for p in rec["picks"]], ["e/ok"])

    def test_all_stale_note_says_probe_again(self):
        rec = M.recommend([self._r("old", 90.0, 100.0, 200.0, "stale")], "capable")
        self.assertEqual(rec["picks"], [])
        self.assertIn("stale", rec["note"])
        self.assertIn("probe again", rec["note"])


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
        self._orig_stream = M._open_stream
        self._orig_sse = M._probe_stream_sse

    def tearDown(self):
        M._post = self._orig_post
        M._open_stream = self._orig_stream
        M._probe_stream_sse = self._orig_sse

    def _stream_ok(self, ttft_ms=40.0, total_ms=90.0):
        M._open_stream = lambda ep, model: (200, object(), ttft_ms, None)
        return None

    def test_probe_success_and_stream(self):
        def fake_post(url, payload, timeout, headers=None):
            return 200, {"choices": [{"message": {"content": "OK"}}]}, 12.0

        M._post = fake_post
        M._open_stream = lambda ep, model: (200, object(), 30.0, None)
        # The SSE reader is exercised separately; here it is stubbed to succeed
        # so this test covers the non-stream decision path only.
        orig = M._probe_stream_sse
        M._probe_stream_sse = lambda ep, model: {
            "ok": True, "status": 200, "ttft_ms": 30.0, "latency_ms": 60.0, "error": None}
        try:
            ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
            out = M.probe_model(ep, "m")
        finally:
            M._probe_stream_sse = orig
        self.assertTrue(out["ok"])
        self.assertEqual(out["latency_ms"], 12.0)
        self.assertTrue(out["stream_ok"])
        self.assertEqual(out["ttft_ms"], 30.0)

    def test_probe_error_path(self):
        M._post = lambda *a, **k: (429, {"error": {"message": "quota"}}, 3.0)
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M.probe_model(ep, "m")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 429)
        self.assertEqual(out["error"], "quota")

    def test_probe_does_not_stream_when_first_call_failed(self):
        M._post = lambda *a, **k: (500, {}, 3.0)
        called = []
        M._probe_stream_sse = lambda ep, model: called.append(1) or {
            "ok": True, "status": 200, "ttft_ms": 1.0, "latency_ms": 1.0, "error": None}
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M.probe_model(ep, "m")
        self.assertFalse(out["ok"])
        self.assertEqual(called, [])  # no point paying for a stream of a dead model
        self.assertIsNone(out["stream_ok"])


class ProbeValidationTest(unittest.TestCase):
    """HTTP 200 is not success. Each case here is a real gateway lying."""

    def test_error_body_with_200_is_failure(self):
        valid, reason = M._is_valid_chat_response(
            {"error": {"message": "insufficient quota", "code": 429}})
        self.assertFalse(valid)
        self.assertEqual(reason, "body_error")

    def test_raw_wrapper_is_not_a_completion(self):
        # _maybe_json wraps non-JSON (502 HTML, empty body) as {"raw": ...}.
        self.assertFalse(M._is_valid_chat_response({"raw": "<html>502</html>"})[0])
        self.assertEqual(M._is_valid_chat_response({"raw": ""}), (False, "non_json_raw"))

    def test_empty_choices_is_failure(self):
        self.assertEqual(M._is_valid_chat_response({"choices": []}), (False, "empty_choices"))
        self.assertEqual(M._is_valid_chat_response({}), (False, "non_json_raw"))

    def test_empty_content_is_failure(self):
        body = {"choices": [{"message": {"content": "   "}}]}
        self.assertEqual(M._is_valid_chat_response(body), (False, "empty_content"))

    def test_finish_reason_error_is_failure(self):
        body = {"choices": [{"finish_reason": "error", "message": {"content": "x"}}]}
        self.assertEqual(M._is_valid_chat_response(body), (False, "finish_error"))

    def test_reasoning_or_tool_call_counts_as_payload(self):
        self.assertTrue(M._is_valid_chat_response(
            {"choices": [{"message": {"content": "", "reasoning_content": "think"}}]})[0])
        self.assertTrue(M._is_valid_chat_response(
            {"choices": [{"message": {"content": None,
                                      "tool_calls": [{"function": {"name": "x"}}]}}]})[0])

    def test_non_dict_is_failure(self):
        self.assertEqual(M._is_valid_chat_response("ok"), (False, "non_json"))

    def test_network_error_body_is_classified(self):
        import socket as _socket
        body = M._network_error_body(_socket.timeout("timed out"))
        self.assertEqual(body["__error__"]["type"], "timeout")
        self.assertIn("timed out", body["__error__"]["message"])

    def test_failure_message_surfaces_transport_type(self):
        text, etype = M._failure_message(0, {"__error__": {"type": "dns_error",
                                                           "message": "no such host"}}, "")
        self.assertEqual(etype, "dns_error")
        self.assertIn("no such host", text)


class StreamProbeTest(unittest.TestCase):
    """_probe_stream_sse against a stubbed SSE transport."""

    def setUp(self):
        self._orig = M._open_stream

    def tearDown(self):
        M._open_stream = self._orig

    def _serve(self, lines, status=200):
        M._open_stream = lambda ep, model: (status, iter(lines), 10.0, None)

    def test_reads_sse_and_measures_ttft(self):
        self._serve([b'data: {"choices":[{"delta":{"content":"O"}}]}\n',
                     b'data: {"choices":[{"delta":{"content":"K"}}]}\n',
                     b'data: [DONE]\n'])
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M._probe_stream_sse(ep, "m")
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["ttft_ms"])
        self.assertGreaterEqual(out["latency_ms"], 0)

    def test_done_without_tokens_is_not_ok(self):
        self._serve([b'data: [DONE]\n'])
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M._probe_stream_sse(ep, "m")
        self.assertFalse(out["ok"])
        self.assertIn("no token chunk", out["error"])

    def test_error_event_inside_stream_is_failure(self):
        self._serve([b'data: {"error":{"message":"upstream exploded"}}\n'])
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M._probe_stream_sse(ep, "m")
        self.assertFalse(out["ok"])
        self.assertIn("upstream exploded", out["error"])

    def test_non_200_stream_is_failure(self):
        M._open_stream = lambda ep, model: (200, None, 5.0, "HTTP 502")
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M._probe_stream_sse(ep, "m")
        self.assertFalse(out["ok"])
        self.assertIn("502", out["error"])

    def test_garbage_lines_are_skipped(self):
        self._serve([b': keepalive\n',
                     b'data: not-json\n',
                     b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'])
        ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
        out = M._probe_stream_sse(ep, "m")
        self.assertTrue(out["ok"])


class ArgParsingTest(unittest.TestCase):
    def test_parse_bool_string_false_is_false(self):
        # bool("false") is True in Python — the whole point of the helper.
        self.assertFalse(M._parse_bool("false"))
        self.assertFalse(M._parse_bool("0"))
        self.assertFalse(M._parse_bool(False))
        self.assertFalse(M._parse_bool(0))

    def test_parse_bool_truthy(self):
        for v in (True, 1, "1", "true", "TRUE", "yes", "on"):
            self.assertTrue(M._parse_bool(v), v)

    def test_parse_bool_garbage_falls_back(self):
        self.assertTrue(M._parse_bool("maybe", default=True))
        self.assertFalse(M._parse_bool("maybe", default=False))
        self.assertFalse(M._parse_bool(None))

    def test_clamp_int_bounds(self):
        self.assertEqual(M._clamp_int(-5, 20, 1, 200), 1)      # no negative slice
        self.assertEqual(M._clamp_int(50000, 1, 1, 16), 16)    # no thread storm
        self.assertEqual(M._clamp_int("7", 20, 1, 200), 7)
        self.assertEqual(M._clamp_int(None, 20, 1, 200), 20)
        self.assertEqual(M._clamp_int("abc", 20, 1, 200), 20)

    def test_ranking_default_limit_matches_schema(self):
        # The schema advertises "(default 20)" — the code must use it too.
        self.assertEqual(M.RANKING_DEFAULT_LIMIT, 20)
        schema = {t[0]: t[2] for t in M.TOOLS}["ranking"]
        self.assertIn("default 20", json.dumps(schema))
        self.assertIn("all", schema)


class ScoreEdgeTest(unittest.TestCase):
    """Inputs that used to raise or mislead."""

    def test_missing_latency_earns_no_latency_credit(self):
        rows = [row(True, 200, lat=None) for _ in range(5)]
        s = M.score_model(rows)  # used to TypeError: None / int
        self.assertEqual(s["success_rate"], 100.0)
        self.assertIsNone(s["median_latency_ms"])
        self.assertEqual(s["verdict"], "mixed")
        expected = (M.wilson_lower(5, 5) * 100 * 50 + 0 + 100 * 20) / 100
        self.assertAlmostEqual(s["score"], round(expected, 1), places=1)

    def test_zero_latency_is_fast_not_skipped(self):
        # `(med or 9e9)` made med == 0 read as unmeasured; it is just fast.
        s = M.score_model([row(True, 200, lat=0.0) for _ in range(5)])
        self.assertEqual(s["verdict"], "healthy")

    def test_bool_latency_ignored(self):
        rows = [row(True, 200, lat=True), row(True, 200, lat=100.0)]
        s = M.score_model(rows)
        self.assertEqual(s["median_latency_ms"], 100.0)

    def test_auth_errors_are_named(self):
        s = M.score_model([row(False, 401), row(False, 403)])
        self.assertEqual(s["verdict"], "auth_error")
        self.assertEqual(s["score"], 0.0)

    def test_server_errors_are_failing_not_mixed(self):
        s = M.score_model([row(False, 500), row(False, 502), row(False, 503)])
        self.assertEqual(s["verdict"], "failing")
        self.assertEqual(s["score"], 0.0)

    def test_age_keeps_minute_precision(self):
        now = 1790000000.0
        ts = _iso(now - 17 * 60)  # 17 minutes ago
        s = M.score_model([row(True, 200, 100.0, probed_at=ts)], now=now)
        self.assertAlmostEqual(s["age_hours"] * 60, 17.0, places=0)
        self.assertEqual(M._format_age(s["age_hours"]), "17m")  # not 18m from 0.3h

    def test_all_failing_recommends_nothing(self):
        failing = {"id": "e/x", "endpoint": "e", "model": "x", "score": 0.0,
                   "success_rate": 0.0, "median_latency_ms": None,
                   "verdict": "failing", "attempts": 3, "pricing": {}}
        self.assertEqual(M.recommend([failing], "capable")["picks"], [])


class CliTest(unittest.TestCase):
    """The README's CLI examples must actually run (argparse used to reject them)."""

    def _run(self, *argv):
        import subprocess
        env = dict(os.environ, MODELLENS_CONFIG="", MODELLENS_DATA="",
                   PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "modellens.py"), *argv],
            env=env, capture_output=True, text=True, timeout=60)

    def test_version_flag(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(M.SERVER_VERSION, r.stdout)

    def test_once_probe_accepts_documented_flags(self):
        r = self._run("--once", "probe", "--limit", "2", "--workers", "2",
                      "--check-tools", "--no-stream")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No models matched", r.stdout)

    def test_once_ranking_accepts_limit_and_all(self):
        r = self._run("--once", "ranking", "--limit", "5", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No models discovered", r.stdout)

    def test_once_recommend_accepts_profile(self):
        r = self._run("--once", "recommend", "--profile", "fast")
        self.assertEqual(r.returncode, 0, r.stderr)


class ProtocolRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.ctx = M.Context("", "")

    def test_non_object_message_is_invalid_request_not_a_crash(self):
        for bad in ([], "hi", 7, None):
            r = M.handle(self.ctx, bad)
            self.assertIsNotNone(r, bad)
            self.assertEqual(r["error"]["code"], -32600, bad)

    def test_tools_call_without_params_is_error(self):
        r = M.handle(self.ctx, {"id": 1, "method": "tools/call"})
        self.assertEqual(r["error"]["code"], -32602)

    def test_tools_call_with_non_object_args_defaults(self):
        r = M.handle(self.ctx, {"id": 2, "method": "tools/call",
                                "params": {"name": "list_models", "arguments": "nope"}})
        self.assertIn("result", r)


class StoreMultiWriterTest(unittest.TestCase):
    """The CLI, the MCP server and the hosted x402 server share one history file."""

    def test_two_instances_do_not_lose_each_others_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            a, b = M.Store(path), M.Store(path)
            a.add([{"endpoint": "e", "model": "a", "ok": True}])
            a.save()
            b.add([{"endpoint": "e", "model": "b", "ok": True}])
            b.save()                       # reloads a's row instead of clobbering
            with open(path, encoding="utf-8") as fh:
                models = sorted(r["model"] for r in json.load(fh)["rows"])
            self.assertEqual(models, ["a", "b"])

    def test_foreign_write_with_equal_mtime_is_still_merged(self):
        # A restored backup / rsync -t / coarse filesystem leaves mtime equal:
        # mtime-based detection misses it, the content hash does not.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            st = M.Store(path)
            st.add([{"endpoint": "e", "model": "mine", "ok": True}])
            st.save()
            before = os.stat(path)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "rows": [
                    {"endpoint": "e", "model": "theirs", "ok": True}]}, fh)
            os.utime(path, (before.st_atime, before.st_mtime))
            st.add([{"endpoint": "e", "model": "mine2", "ok": True}])
            st.save()
            with open(path, encoding="utf-8") as fh:
                models = sorted(r["model"] for r in json.load(fh)["rows"])
            self.assertEqual(models, ["mine2", "theirs"])

    def test_append_is_one_atomic_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            st = M.Store(path)
            st.append([{"endpoint": "e", "model": "m", "ok": True}])
            st2 = M.Store(path)
            self.assertEqual(len(st2.for_model("e", "m")), 1)
            self.assertEqual([f for f in os.listdir(tmp) if f.endswith(".tmp")], [])

    def test_corrupt_history_is_copied_aside_not_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            st = M.Store(path)
            self.assertEqual(st.rows, [])
            st.append([{"endpoint": "e", "model": "m", "ok": True}])
            backups = [f for f in os.listdir(tmp) if ".corrupt-" in f]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(tmp, backups[0]), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "{not json")

    def test_lock_file_is_reused_not_grown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            st = M.Store(path)
            for i in range(3):
                st.append([{"endpoint": "e", "model": "m", "ok": True, "n": i}])
            self.assertLessEqual(os.path.getsize(path + ".lock"), 8)

    def test_a_server_instance_sees_rows_written_by_another(self):
        # A long-running hosted server only has its own in-memory rows; a CLI
        # probe batch must show up in its next ranking.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{"name": "x", "base_url": "http://h/v1"}]}, fh)
            server, cli = M.Store(path), M.Store(path)
            ep = M.Endpoint({"name": "x", "base_url": "http://h/v1"})
            orig_get = M._get
            M._get = lambda *a, **k: (200, {"data": [{"id": "m"}]}, 1.0)
            try:
                cli.append([row(True, 200, 100.0, endpoint="x", model="m")])
                ranking = M.build_ranking(server, [ep])
            finally:
                M._get = orig_get
            self.assertEqual(len(ranking), 1)
            self.assertEqual(ranking[0]["attempts"], 1)
            self.assertEqual(ranking[0]["verdict"], "healthy")

    def test_writer_threads_against_the_same_file_all_land(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            errors = []
            def work(n):
                try:
                    st = M.Store(path)
                    for i in range(5):
                        st.append([{"endpoint": "e", "model": f"m{n}", "ok": True, "n": i}])
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            st = M.Store(path)
            for n in range(6):
                self.assertEqual(len(st.for_model("e", f"m{n}")), 5, n)


class StoreConcurrencyTest(unittest.TestCase):
    def test_concurrent_saves_use_unique_temp_files(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            errors = []

            def work(n):
                try:
                    st = M.Store(path)
                    st.add([row(True, 200, float(n), model="m%d" % n)])
                    for _ in range(10):
                        st.save()
                except Exception as exc:  # a fixed .tmp name races here
                    errors.append(exc)

            threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertTrue(os.path.exists(path))
            import glob as _glob
            self.assertEqual(_glob.glob(os.path.join(tmp, "*.tmp")), [])

    def test_for_model_reads_a_snapshot(self):
        st = M.Store("")
        st.add([row(True, 200, 1.0, model="m")])
        got = st.for_model("e", "m")
        got.clear()  # mutating the returned list must not corrupt the store
        self.assertEqual(len(st.for_model("e", "m")), 1)


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
                         ["list_models", "probe", "ranking", "recommend", "report", "audit"])
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



class EvidenceConfidenceTest(unittest.TestCase):
    def test_one_lucky_probe_does_not_outrank_a_proven_model(self):
        lucky = M.score_model([row(True, 200, 150.0)])
        proven = M.score_model([row(True, 200, 400.0) for _ in range(40)])
        self.assertTrue(lucky["low_evidence"])
        self.assertFalse(proven["low_evidence"])
        self.assertGreater(proven["score"], lucky["score"])

    def test_wilson_lower_bounds(self):
        self.assertEqual(M.wilson_lower(0, 0), 0.0)
        self.assertAlmostEqual(M.wilson_lower(1, 1), 0.2065, places=3)
        self.assertLess(M.wilson_lower(20, 20), M.wilson_lower(100, 100))
        self.assertLessEqual(M.wilson_lower(100, 100), 1.0)

    def test_lower_bound_reported(self):
        s = M.score_model([row(True, 200, 100.0)])
        self.assertEqual(s["success_rate"], 100.0)
        self.assertAlmostEqual(s["success_lower_bound"], 20.7, places=1)


class AuditTest(unittest.TestCase):
    def rows(self):
        return [
            {"endpoint": "gw", "model": "fast", "ok": True, "status": 200, "latency_ms": 300.0,
             "ttft_ms": 120.0, "stream_ok": True, "tools_ok": True},
            {"endpoint": "gw", "model": "slow", "ok": True, "status": 200, "latency_ms": 12000.0,
             "stream_ok": False},
            {"endpoint": "gw", "model": "gone", "ok": False, "status": 404, "latency_ms": None},
            {"endpoint": "gw", "model": "quota", "ok": False, "status": 429, "latency_ms": None},
        ]

    def test_summary_counts_and_failure_reasons(self):
        md = M.build_audit("gw", 6, self.rows(), generated_at="T")
        self.assertIn("# Gateway audit: gw", md)
        self.assertIn("| Models advertised | 6 |", md)
        self.assertIn("**2/4 (50%)**", md)
        self.assertIn("| Not probed (limit) | 2 |", md)
        self.assertIn("| not found (404) | 1 |", md)
        self.assertIn("| quota / rate limit (429) | 1 |", md)
        self.assertIn("| Streaming works | 1/2 (50%) |", md)
        self.assertIn("12.0s", md)
        # working models first, fastest first
        self.assertLess(md.index("`fast`"), md.index("`slow`"))
        self.assertLess(md.index("`slow`"), md.index("`gone`"))

    def test_empty_run_does_not_crash(self):
        md = M.build_audit("gw", 0, [], generated_at="T")
        self.assertIn("0/0", md)

    def test_tool_audit_probes_and_records(self):
        ctx = M.Context("", os.path.join(tempfile.mkdtemp(), "h.json"))
        ctx.endpoints = [M.Endpoint({"name": "gw", "base_url": "http://x/v1"})]
        rows = {r["model"]: r for r in self.rows()}
        with mock.patch.object(M, "discover_models", return_value=list(rows)), \
             mock.patch.object(M, "_probe_one", side_effect=lambda ep, m, t, s: dict(rows[m])):
            md = M.tool_audit(ctx, {"limit": 3})
        self.assertIn("| Models probed | 3 |", md)
        self.assertEqual(len(ctx.store.rows), 3)

    def test_tool_audit_needs_endpoint_when_ambiguous(self):
        ctx = M.Context("", os.path.join(tempfile.mkdtemp(), "h.json"))
        ctx.endpoints = [M.Endpoint({"name": "a", "base_url": "http://a/v1"}),
                         M.Endpoint({"name": "b", "base_url": "http://b/v1"})]
        self.assertIn("audit needs endpoint", M.tool_audit(ctx, {}))


if __name__ == '__main__':
    unittest.main()
