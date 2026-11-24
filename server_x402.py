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
  X402_PAY_TO                        receiving EVM address (Base) — REQUIRED:
                                     the server refuses to start without it, so
                                     a self-host never silently pays out to a
                                     hardcoded default address.
  X402_FACILITATOR                   default https://x402.org/facilitator
  X402_PORT                          default 8765
  X402_MAX_PROBE_LIMIT               server-side cap on probe limit (default 20)
  X402_MAX_PROBE_WORKERS             server-side cap on probe workers (default 4)
  PRICES in atomic USDC (6 dec): probe=50000 ranking=10000 recommend=10000 report=20000; list_models free

Payment flow (two-phase, on purpose): /verify (signature check, no money
moves) -> run the tool -> /settle only after the tool succeeded. Settling
before execution would charge the caller on-chain for a call that may still
fail (bad args, unknown model, upstream 5xx) with no refund path.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modellens as ml  # noqa: E402

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
NETWORK = "eip155:8453"  # Base mainnet
PAY_TO = os.environ.get("X402_PAY_TO", "").strip()
if not PAY_TO:
    sys.stderr.write(
        "server_x402: refusing to start — X402_PAY_TO is not set.\n"
        "  Payments would otherwise go to a hardcoded default address.\n"
        "  Export your receiving EVM address (Base), e.g.:  "
        "export X402_PAY_TO=0xYourAddress\n")
    raise SystemExit(2)
FACILITATOR = os.environ.get("X402_FACILITATOR", "https://x402.org/facilitator").rstrip("/")
PORT = int(os.environ.get("X402_PORT", "8765"))

# Hosted-mode caps: probe makes REAL upstream calls, so a caller paying $0.05
# must not be able to ask for limit=1000 / workers=500 and burn the operator's
# upstream quota. The server-side clamp wins over client arguments.
PROBE_LIMIT_CAP = int(os.environ.get("X402_MAX_PROBE_LIMIT", "20"))
PROBE_WORKERS_CAP = int(os.environ.get("X402_MAX_PROBE_WORKERS", "4"))
GLOBAL_LIMIT_CAP = 200

