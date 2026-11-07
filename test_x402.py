#!/usr/bin/env python3
"""Smoke-test server_x402.py locally: manifest, initialize, tools/list, free tool, paid 402."""
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error, ssl

REPO = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ)
env["MODELLENS_CONFIG"] = os.path.join(REPO, "config.test.json")
env["MODELLENS_DATA"] = os.path.join(REPO, "history.test.json")
env["X402_PORT"] = "8799"
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server_x402.py")], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2)

def req(method, path, body=None, headers=None):
    url = "http://127.0.0.1:8799" + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=30, context=ssl.create_default_context()) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()

def b64d(s):
    pad = "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(s + pad))

try:
    st, hd, body = req("GET", "/.well-known/x402")
    print("manifest:", st, json.loads(body).get("payTo"), "tools:", json.loads(body).get("tools"))

    st, hd, body = req("POST", "/mcp", {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}})
    print("initialize:", st, json.loads(body)["result"]["serverInfo"])

    st, hd, body = req("POST", "/mcp", {"jsonrpc":"2.0","id":2,"method":"tools/list"})
    tools = json.loads(body)["result"]["tools"]
    print("tools/list:", st, [(t["name"], t["x402Price"], bool(t.get("annotations"))) for t in tools])

    # free tool: list_models (real, hits being magpie over tailnet)
    st, hd, body = req("POST", "/mcp", {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_models","arguments":{"limit":"3"}}})
    txt = json.loads(body)["result"]["content"][0]["text"]
    print("list_models (free):", st, "->", txt[:120].replace("\n"," | "))

    # paid tool without payment -> expect 402 + PAYMENT-REQUIRED
    st, hd, body = req("POST", "/mcp", {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"probe","arguments":{"limit":"1"}}})
    pr = hd.get("PAYMENT-REQUIRED") or hd.get("Payment-required")
    print("probe unpaid: HTTP", st, "PAYMENT-REQUIRED present:", bool(pr))
    if pr:
        ch = b64d(pr)
        print("  challenge accepts[0]:", {k: ch["accepts"][0][k] for k in ("scheme","network","maxAmountRequired","payTo","asset")})
        print("  jsonrpc error code:", json.loads(body)["error"]["code"])
finally:
    proc.terminate()
    print("server stopped")