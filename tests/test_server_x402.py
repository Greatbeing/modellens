#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local HTTP tests for server_x402.py hardening — no gateway, no real payment.

Starts the server with an EMPTY endpoint config on a free port and checks the
wire behavior: manifest, initialize, tools/list, unknown-tool fail-closed,
free tool, paid 402 challenge, X-Forwarded-Proto resource, body-size cap and
malformed Content-Length. Run: python -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
                   X402_PORT=str(cls.port), PYTHONIOENCODING="utf-8")
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "server_x402.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                cls.req("GET", "/health")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server_x402 did not become ready")

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
        st, _, body = self.req("GET", "/health")
        self.assertEqual(st, 200)
        st, _, body = self.req("GET", "/.well-known/x402")
        self.assertEqual(st, 200)
        self.assertIn("payTo", json.loads(body))

    def test_initialize_and_tools_list(self):
        st, _, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(st, 200)
        st, _, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = json.loads(body)["result"]["tools"]
        self.assertEqual(len(tools), 5)

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
        st, hd, body = self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                                 "params": {"name": "probe", "arguments": {"limit": "1"}}})
        self.assertEqual(st, 402)
        self.assertTrue(hd.get("PAYMENT-REQUIRED") or hd.get("Payment-Required"))

    def test_resource_respects_forwarded_proto(self):
        st, _, body = self.req("GET", "/.well-known/x402", headers={"X-Forwarded-Proto": "https"})
        self.assertTrue(json.loads(body)["endpoint"].startswith("https://"))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)