#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local HTTP tests for server_x402.py — no gateway, no real payment.

Two layers:
  * ServerX402Test spawns the real script on a free port and checks the wire
    behavior: manifest, initialize, tools/list, unknown-tool fail-closed, free
    tool, paid 402 challenge, X-Forwarded-Proto resource, body-size cap,
    malformed Content-Length, CORS preflight and non-object JSON-RPC.
  * PaymentFlowTest runs the same Handler in-process with a stubbed facilitator
    so the two-phase payment ordering (verify -> execute -> settle) is proven:
    no settle on tool failure, no execution on invalid payment, and the
    server-side limit/workers clamp.
Run: python -m unittest discover -s tests -v
"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# server_x402 refuses to start without a pay-out address (a hardcoded default
# would silently receive a self-hoster's money), so give it a throwaway one
# before importing the module for the in-process tests.
os.environ.setdefault("X402_PAY_TO", "0x000000000000000000000000000000000000dEaD")
os.environ.setdefault("MODELLENS_CONFIG", "")
os.environ.setdefault("MODELLENS_DATA", "")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerX402Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = dict(os.environ, MODELLENS_CONFIG="", MODELLENS_DATA="",
                   X402_PORT=str(cls.port), PYTHONIOENCODING="utf-8",
                   X402_PAY_TO="0x000000000000000000000000000000000000dEaD")
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "server_x402.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                cls.req("GET", "/health")
                return
            except Exception:
                time.sleep(0.1)
        cls.proc.kill()
        out = b""
        try:
            out = cls.proc.stdout.read() or b""
        except Exception:
            pass
        raise RuntimeError("server_x402 did not become ready: " + out.decode("utf-8", "replace")[:400])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()
        if cls.proc.stdout:
            cls.proc.stdout.close()

    @classmethod
    def req(cls, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{cls.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json",
                                            **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def _raw(self, raw_headers, body=b"{}"):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(raw_headers + b"\r\n\r\n" + body)
        chunks = []
        while True:
            d = s.recv(4096)
            if not d:
                break
            chunks.append(d)
        s.close()
        return b"".join(chunks).decode("utf-8", "replace")

    def test_health_and_manifest(self):
        st, _, _ = self.req("GET", "/health")
        self.assertEqual(st, 200)
        st, _, body = self.req("GET", "/.well-known/x402")
        self.assertEqual(st, 200)
        manifest = json.loads(body)
        self.assertIn("payTo", manifest)
        self.assertEqual(manifest["payTo"], "0x000000000000000000000000000000000000dEaD")

    def test_initialize_and_tools_list(self):
        st, _, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(st, 200)
        st, _, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = json.loads(body)["result"]["tools"]
        self.assertEqual(len(tools), 5)
        # audit runs up to 100 billed probes and has no price: never served free.
        self.assertNotIn("audit", [t["name"] for t in tools])

    def test_unknown_tool_is_jsonrpc_error_not_free_run(self):
        st, _, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32602)

    def test_free_tool_runs(self):
        st, _, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                "params": {"name": "list_models", "arguments": {}}})
        self.assertEqual(st, 200)
        self.assertIn("result", json.loads(body))

    def test_paid_tool_without_payment_is_402(self):
        st, hd, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                              "params": {"name": "probe", "arguments": {"limit": "1"}}})
        self.assertEqual(st, 402)
        self.assertTrue(hd.get("PAYMENT-REQUIRED") or hd.get("Payment-Required"))

    def test_resource_respects_forwarded_proto(self):
        st, _, body = self.req("GET", "/.well-known/x402", headers={"X-Forwarded-Proto": "https"})
        self.assertTrue(json.loads(body)["endpoint"].startswith("https://"))

    def test_cors_preflight_is_204(self):
        st, hd, _ = self.req("OPTIONS", "/mcp")
        self.assertEqual(st, 204)
        self.assertEqual(hd.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("X-PAYMENT", hd.get("Access-Control-Allow-Headers", ""))

    def test_responses_carry_cors_headers(self):
        _, hd, _ = self.req("GET", "/health")
        self.assertEqual(hd.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("PAYMENT-REQUIRED", hd.get("Access-Control-Expose-Headers", ""))

    def test_non_object_jsonrpc_is_400(self):
        st, _, _ = self.req("POST", "/mcp", [])
        self.assertEqual(st, 400)

    def test_oversized_body_is_413(self):
        resp = self._raw(
            b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2000000\r\nConnection: close")
        self.assertIn("413", resp.split("\r\n")[0])

    def test_malformed_content_length_is_400(self):
        resp = self._raw(
            b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: abc\r\nConnection: close")
        self.assertIn("400", resp.split("\r\n")[0])


class PaymentFlowTest(unittest.TestCase):
    """In-process Handler with a stubbed facilitator: proves the payment order."""

    @classmethod
    def setUpClass(cls):
        import server_x402 as S
        cls.S = S
        cls.port = _free_port()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.port), S.Handler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        S = self.S
        self.calls = []
        self._orig_facilitator = S.facilitator
        self._orig_tool_fn = S._tool_fn
        self._orig_prices = dict(S.PRICES)

    def tearDown(self):
        S = self.S
        S.facilitator = self._orig_facilitator
        S._tool_fn = self._orig_tool_fn
        S.PRICES.clear()
        S.PRICES.update(self._orig_prices)

    def _post(self, body, headers=None):
        url = f"http://127.0.0.1:{self.port}/mcp"
        r = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def _payment(self):
        payload = {"x402Version": 1, "scheme": "exact", "payload": {"sig": "0xdead"}}
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return payload, raw

    def _paid_call(self, name="probe", arguments=None):
        _, raw = self._payment()
        return self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}},
                          headers={"X-PAYMENT": raw})

    def test_settle_happens_only_after_successful_execution(self):
        S = self.S
        order = []

        def fake_facilitator(path, body):
            order.append("verify" if path == "/verify" else "settle")
            return ({"isValid": True} if path == "/verify"
                    else {"success": True, "transaction": "0xtx"})

        def fake_tool(ctx, args):
            order.append("execute")
            return "probe result"

        S.facilitator = fake_facilitator
        S._tool_fn = lambda name: fake_tool
        st, hd, body = self._paid_call()
        self.assertEqual(st, 200)
        self.assertIn("probe result", body)
        self.assertEqual(order, ["verify", "execute", "settle"])
        self.assertIn("PAYMENT-RESPONSE", hd)

    def test_tool_failure_does_not_settle(self):
        S = self.S
        order = []

        def fake_facilitator(path, body):
            order.append("verify" if path == "/verify" else "settle")
            return {"isValid": True}

        def boom(ctx, args):
            order.append("execute")
            raise RuntimeError("upstream exploded")

        S.facilitator = fake_facilitator
        S._tool_fn = lambda name: boom
        st, _, body = self._paid_call()
        self.assertEqual(st, 200)
        self.assertNotIn("settle", order)  # caller must not be charged
        self.assertTrue(json.loads(body)["result"]["isError"])

    def test_tool_failure_does_not_leak_the_exception_text(self):
        S = self.S
        S.facilitator = lambda path, body: {"isValid": True}

        def boom(ctx, args):
            # The marker MUST reach the log and MUST NOT reach the caller.
            raise RuntimeError("upstream 500: postgres://app:topsecret-db-pass@10.0.0.5/db")

        S._tool_fn = lambda name: boom
        st, _, body = self._paid_call()
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertEqual(st, 200)
        self.assertNotIn("topsecret-db-pass", text)   # no credential to a stranger
        self.assertNotIn("10.0.0.5", text)
        self.assertIn("RuntimeError", text)      # the type is still useful
        self.assertIn("not charged", text)

    def test_probe_authorization_covers_the_worst_case_execution(self):
        # Settlement happens after execution, so the signed authorization must
        # stay valid longer than the slowest possible probe.
        S = self.S
        for name in ("probe",):
            req = S.payment_requirements("50000", "https://h/mcp", f"ModelLens {name}",
                                         max_timeout=S.PAYMENT_TIMEOUT_S)
            self.assertGreaterEqual(req["maxTimeoutSeconds"], S.PROBE_WORST_CASE_S,
                                    "authorization would expire mid-call")
        self.assertGreater(S.PAYMENT_TIMEOUT_S, S.PROBE_WORST_CASE_S)
        quick = S.payment_requirements("10000", "https://h/mcp", "ModelLens ranking")
        self.assertEqual(quick["maxTimeoutSeconds"], S.QUICK_TOOL_TIMEOUT_S)
        self.assertLessEqual(S.QUICK_TOOL_TIMEOUT_S, 60)

    def test_settlement_failure_after_delivery_is_parked_for_ops(self):
        S = self.S
        with tempfile.TemporaryDirectory() as tmp:
            old = S.PENDING_LOG
            S.PENDING_LOG = os.path.join(tmp, "pending.jsonl")
            try:
                def fake_facilitator(path, body):
                    return {"isValid": True} if path == "/verify" else {
                        "success": False, "errorReason": "expired"}
                S.facilitator = fake_facilitator
                S._tool_fn = lambda name: (lambda ctx, args: "the answer")
                st, _, body = self._paid_call()
                self.assertEqual(st, 200)
                self.assertIn("the answer", body)      # answer wins over collection
                with open(S.PENDING_LOG, encoding="utf-8") as fh:
                    records = [json.loads(line) for line in fh if line.strip()]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["tool"], "probe")
                self.assertIn("expired", records[0]["error"])
                self.assertIn("paymentPayload", records[0])
            finally:
                S.PENDING_LOG = old

    def test_invalid_payment_never_executes(self):
        S = self.S
        executed = []
        S.facilitator = lambda path, body: {"isValid": False, "invalidReason": "nope"}
        S._tool_fn = lambda name: (lambda ctx, args: executed.append(1) or "x")
        st, _, body = self._paid_call()
        self.assertEqual(st, 402)
        self.assertEqual(executed, [])
        self.assertIn("invalid payment", body)

    def test_settle_failure_does_not_withhold_the_answer(self):
        S = self.S

        def fake_facilitator(path, body):
            if path == "/verify":
                return {"isValid": True}
            return {"success": False, "errorReason": "network"}

        S.facilitator = fake_facilitator
        S._tool_fn = lambda name: (lambda ctx, args: "answer")
        st, hd, body = self._paid_call()
        self.assertEqual(st, 200)
        self.assertIn("answer", body)
        self.assertNotIn("PAYMENT-RESPONSE", hd)

    def test_limit_and_workers_are_clamped_server_side(self):
        S = self.S
        seen = {}
        S.facilitator = lambda path, body: {"isValid": True}
        S._tool_fn = lambda name: (lambda ctx, args: seen.update(args) or "ok")
        st, _, _ = self._paid_call(arguments={"limit": "1000", "workers": "500"})
        self.assertEqual(st, 200)
        self.assertEqual(seen["limit"], str(S.PROBE_LIMIT_CAP))
        self.assertEqual(seen["workers"], str(S.PROBE_WORKERS_CAP))

    def test_hosted_caps_are_sane(self):
        S = self.S
        self.assertLessEqual(S.PROBE_LIMIT_CAP, 50)
        self.assertLessEqual(S.PROBE_WORKERS_CAP, 8)
        self.assertGreaterEqual(S.PROBE_LIMIT_CAP, 1)
        self.assertGreaterEqual(S.PROBE_WORKERS_CAP, 1)

    def test_bad_payment_payload_is_rejected(self):
        st, _, body = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "probe", "arguments": {}}},
            headers={"X-PAYMENT": "!!!not-base64!!!"})
        self.assertEqual(st, 402)
        self.assertIn("bad X-PAYMENT", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
