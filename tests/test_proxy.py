#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ModelLens proxy tests - stdlib fake upstream, no real gateway, no tokens.

Covers the parts that decide where a user's request goes: ordering per profile,
capability gating, failover (and when NOT to fail over), sticky sessions,
exploration, the discovery cache, streaming relay and outcome recording.
Run: python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("MODELLENS_PROXY_QUIET", "1")   # keep test output readable

import modellens as M          # noqa: E402
import modellens_proxy as P    # noqa: E402


# --------------------------------------------------------------------------
# Fake upstream gateway
# --------------------------------------------------------------------------

class Upstream:
    """Shared state for the fake gateway: per-model behaviour + call log."""

    models: dict = {}
    calls: list = []
    discovery_calls = 0
    connections = 0
    lock = threading.Lock()

    @classmethod
    def reset(cls, models: dict) -> None:
        with cls.lock:
            cls.models = models
            cls.calls = []
            cls.discovery_calls = 0
            cls.connections = 0

    @classmethod
    def called_models(cls) -> list:
        with cls.lock:
            return [(c.get("body") or {}).get("model") for c in cls.calls]


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with Upstream.lock:
            Upstream.connections += 1

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.path.rstrip("/").endswith("/models"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with Upstream.lock:
            Upstream.discovery_calls += 1
            data = [{"id": mid, **spec.get("meta", {})}
                    for mid, spec in Upstream.models.items()]
        body = json.dumps({"object": "list", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        with Upstream.lock:
            Upstream.calls.append({"body": body, "auth": self.headers.get("Authorization")})
            spec = dict(Upstream.models.get(body.get("model"), {}))
        if spec.get("delay"):
            time.sleep(spec["delay"])
        status = int(spec.get("status", 200))
        if body.get("stream") and spec.get("stream", True):
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in spec.get("chunks") or [
                    '{"choices":[{"delta":{"content":"he"}}]}',
                    '{"choices":[{"delta":{"content":"llo"}}]}']:
                data = ("data: " + piece + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(done), done))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        payload = json.dumps(spec.get("body") or {
            "id": "chatcmpl-test", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "ok"},
                         "finish_reason": "stop"}],
        }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _rows(store, endpoint, model, n=3, ok=True, status=200, latency=100.0,
          now=None, **extra):
    now = time.time() if now is None else now
    rows = [{**{"endpoint": endpoint, "model": model, "ok": ok, "status": status,
                "latency_ms": latency, "probed_at": _iso(now)}, **extra}
            for _ in range(n)]
    store.append(rows)