# A hosted probe must fit inside the payment authorization window. The caller
# signs an authorization valid for maxTimeoutSeconds, and settlement happens
# only AFTER the tool runs, so an execution longer than that window expires the
# authorization: the caller gets their answer and the operator is never paid.
# Worst case = ceil(limit / workers) waves x (chat timeout + stream timeout).
# Shorten the per-request timeout for hosted mode too: a hung upstream must not
# hold a paid call for 90 s per model.
PROBE_TIMEOUT_S = int(os.environ.get("X402_PROBE_TIMEOUT", "30"))
ml.PROBE_TIMEOUT = PROBE_TIMEOUT_S
PROBE_WAVES = -(-PROBE_LIMIT_CAP // max(1, PROBE_WORKERS_CAP))  # ceil division
PROBE_WORST_CASE_S = PROBE_WAVES * PROBE_TIMEOUT_S * 2
PAYMENT_TIMEOUT_S = max(120, PROBE_WORST_CASE_S + 60)  # + margin to settle
QUICK_TOOL_TIMEOUT_S = 60  # ranking/recommend/report only read local state

# Settlement can fail after the answer was already delivered (expired
# authorization, facilitator outage). Nothing must block the caller, but the
# operator needs a durable record to collect it later.
PENDING_LOG = os.environ.get(
    "X402_PENDING_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "settlements_pending.jsonl"))
_pending_lock = threading.Lock()

PRICES = {  # atomic USDC (6 decimals); 0 = free
    "list_models": "0",
    "ranking": os.environ.get("PRICE_RANKING", "10000"),      # $0.01
    "recommend": os.environ.get("PRICE_RECOMMEND", "10000"),  # $0.01
    "report": os.environ.get("PRICE_REPORT", "20000"),        # $0.02
    "probe": os.environ.get("PRICE_PROBE", "50000"),          # $0.05 (costs real upstream tokens)
}

CTX = ml.Context(os.environ.get("MODELLENS_CONFIG", ""), os.environ.get("MODELLENS_DATA", ""))

MAX_BODY = 1_000_000  # bytes; a JSON-RPC call is tiny, cap to avoid memory abuse


def _hosted_tools():
    # Only tools with an explicit price are exposed. A tool missing from PRICES
    # would otherwise be served as free: `audit` probes up to 100 models with
    # real completions, so it stays a local/CLI tool, never a free hosted one.
    return [t for t in ml.TOOLS if t[0] in PRICES]


def _tool_fn(name):
    for n, _, _, fn in _hosted_tools():
        if n == name:
            return fn
    return None


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def payment_requirements(amount: str, resource: str, description: str,
                         max_timeout: int = QUICK_TOOL_TIMEOUT_S) -> dict:
    return {
        "scheme": "exact",
        "network": NETWORK,
        "maxAmountRequired": amount,
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": PAY_TO,
        # The caller signs an authorization valid for this long; settlement
        # happens after execution, so it must cover the whole tool call.
        "maxTimeoutSeconds": max_timeout,
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


def decode_payment(x_payment_header: str):
    """Decode the base64url X-PAYMENT payload; None if malformed."""
    try:
        return json.loads(unb64url(x_payment_header))
    except Exception:
        return None


def _payment_body(payload, req: dict) -> dict:
    return {"x402Version": 1, "paymentPayload": payload, "paymentRequirements": req}


def verify_payment(x_payment_header: str, req: dict) -> dict:
    """Phase 1 — verify the X-PAYMENT payload with the facilitator.

    Signature/permit checks only: NO money moves yet, so a later tool failure
    leaves the caller uncharged.
    """
    payload = decode_payment(x_payment_header)
    if payload is None:
        return {"ok": False, "payload": None, "error": "bad X-PAYMENT payload"}
    try:
        v = facilitator("/verify", _payment_body(payload, req))
        if not v.get("isValid"):
            return {"ok": False, "payload": None,
                    "error": "invalid payment: " + json.dumps(v)[:200]}
        return {"ok": True, "payload": payload, "error": None}
    except Exception as e:
        return {"ok": False, "payload": payload, "error": f"facilitator error: {e}"}


def settle_payment(payload, req: dict) -> dict:
    """Phase 2 — settle on-chain. Call ONLY after the tool has succeeded."""
    try:
        s = facilitator("/settle", _payment_body(payload, req))
        if not s.get("success"):
            return {"ok": False, "tx": s.get("transaction"),
                    "error": "settle failed: " + json.dumps(s)[:200]}
        return {"ok": True, "tx": s.get("transaction"), "error": None}
    except Exception as e:
        return {"ok": False, "tx": None, "error": f"facilitator error: {e}"}


def record_pending_settlement(tool: str, payload, req: dict, error: str) -> None:
    """Keep a durable record of work delivered but not collected.

    Settlement runs after execution, so it can fail on an expired authorization
    or a facilitator outage. The caller already has the answer and must not be
    charged twice, so the payment payload is parked here for the operator to
    settle later instead of being lost with the response.
    """
    try:
        line = json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "error": error,
            "paymentPayload": payload,
            "paymentRequirements": req,
        }, ensure_ascii=False)
        with _pending_lock:
            parent = os.path.dirname(os.path.abspath(PENDING_LOG))
            os.makedirs(parent, exist_ok=True)
            with open(PENDING_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    except Exception as exc:            # never fail a delivered call over bookkeeping
        sys.stderr.write(f"could not record pending settlement for {tool}: {exc}\n")


def _sanitize_args(name, args) -> dict:
    """Clamp caller-supplied limits server-side (paid-amplification guard)."""
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    try:
        raw = out.get("limit")
        cap = PROBE_LIMIT_CAP if name == "probe" else GLOBAL_LIMIT_CAP
        if raw is not None and str(raw).strip() != "":
            out["limit"] = str(max(1, min(cap, int(raw))))
    except (TypeError, ValueError):
        pass
    if name == "probe":
        try:
            raw = out.get("workers")
            if raw is not None and str(raw).strip() != "":
                out["workers"] = str(max(1, min(PROBE_WORKERS_CAP, int(raw))))
        except (TypeError, ValueError):
            pass
    return out


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
        for n, d, spec, _ in _hosted_tools()
    ]


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-PAYMENT, Mcp-Session-Id",
    # Browser clients (MCP Inspector in a page) must be able to read them.
    "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj, extra_headers=None):
        headers = dict(CORS_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # CORS preflight
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _resource(self):
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("Host", "localhost")
        return f"{proto}://{host}/mcp"

    def do_GET(self):
        if self.path.startswith("/.well-known/x402"):
            manifest = {
                "x402Version": 1,
                "operator": "Greatbeing/modellens",
                "network": NETWORK,
                "asset": USDC_BASE,
                "payTo": PAY_TO,
                "facilitator": FACILITATOR,
                "endpoint": self._resource(),
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad content-length"})
            return
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "request body too large"})
            return
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        if not isinstance(msg, dict):
            # A JSON array/string is valid JSON but not a JSON-RPC message;
            # msg.get below would raise AttributeError inside a request thread.
            self._json(400, {"error": "jsonrpc message must be an object"})
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
            self.send_response(202)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "ping":
            self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        if method == "tools/list":
            self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_spec()}})
            return
        if method == "tools/call":
            params = msg.get("params")
            if not isinstance(params, dict):
                params = {}
            name = params.get("name")
            args = _sanitize_args(name, params.get("arguments"))
            fn = _tool_fn(name)
            if fn is None:
                self._json(200, {"jsonrpc": "2.0", "id": mid,
                                 "error": {"code": -32602, "message": f"unknown tool: {name}"}})
                return
            price = int(PRICES.get(name, "0"))
            resource = self._resource()
            if price > 0:
                xpay = self.headers.get("X-PAYMENT") or self.headers.get("X-Payment")
                # probe runs real upstream requests, so its authorization must
                # stay valid for the whole call; the read-only tools answer
                # instantly.
                timeout_s = PAYMENT_TIMEOUT_S if name == "probe" else QUICK_TOOL_TIMEOUT_S
                req = payment_requirements(str(price), resource, f"ModelLens {name}",
                                           max_timeout=timeout_s)
                if not xpay:
                    challenge = {"x402Version": 1, "accepts": [req], "error": "payment required"}
                    body = {"jsonrpc": "2.0", "id": mid,
                            "error": {"code": 402, "message": "payment required",
                                      "data": {"x402": challenge}}}
                    self._json(402, body, {"PAYMENT-REQUIRED": b64url(json.dumps(challenge).encode())})
                    return
                # Two-phase payment. Settling before execution would charge the
                # caller on-chain for a call that may still fail (bad args,
                # unknown model, upstream 5xx) with no refund path.
                v = verify_payment(xpay, req)
                if not v["ok"]:
                    self._json(402, {"jsonrpc": "2.0", "id": mid,
                                     "error": {"code": 402, "message": v["error"]}})
                    return
                try:
                    text = fn(CTX, args)
                except Exception as e:
                    # Nothing was settled, so the caller was not charged. Log the
                    # detail; return only the exception type (a raw message can
                    # leak paths, upstream bodies and key material to a stranger).
                    sys.stderr.write(f"tool {name} failed after verify; not settling: {e!r}\n")
                    self._json(200, {"jsonrpc": "2.0", "id": mid,
                                     "result": {"content": [{"type": "text",
                                                             "text": f"tool error ({type(e).__name__}) — not charged"}],
                                                "isError": True}})
                    return
                s = settle_payment(v["payload"], req)
                headers = {}
                if s["ok"]:
                    headers["PAYMENT-RESPONSE"] = b64url(json.dumps(
                        {"x402Version": 1, "transaction": s["tx"], "network": NETWORK}).encode())
                else:
                    # The tool already ran and the caller has their answer:
                    # answering 402 here would make a retrying client run the
                    # tool (and pay) again. Deliver, log, and park the payment
                    # for later collection.
                    sys.stderr.write(f"settle failed for {name}: {s['error']}\n")
                    record_pending_settlement(name, v["payload"], req, s["error"])
                self._json(200, {"jsonrpc": "2.0", "id": mid,
                                 "result": {"content": [{"type": "text", "text": text}]}},
                            headers)
                return
            # free tool
            try:
                text = fn(CTX, args)
            except Exception as e:
                sys.stderr.write(f"tool {name} failed: {e!r}\n")
                self._json(200, {"jsonrpc": "2.0", "id": mid,
                                 "result": {"content": [{"type": "text",
                                                         "text": f"tool error ({type(e).__name__})"}],
                                            "isError": True}})
                return
            self._json(200, {"jsonrpc": "2.0", "id": mid,
                             "result": {"content": [{"type": "text", "text": text}]}})
            return
        self._json(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"modellens-x402 on :{PORT}  payTo={PAY_TO}  facilitator={FACILITATOR}",
          f"probe caps: limit<={PROBE_LIMIT_CAP} workers<={PROBE_WORKERS_CAP}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()