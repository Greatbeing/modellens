#!/usr/bin/env python3
"""ModelLens hosted MCP server with x402 per-call payments (USDC on Base).

Endpoints:
  GET  /.well-known/x402   x402 manifest (payTo, network, asset, facilitator)
  GET  /health             liveness
  POST /mcp                MCP streamable-HTTP (JSON-RPC 2.0, application/json)

Paid tools return HTTP 402 + PAYMENT-REQUIRED when no X-PAYMENT header is
present. The client pays (EIP-3009 gasless USDC on Base) and retries with
X-PAYMENT; the server verifies+settles via the facilitator, then runs the tool.

Env:
  MODELLENS_CONFIG, MODELLENS_DATA   endpoint config / history (like CLI)
  X402_PAY_TO                        receiving EVM address (Base)
  X402_FACILITATOR                   default https://x402.org/facilitator
  X402_PORT                          default 8765
  PRICES in atomic USDC (6 dec): probe=50000 ranking=10000 recommend=10000 report=20000; list_models free
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modellens as ml  # noqa: E402

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
NETWORK = "eip155:8453"  # Base mainnet
PAY_TO = os.environ.get("X402_PAY_TO", "0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50")
FACILITATOR = os.environ.get("X402_FACILITATOR", "https://x402.org/facilitator").rstrip("/")
PORT = int(os.environ.get("X402_PORT", "8765"))

PRICES = {  # atomic USDC (6 decimals); 0 = free
    "list_models": "0",
    "ranking": os.environ.get("PRICE_RANKING", "10000"),      # $0.01
    "recommend": os.environ.get("PRICE_RECOMMEND", "10000"),  # $0.01
    "report": os.environ.get("PRICE_REPORT", "20000"),        # $0.02
    "probe": os.environ.get("PRICE_PROBE", "50000"),          # $0.05 (costs real upstream tokens)
}

CTX = ml.Context(os.environ.get("MODELLENS_CONFIG", ""), os.environ.get("MODELLENS_DATA", ""))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def payment_requirements(amount: str, resource: str, description: str) -> dict:
    return {
        "scheme": "exact",
        "network": NETWORK,
        "maxAmountRequired": amount,
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "asset": USDC_BASE,
        "extra": {"name": "USDC", "version": "2"},
    }


def facilitator(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        FACILITATOR + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def verify_and_settle(x_payment_header: str, req: dict) -> dict:
    """Verify the X-PAYMENT payload with the facilitator and settle on-chain.
    Returns {"ok": bool, "tx": str|None, "error": str|None}."""
    try:
        payload = json.loads(unb64url(x_payment_header))
    except Exception as e:
        return {"ok": False, "tx": None, "error": f"bad X-PAYMENT: {e}"}
    body = {"x402Version": 1, "paymentPayload": payload, "paymentRequirements": req}
    try:
        v = facilitator("/verify", body)
        if not v.get("isValid"):
            return {"ok": False, "tx": None, "error": "invalid payment: " + json.dumps(v)[:200]}
        s = facilitator("/settle", body)
        if not s.get("success"):
            return {"ok": False, "tx": s.get("transaction"), "error": "settle failed: " + json.dumps(s)[:200]}
        return {"ok": True, "tx": s.get("transaction"), "error": None}
    except Exception as e:
        return {"ok": False, "tx": None, "error": f"facilitator error: {e}"}


def tool_spec():
    return [
        {
            "name": n,
            "description": d,
            "annotations": ml.TOOL_ANNOTATIONS.get(n),
            "inputSchema": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v} for k, v in spec.items()},
                "required": [],
            },
            "x402Price": ("$" + format(int(PRICES.get(n, "0")) / 1e6, ".6f").rstrip("0").rstrip(".")) if int(PRICES.get(n, "0")) else "free",
        }
        for n, d, spec, _ in ml.TOOLS
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/.well-known/x402"):
            manifest = {
                "x402Version": 1,
                "operator": "Greatbeing/modellens",
                "network": NETWORK,
                "asset": USDC_BASE,
                "payTo": PAY_TO,
                "facilitator": FACILITATOR,
                "endpoint": f"http://{self.headers.get('Host', 'localhost')}/mcp",
                "tools": {t["name"]: t["x402Price"] for t in tool_spec()},
            }
            self._json(200, manifest)
        elif self.path.startswith("/health"):
            self._json(200, {"ok": True, "server": ml.SERVER_NAME, "version": ml.SERVER_VERSION})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/mcp"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": ml.SERVER_NAME + "-x402", "version": ml.SERVER_VERSION},
            }})
            return
        if method and method.startswith("notifications/"):
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers()
            return
        if method == "ping":
            self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        if method == "tools/list":
            self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_spec()}})
            return
        if method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
            price = int(PRICES.get(name, "0"))
            resource = f"http://{self.headers.get('Host', 'localhost')}/mcp"
            if price > 0:
                xpay = self.headers.get("X-PAYMENT") or self.headers.get("X-Payment")
                req = payment_requirements(str(price), resource, f"ModelLens {name}")
                if not xpay:
                    challenge = {"x402Version": 1, "accepts": [req], "error": "payment required"}
                    body = {"jsonrpc": "2.0", "id": mid,
                            "error": {"code": 402, "message": "payment required",
                                      "data": {"x402": challenge}}}
                    self._json(402, body, {"PAYMENT-REQUIRED": b64url(json.dumps(challenge).encode())})
                    return
                res = verify_and_settle(xpay, req)
                if not res["ok"]:
                    body = {"jsonrpc": "2.0", "id": mid, "error": {"code": 402, "message": res["error"]}}
                    self._json(402, body)
                    return
                # run the tool
                for n, _, _, fn in ml.TOOLS:
                    if n == name:
                        try:
                            text = fn(CTX, args)
                        except Exception as e:
                            self._json(200, {"jsonrpc": "2.0", "id": mid,
                                             "result": {"content": [{"type": "text", "text": f"tool error: {e}"}],
                                                        "isError": True}})
                            return
                        pay_resp = b64url(json.dumps({"x402Version": 1, "transaction": res["tx"],
                                                      "network": NETWORK}).encode())
                        self._json(200, {"jsonrpc": "2.0", "id": mid,
                                         "result": {"content": [{"type": "text", "text": text}]}},
                                   {"PAYMENT-RESPONSE": pay_resp})
                        return
                self._json(200, {"jsonrpc": "2.0", "id": mid,
                                 "result": {"content": [{"type": "text", "text": "unknown tool"}],
                                            "isError": True}})
                return
            # free tool
            for n, _, _, fn in ml.TOOLS:
                if n == name:
                    try:
                        text = fn(CTX, args)
                    except Exception as e:
                        self._json(200, {"jsonrpc": "2.0", "id": mid,
                                         "result": {"content": [{"type": "text", "text": f"tool error: {e}"}],
                                                    "isError": True}})
                        return
                    self._json(200, {"jsonrpc": "2.0", "id": mid,
                                     "result": {"content": [{"type": "text", "text": text}]}})
                    return
            self._json(200, {"jsonrpc": "2.0", "id": mid,
                             "result": {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}})
            return
        self._json(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"modellens-x402 on :{PORT}  payTo={PAY_TO}  facilitator={FACILITATOR}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()