class ProxyHarness(unittest.TestCase):
    """Starts a fake gateway + a proxy pointed at it, on free ports."""

    models: dict = {}
    history = None          # callable(store) to seed evidence
    proxy_cfg: dict = {}
    endpoint_cfg: dict = {}

    def setUp(self):
        Upstream.reset(self.models)
        self.up_port = _free_port()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", self.up_port), FakeHandler)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = {"endpoints": [{"name": "gw",
                                 "base_url": f"http://127.0.0.1:{self.up_port}/v1",
                                 **self.endpoint_cfg}]}
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        self.data_path = os.path.join(self.tmp.name, "history.json")

        cfg = P.ProxyConfig({"table_ttl_s": 3600, "discovery_ttl_s": 3600,
                             "explore_ratio": 0, "sticky_ttl_s": 0,
                             "attempt_timeout_s": 10, "stream_timeout_s": 10,
                             **self.proxy_cfg})
        self.app = P.App(cfg, self.config_path, self.data_path,
                         synchronous_records=True)
        if self.history:
            self.history(self.app.ctx.store)
        self.app.start()
        self.addCleanup(self.app.stop)

        self.port = _free_port()
        self.proxy = P.ProxyServer(("127.0.0.1", self.port),
                                   P.make_handler(self.app), self.app)
        self.proxy.daemon_threads = True
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_proxy)

    def _stop_proxy(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    # -- client helpers --
    def request(self, body, headers=None, path="/v1/chat/completions",
                method="POST", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = None if method == "GET" else (
            raw if raw is not None else json.dumps(body))
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return out

    def chat(self, model="auto", messages=None, **extra):
        body = {"model": model,
                "messages": messages or [{"role": "user", "content": "hi"}],
                **extra}
        return self.request(body)

    @staticmethod
    def selected(headers):
        for key, val in headers.items():
            if key.lower() == "x-modellens-model":
                return val
        return None

    @staticmethod
    def header(headers, name):
        for key, val in headers.items():
            if key.lower() == name.lower():
                return val
        return None

    def wait_for(self, predicate, timeout=3.0):
        """The proxy records outcomes after relaying, so the client can win the race."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def proxy_rows(self, endpoint="gw", model=None):
        return [r for r in self.app.ctx.store.rows
                if r.get("source") == "proxy"
                and r.get("endpoint") == endpoint
                and (model is None or r.get("model") == model)]


# --------------------------------------------------------------------------
# Pure selection logic
# --------------------------------------------------------------------------

class OrderingTest(unittest.TestCase):
    def _row(self, model, score, succ, lat, verdict="healthy", pricing=None):
        return {"id": "gw/" + model, "endpoint": "gw", "model": model,
                "score": score, "success_rate": succ, "median_latency_ms": lat,
                "verdict": verdict, "attempts": 5,
                "pricing": pricing or {"input": None, "output": None}}

    def test_fast_orders_by_latency_above_the_bar(self):
        rows = [self._row("slow", 90, 100, 900.0), self._row("fast", 80, 100, 90.0),
                self._row("flaky", 70, 50, 10.0)]
        ordered, note = M.order_for_profile(rows, "fast")
        self.assertEqual([r["model"] for r in ordered], ["fast", "slow"])
        self.assertIsNone(note)

    def test_capable_orders_by_score(self):
        rows = [self._row("low", 60, 100, 100.0), self._row("high", 95, 100, 900.0)]
        ordered, _ = M.order_for_profile(rows, "capable")
        self.assertEqual([r["model"] for r in ordered], ["high", "low"])

    def test_cheap_prefers_zero_priced(self):
        rows = [self._row("paid", 95, 100, 100.0, pricing={"input": 1.0, "output": 2.0}),
                self._row("free", 80, 100, 200.0, pricing={"input": 0.0, "output": 0.0})]
        ordered, note = M.order_for_profile(rows, "cheap")
        self.assertEqual(ordered[0]["model"], "free")
        self.assertIn("zero-priced", note)

    def test_fast_with_nothing_above_the_bar_returns_empty(self):
        ordered, note = M.order_for_profile([self._row("bad", 40, 50, 10.0)], "fast")
        self.assertEqual(ordered, [])
        self.assertIn("90% success bar", note)

    def test_recommend_uses_the_same_ordering(self):
        rows = [self._row("slow", 90, 100, 900.0), self._row("fast", 80, 100, 90.0)]
        rec = M.recommend(rows, "fast")
        self.assertEqual(rec["picks"][0]["id"], "gw/fast")


class GatingTest(unittest.TestCase):
    def _cand(self, **over):
        base = dict(endpoint="gw", model="m", score=80.0, verdict="healthy",
                    success_rate=100.0, median_ms=100.0, ttft_ms=None, attempts=5,
                    age_hours=0.1, pricing={}, probe_rows=5, proxy_rows=0,
                    caps={"stream": True, "tools": True, "json": None,
                          "vision": None, "ctx": 8000})
        base.update(over)
        return P.Cand(**base)

    def cfg(self, **kw):
        return P.ProxyConfig({"explore_ratio": 0, **kw})

    def test_verdict_and_staleness(self):
        self.assertEqual(P.gate_reason(self._cand(verdict="dead"),
                                       P.Need(), self.cfg()), "verdict:dead")
        self.assertEqual(P.gate_reason(self._cand(verdict="stale"),
                                       P.Need(), self.cfg()), "stale")
        self.assertIsNone(P.gate_reason(self._cand(verdict="stale"),
                                        P.Need(), self.cfg(allow_stale_fallback=True)))

    def test_stream_only_blocked_when_known_false(self):
        need = P.Need(stream=True)
        self.assertEqual(P.gate_reason(self._cand(caps={"stream": False}),
                                       need, self.cfg()), "no_stream")
        # unknown is not a "no": blocking it would hide a working model
        self.assertIsNone(P.gate_reason(self._cand(caps={"stream": None}),
                                        need, self.cfg()))

    def test_tools_need_verified_support_unless_permissive(self):
        need = P.Need(tools=True)
        self.assertEqual(P.gate_reason(self._cand(caps={"tools": None}),
                                       need, self.cfg()), "tools_unverified")
        self.assertIsNone(P.gate_reason(self._cand(caps={"tools": True}),
                                        need, self.cfg()))
        self.assertIsNone(P.gate_reason(self._cand(caps={"tools": None}),
                                        need, self.cfg(permissive_caps=True)))

    def test_context_window_and_deny_list(self):
        need = P.Need(ctx=20000)
        self.assertEqual(P.gate_reason(self._cand(caps={"ctx": 8000}),
                                       need, self.cfg()), "context_too_small")
        self.assertIsNone(P.gate_reason(self._cand(caps={"ctx": None}),
                                        need, self.cfg()))
        self.assertEqual(P.gate_reason(self._cand(model="x-preview"),
                                       P.Need(), self.cfg(deny=["*-preview"])),
                         "denied")

    def test_deny_glob_and_allow_list(self):
        self.assertEqual(P.gate_reason(self._cand(model="gpt-4o"),
                                       P.Need(), self.cfg(allow=["claude*"])),
                         "denied")
        self.assertIsNone(P.gate_reason(self._cand(model="claude-3"),
                                        P.Need(), self.cfg(allow=["claude*"])))


class SelectionTest(unittest.TestCase):
    def _table(self, cands):
        ordered = {"capable": cands, "fast": cands, "cheap": cands,
                   "balanced": cands}
        return P.Table(time.time(), cands, ordered,
                       {k: False for k in ordered},
                       {k: None for k in ordered}, [])

    def _cand(self, model, probe_rows=5, attempts=5, success=100.0):
        return P.Cand(endpoint="gw", model=model, score=80.0, verdict="healthy",
                      success_rate=success, median_ms=100.0, ttft_ms=None,
                      attempts=attempts, age_hours=0.1, pricing={},
                      probe_rows=probe_rows, proxy_rows=0,
                      caps={"stream": True, "tools": True, "json": None,
                            "vision": None, "ctx": None})

    def cfg(self, **kw):
        return P.ProxyConfig({"explore_ratio": 0, **kw})

    def test_ladder_is_best_first_with_reason(self):
        a, b = self._cand("a"), self._cand("b")
        out = P.build_ladder(self._table([a, b]), self.cfg(), "capable",
                             P.Need(), P.Breaker(self.cfg()), P.Sticky(0),
                             None, roll=0.99)
        self.assertEqual([c.model for c in out["ladder"]], ["a", "b"])
        self.assertEqual(out["reason"], "best")

    def test_sticky_wins_then_expires(self):
        a, b = self._cand("a"), self._cand("b")
        sticky = P.Sticky(1800)
        table = self._table([a, b])
        breaker = P.Breaker(self.cfg())
        sticky.set("sess", "gw/b")
        out = P.build_ladder(table, self.cfg(), "capable", P.Need(), breaker,
                             sticky, "sess", roll=0.99)
        self.assertEqual(out["ladder"][0].model, "b")
        self.assertEqual(out["reason"], "sticky")

        expired = P.Sticky(0.0001)
        expired.set("sess", "gw/b")
        time.sleep(0.01)
        out2 = P.build_ladder(table, self.cfg(), "capable", P.Need(), breaker,
                              expired, "sess", roll=0.99)
        self.assertEqual(out2["ladder"][0].model, "a")
        self.assertEqual(out2["reason"], "best")

    def test_breaker_removes_a_candidate_then_allows_a_probe(self):
        a, b = self._cand("a"), self._cand("b")
        cfg = self.cfg(breaker={"fail_threshold": 2, "open_s": 30})
        breaker = P.Breaker(cfg)
        table = self._table([a, b])
        for _ in range(2):
            breaker.record("gw/a", ok=False, retryable=True)
        self.assertTrue(breaker.is_open("gw/a"))
        out = P.build_ladder(table, cfg, "capable", P.Need(), breaker,
                             P.Sticky(0), None, roll=0.99)
        self.assertEqual([c.model for c in out["ladder"]], ["b"])
        self.assertIn({"id": "gw/a", "reason": "breaker_open"}, out["skipped"])
        # half-open after the window: it may be tried again
        breaker._state["gw/a"]["opens_at"] = time.time() - 1
        self.assertFalse(breaker.is_open("gw/a"))
        out2 = P.build_ladder(table, cfg, "capable", P.Need(), breaker,
                              P.Sticky(0), None, roll=0.99)
        self.assertEqual([c.model for c in out2["ladder"]], ["a", "b"])

    def test_exploration_is_bounded_and_deterministic(self):
        a = self._cand("a", probe_rows=50)
        b = self._cand("b", probe_rows=1)
        c = self._cand("c", probe_rows=2)
        cfg = self.cfg(explore_ratio=0.05)
        self.assertIsNone(P.explore_pick([a, b, c], cfg, roll=0.5))
        picked = P.explore_pick([a, b, c], cfg, roll=0.01)
        self.assertEqual(picked.model, "b")      # least-measured of the top-K
        self.assertEqual(P.explore_pick([a, b, c], cfg, roll=0.01).model, "b")

    def test_no_exploration_below_the_success_bar(self):
        a = self._cand("a", probe_rows=50)
        b = self._cand("b", probe_rows=1, success=40.0)
        cfg = self.cfg(explore_ratio=0.5, explore_min_success=85)
        self.assertIsNone(P.explore_pick([a, b], cfg, roll=0.0))


class RequestShapeTest(unittest.TestCase):
    def test_resolve_profile(self):
        self.assertEqual(P.resolve_profile("auto:fast", None, "capable"),
                         ("fast", None))
        self.assertEqual(P.resolve_profile("auto", None, "capable"),
                         ("capable", None))
        self.assertEqual(P.resolve_profile("gpt-4o", None, "capable"),
                         ("capable", "gpt-4o"))
        self.assertEqual(P.resolve_profile("auto", "fast", "capable"),
                         ("fast", None))
        self.assertEqual(P.resolve_profile("auto:weird", None, "capable"),
                         ("capable", None))

    def test_need_from_body(self):
        need = P.need_from_body({
            "model": "auto", "stream": True,
            "tools": [{"type": "function"}],
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:..."}}]}],
        })
        self.assertTrue(need.stream)
        self.assertTrue(need.tools)
        self.assertTrue(need.json_mode)
        self.assertTrue(need.vision)
        self.assertGreater(need.ctx, 0)

    def test_need_without_features_is_cheap(self):
        need = P.need_from_body({"model": "auto",
                                 "messages": [{"role": "user", "content": "hi"}]})
        self.assertFalse(need.stream or need.tools or need.json_mode or need.vision)


# --------------------------------------------------------------------------
# End-to-end through the proxy
# --------------------------------------------------------------------------

class RoutingE2ETest(ProxyHarness):
    models = {
        "fast": {"delay": 0.0, "meta": {"priced": {"input": 0, "output": 0}}},
        "slow": {"delay": 0.25},
    }
    # tools/json are not measurable by a probe, so a test that sends them must
    # declare support - exactly what a real deployment has to do.
    endpoint_cfg = {"capabilities": {
        "fast": {"tools": True, "json": True},
        "slow": {"tools": True, "json": True},
    }}

    @staticmethod
    def history(store):
        _rows(store, "gw", "fast", latency=80.0)
        _rows(store, "gw", "slow", latency=900.0)

    def test_routes_to_the_fast_model_and_says_so(self):
        status, headers, data = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(self.selected(headers), "fast")
        self.assertEqual(self.header(headers, "X-Modellens-Reason"), "best")
        self.assertEqual(self.header(headers, "X-Modellens-Endpoint"), "gw")
        self.assertIn("choices", json.loads(data))
        self.assertEqual(Upstream.called_models(), ["fast"])

    def test_profile_alias_selects_by_that_profile(self):
        status, headers, _ = self.chat(model="auto:capable")
        self.assertEqual(status, 200)
        # `capable` sorts by score; both are 100% success here, so the faster
        # median wins on the tie-break - the important part is that it routed.
        self.assertIn(self.selected(headers), ("fast", "slow"))
        self.assertEqual(self.header(headers, "X-Modellens-Profile"), "capable")

    def test_pinned_model_passes_through_untouched(self):
        status, headers, _ = self.chat(model="slow")
        self.assertEqual(status, 200)
        self.assertEqual(self.selected(headers), "slow")
        self.assertEqual(self.header(headers, "X-Modellens-Profile"), "pinned")
        self.assertEqual(Upstream.called_models(), ["slow"])

    def test_request_body_is_forwarded_except_the_model(self):
        body = {"model": "auto:fast",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.2,
                "tools": [{"type": "function", "function": {"name": "f"}}],
                "response_format": {"type": "json_object"}}
        status, _, _ = self.request(body)
        self.assertEqual(status, 200)
        with Upstream.lock:
            sent = Upstream.calls[-1]["body"]
        self.assertEqual(sent["model"], "fast")
        self.assertEqual(sent["temperature"], 0.2)
        self.assertEqual(sent["tools"], body["tools"])
        self.assertEqual(sent["response_format"], body["response_format"])

    def test_outcome_is_recorded_with_source_proxy(self):
        self.chat()
        self.chat(model="auto:fast")
        self.assertTrue(self.wait_for(lambda: len(self.proxy_rows()) == 2))
        rows = self.proxy_rows()
        self.assertTrue(all(r["ok"] for r in rows))
        self.assertTrue(all(r["profile"] for r in rows))

    def test_dry_run_never_calls_upstream(self):
        status, _, data = self.request(
            {"model": "auto:fast",
             "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Modellens-Dry-Run": "1"})
        self.assertEqual(status, 200)
        decision = json.loads(data)
        self.assertEqual(decision["selected"]["model"], "fast")
        self.assertEqual(Upstream.calls, [])

    def test_discovery_is_cached_across_requests(self):
        self.assertEqual(Upstream.discovery_calls, 1)   # from startup build
        for i in range(5):
            self.chat(messages=[{"role": "user", "content": f"req {i}"}])
        self.assertEqual(Upstream.discovery_calls, 1)

    def test_models_endpoint_advertises_aliases_and_real_models(self):
        status, _, data = self.request({}, path="/v1/models", method="GET")
        self.assertEqual(status, 200)
        ids = {row["id"] for row in json.loads(data)["data"]}
        self.assertIn("auto", ids)
        self.assertIn("auto:fast", ids)
        self.assertIn("fast", ids)

    def test_health_and_metrics(self):
        self.chat()
        status, _, data = self.request({}, path="/modellens/health", method="GET")
        self.assertEqual(status, 200)
        health = json.loads(data)
        self.assertTrue(health["ok"])
        self.assertEqual(health["counters"]["requests"], 1)
        status, _, text = self.request({}, path="/modellens/metrics", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("modellens_proxy_requests_total 1", text.decode())

    def test_explain_returns_the_decision_without_forwarding(self):
        status, _, data = self.request({}, path="/modellens/explain?profile=fast",
                                       method="GET")
        self.assertEqual(status, 200)
        decision = json.loads(data)
        self.assertEqual(decision["profile"], "fast")
        self.assertEqual(decision["selected"]["model"], "fast")
        self.assertEqual(Upstream.calls, [])


class FailoverTest(ProxyHarness):
    models = {
        "broken": {"status": 500},
        "good": {"delay": 0.0},
    }

    @staticmethod
    def history(store):
        _rows(store, "gw", "broken", latency=80.0)   # ranked first
        _rows(store, "gw", "good", latency=200.0)

    def test_500_fails_over_and_reports_the_original(self):
        status, headers, data = self.chat(model="auto:fast")
        self.assertEqual(status, 200)
        self.assertEqual(self.selected(headers), "good")
        self.assertEqual(self.header(headers, "X-Modellens-Fallback-From"),
                         "gw/broken")
        self.assertEqual(self.header(headers, "X-Modellens-Attempts"), "2")
        self.assertEqual(Upstream.called_models(), ["broken", "good"])
        self.assertIn("choices", json.loads(data))

    def test_failure_is_recorded_but_does_not_win(self):
        self.chat(model="auto:fast")
        self.assertTrue(self.wait_for(
            lambda: len(self.proxy_rows(model="broken")) == 1))
        broken = self.proxy_rows(model="broken")
        self.assertFalse(broken[0]["ok"])
        self.assertEqual(broken[0]["status"], 500)


class ClientErrorTest(ProxyHarness):
    models = {
        "strict": {"status": 400,
                   "body": {"error": {"message": "bad request"}}},
        "fine": {"delay": 0.0},
    }

    @staticmethod
    def history(store):
        _rows(store, "gw", "strict", latency=50.0)
        _rows(store, "gw", "fine", latency=300.0)

    def test_400_is_relayed_and_never_retried(self):
        status, headers, data = self.chat(model="auto:fast")
        self.assertEqual(status, 400)
        self.assertEqual(Upstream.called_models(), ["strict"])   # no failover
        self.assertIn("error", json.loads(data))
        # A bad request is not a model failure: recording it would poison the score.
        time.sleep(0.2)
        self.assertEqual(self.proxy_rows(model="strict"), [])


class NoEvidenceTest(ProxyHarness):
    models = {"m": {}}

    @staticmethod
    def history(store):
        pass          # nothing probed yet

    def test_503_explains_why_and_how_to_fix(self):
        status, headers, data = self.chat(model="auto:capable")
        self.assertEqual(status, 503)
        payload = json.loads(data)
        self.assertEqual(payload["error"]["type"], "no_candidate")
        self.assertIn("probe", payload["error"]["message"])
        self.assertEqual(Upstream.calls, [])

    def test_unknown_pin_is_503_not_a_silent_route(self):
        status, _, data = self.chat(model="not-a-model")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(data)["error"]["type"], "model_unavailable")


class StreamingTest(ProxyHarness):
    models = {"chatter": {"stream": True}}

    @staticmethod
    def history(store):
        _rows(store, "gw", "chatter", latency=80.0)

    def test_sse_is_relayed_chunk_by_chunk(self):
        status, headers, data = self.chat(stream=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.header(headers, "Transfer-Encoding"), "chunked")
        text = data.decode()
        self.assertIn('data: {"choices"', text)
        self.assertIn("data: [DONE]", text)
        self.assertEqual(self.header(headers, "Content-Type"), "text/event-stream")

    def test_stream_outcome_records_ttft(self):
        self.chat(stream=True, messages=[{"role": "user", "content": "one"}])
        self.assertTrue(self.wait_for(
            lambda: len(self.proxy_rows(model="chatter")) == 1))
        row = self.proxy_rows(model="chatter")[0]
        self.assertTrue(row["ok"])
        self.assertTrue(row["stream"])
        self.assertIsNotNone(row["ttft_ms"])


class CapabilityE2ETest(ProxyHarness):
    models = {"t": {"delay": 0.0}, "n": {"delay": 0.0}}
    endpoint_cfg = {"capabilities": {"t": {"tools": True}}}

    @staticmethod
    def history(store):
        # `n` is fastest but tools is unverified for it; `t` is declared capable.
        _rows(store, "gw", "n", latency=50.0, tools_ok=False)
        _rows(store, "gw", "t", latency=200.0, tools_ok=True)

    def test_tool_call_skips_the_model_that_fails_tools(self):
        status, headers, _ = self.chat(tools=[{"type": "function",
                                              "function": {"name": "f"}}])
        self.assertEqual(status, 200)
        self.assertEqual(self.selected(headers), "t")

    def test_stream_requirement_skips_a_model_measured_as_non_streaming(self):
        _rows(self.app.ctx.store, "gw", "n2", latency=10.0, stream_ok=False)
        self.app.rebuild_table()
        status, headers, _ = self.chat(stream=True,
                                       messages=[{"role": "user", "content": "s"}])
        self.assertEqual(status, 200)
        self.assertNotEqual(self.selected(headers), "n2")

    def test_permissive_caps_lets_unverified_models_in(self):
        cfg = P.ProxyConfig({"permissive_caps": True, "explore_ratio": 0})
        table = self.app.table()
        need = P.Need(tools=True)
        reasons = {c.model: P.gate_reason(c, need, cfg) for c in table.candidates}
        self.assertIsNone(reasons["t"])
        self.assertIsNone(reasons["n"])


class StickyE2ETest(ProxyHarness):
    models = {"a": {"delay": 0.05}, "b": {"delay": 0.0}}
    proxy_cfg = {"sticky_ttl_s": 600}

    @staticmethod
    def history(store):
        _rows(store, "gw", "a", latency=100.0)
        _rows(store, "gw", "b", latency=150.0)

    def test_same_session_keeps_the_same_model(self):
        msgs = [{"role": "system", "content": "you are terse"}]
        status, headers, _ = self.request(
            {"model": "auto:capable", "messages": msgs + [{"role": "user",
                                                           "content": "one"}]})
        first = self.selected(headers)
        self.assertEqual(status, 200)
        status, headers, _ = self.request(
            {"model": "auto:capable", "messages": msgs + [{"role": "user",
                                                           "content": "two"}]})
        self.assertEqual(self.selected(headers), first)
        self.assertEqual(self.header(headers, "X-Modellens-Reason"), "sticky")

    def test_different_sessions_are_independent(self):
        # Two unrelated conversations must not share a pin: both get the
        # best-ranked model, and neither reports "sticky".
        _, h1, _ = self.request({"model": "auto:capable",
                                 "messages": [{"role": "user", "content": "alpha"}]})
        _, h2, _ = self.request({"model": "auto:capable",
                                 "messages": [{"role": "user", "content": "beta"}]})
        self.assertEqual(self.selected(h1), "a")
        self.assertEqual(self.selected(h2), "a")
        self.assertNotEqual(self.header(h1, "X-Modellens-Reason"), "sticky")
        self.assertNotEqual(self.header(h2, "X-Modellens-Reason"), "sticky")


class CostAccountingTest(ProxyHarness):
    models = {
        "m": {
            "meta": {"priced": {"input": 0.5, "output": 1.5}},
            "body": {"model": "m",
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                     "choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "ok"}}]},
        },
    }

    @staticmethod
    def history(store):
        _rows(store, "gw", "m", latency=100.0)

    def test_cost_and_tokens_are_announced_and_recorded(self):
        status, headers, _ = self.chat(model="auto:capable")
        self.assertEqual(status, 200)
        # 1000/1e6*0.5 + 500/1e6*1.5 = 0.0005 + 0.00075
        self.assertEqual(self.header(headers, "X-Modellens-Tokens"), "1000+500")
        self.assertEqual(float(self.header(headers, "X-Modellens-Cost-USD")), 0.00125)
        self.assertTrue(self.wait_for(lambda: len(self.proxy_rows(model="m")) == 1))
        row = self.proxy_rows(model="m")[0]
        self.assertEqual(row["prompt_tokens"], 1000)
        self.assertEqual(row["completion_tokens"], 500)
        self.assertAlmostEqual(row["cost_usd"], 0.00125, places=8)

    def test_cost_reaches_the_metrics(self):
        self.chat(model="auto:capable")
        self.assertTrue(self.wait_for(
            lambda: self.app.counters["cost_usd"] > 0))
        _, _, text = self.request({}, path="/modellens/metrics", method="GET")
        body = text.decode()
        self.assertIn('modellens_proxy_tokens_total{kind="input"} 1000', body)
        self.assertIn('modellens_proxy_tokens_total{kind="output"} 500', body)
        self.assertIn("modellens_proxy_cost_usd_total 0.00125", body)
        self.assertIn('modellens_proxy_model_cost_usd_total{model="m"} 0.00125', body)

    def test_health_reports_spend(self):
        self.chat(model="auto:capable")
        self.assertTrue(self.wait_for(lambda: self.app.counters["cost_usd"] > 0))
        _, _, data = self.request({}, path="/modellens/health", method="GET")
        health = json.loads(data)
        self.assertAlmostEqual(health["counters"]["cost_usd"], 0.00125, places=8)


class CostMathTest(unittest.TestCase):
    def test_pricing_per_million(self):
        self.assertAlmostEqual(
            P.compute_cost({"input": 2.0, "output": 8.0},
                           {"prompt": 1_000_000, "completion": 1_000_000}), 10.0)

    def test_unknown_price_or_usage_is_none_not_a_guess(self):
        self.assertIsNone(P.compute_cost({"input": None, "output": None},
                                         {"prompt": 100, "completion": 50}))
        self.assertIsNone(P.compute_cost({"input": 1.0, "output": 1.0}, None))
        # Half the numbers known: only the known half is billed, honestly.
        self.assertAlmostEqual(
            P.compute_cost({"input": 1.0, "output": None},
                           {"prompt": 1_000_000, "completion": 999}), 1.0)

    def test_usage_parsing_from_json_and_sse(self):
        self.assertEqual(P.usage_from_json(
            json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}).encode()),
            {"prompt": 3, "completion": 4})
        self.assertIsNone(P.usage_from_json(b"not json"))
        chunk = (b'data: {"choices":[],"usage":{"prompt_tokens":7,'
                 b'"completion_tokens":9}}\n\n')
        self.assertEqual(P.usage_from_sse(chunk), {"prompt": 7, "completion": 9})
        self.assertIsNone(P.usage_from_sse(b'data: {"choices":[{"delta":{}}]}\n\n'))


class StreamCostTest(ProxyHarness):
    models = {
        "s": {"meta": {"priced": {"input": 1.0, "output": 2.0}},
              "chunks": ['{"choices":[{"delta":{"content":"hi"}}]}',
                         '{"choices":[],"usage":{"prompt_tokens":10,'
                         '"completion_tokens":5}}']},
    }

    @staticmethod
    def history(store):
        _rows(store, "gw", "s", latency=100.0)

    def test_stream_usage_is_captured_from_the_final_chunk(self):
        status, _, _ = self.chat(model="auto:capable", stream=True)
        self.assertEqual(status, 200)
        self.assertTrue(self.wait_for(lambda: len(self.proxy_rows(model="s")) == 1))
        row = self.proxy_rows(model="s")[0]
        # 10/1e6*1.0 + 5/1e6*2.0
        self.assertAlmostEqual(row["cost_usd"], 0.00002, places=8)


class ConnectionPoolTest(ProxyHarness):
    models = {"m": {"delay": 0.0}}

    @staticmethod
    def history(store):
        _rows(store, "gw", "m", latency=100.0)

    def test_two_requests_reuse_one_connection(self):
        with Upstream.lock:            # discovery at startup also opens one
            Upstream.connections = 0
        self.chat(model="auto:capable")
        self.chat(model="auto:capable", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(Upstream.connections, 1)      # one TCP connection
        self.assertGreaterEqual(self.app.pool.hits, 1)

    def test_pool_is_bounded_and_expires(self):
        pool = P.ConnPool(max_idle=1, idle_ttl_s=0.01)

        class FakeConn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        pool.release("e", FakeConn())
        pool.release("e", FakeConn())       # over max_idle: closed immediately
        self.assertEqual(len(pool._idle["e"]), 1)
        time.sleep(0.02)
        pool._idle["e"][0] = (pool._idle["e"][0][0], time.monotonic() - 1)
        ep = M.Endpoint({"name": "e", "base_url": "http://127.0.0.1:1/v1"})
        conn, reused = pool.acquire(ep, timeout=1)
        self.assertFalse(reused)            # stale idle connection was dropped
        conn.close()

    def test_stale_keepalive_errors_are_classified(self):
        import http.client as hc
        self.assertTrue(P._stale_connection_error(hc.RemoteDisconnected("x")))
        self.assertTrue(P._stale_connection_error(ConnectionResetError()))
        self.assertFalse(P._stale_connection_error(ValueError("nope")))


class ProberToolsTest(ProxyHarness):
    models = {"tooly": {
        "body": {"model": "tooly",
                 "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "",
                                          "tool_calls": [
                                              {"id": "c1", "type": "function",
                                               "function": {"name": "modellens_ping",
                                                            "arguments": "{}"}}]}}]},
    }}
    proxy_cfg = {"probe": {"enabled": True, "interval_s": 3600, "limit": 3,
                           "check_tools": True, "check_stream": False}}

    @staticmethod
    def history(store):
        # Evidence without tools_ok: capability is unknown, so a tool-using
        # request would be gated until the prober measures it.
        _rows(store, "gw", "tooly", latency=100.0)

    def test_probe_learns_tool_support(self):
        before = self.app.table().by_id("gw/tooly")
        self.assertIsNone(before.caps["tools"])

        rows = self.app.prober.probe_once()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["tools_ok"], True)

        self.app.rebuild_table()
        after = self.app.table().by_id("gw/tooly")
        self.assertIs(after.caps["tools"], True)

    def test_tool_request_routes_once_capability_is_known(self):
        # Unknown tools => gated out before the prober runs.
        status, _, _ = self.chat(tools=[{"type": "function",
                                         "function": {"name": "f"}}])
        self.assertEqual(status, 503)
        self.app.prober.probe_once()
        self.app.rebuild_table()
        status, headers, _ = self.chat(tools=[{"type": "function",
                                              "function": {"name": "f"}}])
        self.assertEqual(status, 200)
        self.assertEqual(self.selected(headers), "tooly")


if __name__ == "__main__":
    unittest.main(verbosity=2)
