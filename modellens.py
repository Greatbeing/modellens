#!/usr/bin/env python3
"""
ModelLens — multi-provider model quality, visible.

An MCP server (stdio transport, JSON-RPC 2.0) that turns any OpenAI-compatible
gateway into an observable, rankable set of models: it discovers what a provider
actually serves, probes what works right now (latency, success, streaming, tool
calling), keeps the history, and answers "which model should I use?".

Zero third-party dependencies: Python 3.9+ standard library only.

Tools
-----
  list_models   what each endpoint advertises
  probe         probe models now (latency / success / streaming / tools)
  ranking       quality ranking built from real call history
  recommend     best model for a task profile (fast | cheap | capable)
  report        the whole picture as markdown

Usage
-----
  python3 modellens.py [--config config.json] [--data history.json]

config.json:
  {"endpoints": [{"name": "magpie", "base_url": "http://127.0.0.1:3425/v1"}]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from statistics import median

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "modellens"
SERVER_VERSION = "0.1.0"

# How hard a probe pushes a model: enough to expose broken routing, cheap enough
# to run often. A probe is a real request, so it costs a few tokens.
PROBE_PROMPT = "Reply with exactly: OK"
PROBE_MAX_TOKENS = 512
PROBE_TIMEOUT = 90

# Scoring weights for the 0-100 quality score.
W_SUCCESS = 50.0
W_LATENCY = 30.0
W_CANCEL = 20.0
LATENCY_REFERENCE_MS = 8000.0  # a median at or above this scores 0 on latency


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _post(url: str, payload: dict, timeout: float, headers: dict | None = None) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw), (time.time() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw), (time.time() - started) * 1000.0
    except Exception:
        return 0, {}, (time.time() - started) * 1000.0


def _get(url: str, timeout: float, headers: dict | None = None) -> tuple[int, dict, float]:
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw), (time.time() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw), (time.time() - started) * 1000.0
    except Exception:
        return 0, {}, (time.time() - started) * 1000.0


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw[:500]}


# --------------------------------------------------------------------------
# Endpoint / model discovery
# --------------------------------------------------------------------------

class Endpoint:
    def __init__(self, cfg: dict):
        self.name = cfg.get("name") or cfg.get("base_url", "endpoint")
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = cfg.get("api_key") or cfg.get("apiKey") or ""
        self.enabled = cfg.get("enabled", True)
        self.models_filter = cfg.get("models") or []

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    def headers(self) -> dict:
        # A gateway that listens on localhost usually accepts any key; sending a
        # placeholder keeps providers that require a header happy.
        return {"Authorization": f"Bearer {self.api_key or 'modellens'}"}


def discover_models(ep: Endpoint) -> list[str]:
    status, body, _ = _get(ep.models_url, timeout=20, headers=ep.headers())
    if status != 200 or not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str):
            ids.append(item)
    if ep.models_filter:
        ids = [i for i in ids if any(f in i for f in ep.models_filter)]
    return sorted(set(ids))


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe_model(ep: Endpoint, model: str, check_stream: bool = True) -> dict:
    """Run one real request. Cancelled/failed/timeout are all recorded."""
    out: dict = {
        "model": model,
        "endpoint": ep.name,
        "ok": False,
        "status": 0,
        "latency_ms": None,
        "stream_ok": None,
        "tools_ok": None,
        "error": None,
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    status, body, ms = _post(
        ep.chat_url,
        {"model": model,
         "messages": [{"role": "user", "content": PROBE_PROMPT}],
         "max_tokens": PROBE_MAX_TOKENS,
         "stream": False},
        timeout=PROBE_TIMEOUT,
        headers=ep.headers(),
    )
    out["status"] = status
    out["latency_ms"] = round(ms, 1)

    if status == 200 and isinstance(body, dict):
        out["ok"] = True
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        out["reply"] = (content or "")[:80] if isinstance(content, str) else None
    else:
        msg = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or "")
            elif err:
                msg = str(err)
        out["error"] = (msg or f"HTTP {status}")[:300]

    if check_stream and out["ok"]:
        s_status, s_body, _ = _post(
            ep.chat_url,
            {"model": model,
             "messages": [{"role": "user", "content": PROBE_PROMPT}],
             "max_tokens": 32,
             "stream": True},
            timeout=PROBE_TIMEOUT,
            headers=ep.headers(),
        )
        out["stream_ok"] = s_status == 200

    return out


def probe_tools(ep: Endpoint, model: str) -> bool | None:
    tool = {
        "type": "function",
        "function": {
            "name": "modellens_ping",
            "description": "Return the word pong.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    status, body, _ = _post(
        ep.chat_url,
        {"model": model,
         "messages": [{"role": "user", "content": "Call modellens_ping."}],
         "tools": [tool],
         "max_tokens": 256,
         "stream": False},
        timeout=PROBE_TIMEOUT,
        headers=ep.headers(),
    )
    if status != 200 or not isinstance(body, dict):
        return None
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = msg.get("tool_calls") or []
    return any(c.get("function", {}).get("name") == "modellens_ping" for c in calls)


# --------------------------------------------------------------------------
# History store
# --------------------------------------------------------------------------

class Store:
    """Append-only JSON store of probe results, bounded to the last N per model."""

    def __init__(self, path: str, keep: int = 200):
        self.path = path
        self.keep = keep
        self.rows: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.rows = list(data.get("rows") or [])
            elif isinstance(data, list):
                self.rows = data
        except Exception:
            self.rows = []

    def add(self, rows: list[dict]) -> None:
        self.rows.extend(rows)
        # Bound memory: keep the most recent N per (endpoint, model).
        seen: dict[str, list[dict]] = {}
        for row in reversed(self.rows):
            key = f"{row.get('endpoint')}/{row.get('model')}"
            bucket = seen.setdefault(key, [])
            if len(bucket) < self.keep:
                bucket.append(row)
        kept = []
        for bucket in seen.values():
            kept.extend(bucket)
        kept.sort(key=lambda r: r.get("probed_at") or "")
        self.rows = kept

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rows": self.rows}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def for_model(self, endpoint: str, model: str) -> list[dict]:
        return [r for r in self.rows
                if r.get("endpoint") == endpoint and r.get("model") == model]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def score_model(rows: list[dict]) -> dict:
    attempts = len(rows)
    oks = [r for r in rows if r.get("ok")]
    failures = attempts - len(oks)
    cancelled = len([r for r in rows if not r.get("ok") and r.get("status") in (0, 499)])
    latencies = [r["latency_ms"] for r in oks if isinstance(r.get("latency_ms"), (int, float))]

    success_rate = (len(oks) / attempts * 100.0) if attempts else 0.0
    cancel_rate = (cancelled / attempts * 100.0) if attempts else 0.0
    med = median(latencies) if latencies else None

    if attempts == 0:
        verdict = "untested"
    elif success_rate == 0 and all(r.get("status") == 404 for r in rows):
        verdict = "dead"
    elif success_rate == 0 and all(r.get("status") == 429 for r in rows):
        verdict = "quota_blocked"
    elif cancel_rate >= 30:
        verdict = "unreliable"
    elif success_rate >= 95 and (med or 9e9) < 3000:
        verdict = "healthy"
    else:
        verdict = "mixed"

    if attempts == 0 or success_rate == 0:
        # No evidence, or only failures: nothing to recommend. Keeping this at 0
        # stops a dead or quota-blocked model outranking a merely mediocre one.
        return {
            "attempts": attempts, "ok": 0, "failed": attempts,
            "cancelled": cancelled,
            "success_rate": 0.0, "cancel_rate": round(cancel_rate, 1),
            "median_latency_ms": None, "p95_latency_ms": None,
            "score": 0.0, "verdict": verdict,
        }

    latency_score = max(0.0, min(1.0, 1.0 - (med / LATENCY_REFERENCE_MS))) * 100.0
    score = (success_rate * W_SUCCESS + latency_score * W_LATENCY
             + max(0.0, 100.0 - cancel_rate * 2.0) * W_CANCEL) / (
        W_SUCCESS + W_LATENCY + W_CANCEL)

    return {
        "attempts": attempts,
        "ok": len(oks),
        "failed": failures,
        "cancelled": cancelled,
        "success_rate": round(success_rate, 1),
        "cancel_rate": round(cancel_rate, 1),
        "median_latency_ms": round(med, 1) if med is not None else None,
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1)
                            if len(latencies) >= 20 else None,
        "score": round(score, 1),
        "verdict": verdict,
    }


def build_ranking(store: Store, endpoints: list[Endpoint]) -> list[dict]:
    models: dict[str, dict] = {}
    for ep in endpoints:
        if not ep.enabled:
            continue
        for mid in discover_models(ep):
            key = f"{ep.name}/{mid}"
            models[key] = {"endpoint": ep.name, "model": mid, "id": key}
    out = []
    for key, info in models.items():
        stats = score_model(store.for_model(info["endpoint"], info["model"]))
        out.append({**info, **stats})
    out.sort(key=lambda r: (-r["score"], r["id"]))
    return out


PROFILE_HINTS = {
    "fast": "lowest median latency, success rate at least 90%",
    "cheap": "any provider that reports zero pricing; otherwise prefer open-weight",
    "capable": "highest success rate, then lowest latency",
}


def recommend(ranking: list[dict], profile: str) -> dict:
    profile = (profile or "capable").lower()
    usable = [r for r in ranking if r["score"] > 0 and r["verdict"] not in ("dead", "quota_blocked")]
    if not usable:
        return {"profile": profile, "picks": [], "note": "no model has a passing score yet"}
    if profile == "fast":
        usable.sort(key=lambda r: (r["median_latency_ms"] if r["median_latency_ms"] is not None else 1e12))
    elif profile == "cheap":
        usable.sort(key=lambda r: (-r["success_rate"], r["median_latency_ms"] or 1e12))
    else:
        usable.sort(key=lambda r: (-r["score"], r["median_latency_ms"] or 1e12))
    return {
        "profile": profile,
        "criteria": PROFILE_HINTS.get(profile, PROFILE_HINTS["capable"]),
        "picks": [
            {"rank": i + 1, "id": r["id"], "score": r["score"],
             "median_latency_ms": r["median_latency_ms"],
             "success_rate": r["success_rate"], "verdict": r["verdict"],
             "attempts": r["attempts"]}
            for i, r in enumerate(usable[:5])
        ],
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

class Context:
    def __init__(self, config_path: str, data_path: str):
        self.config_path = config_path
        self.data_path = data_path
        self.endpoints = load_config(config_path)
        self.store = Store(data_path)


def load_config(path: str) -> list[Endpoint]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        sys.stderr.write(f"modellens: cannot read config {path}: {exc}\n")
        return []
    raw = data.get("endpoints") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [Endpoint(e) for e in raw if isinstance(e, dict) and e.get("base_url")]


def tool_list_models(ctx: Context, args: dict) -> str:
    pattern = (args.get("contains") or "").strip().lower()
    limit = int(args.get("limit") or 0)
    lines = []
    for ep in ctx.endpoints:
        if not ep.enabled:
            continue
        models = discover_models(ep)
        if pattern:
            models = [m for m in models if pattern in m.lower()]
        lines.append(f"## {ep.name}  ({ep.base_url}) — {len(models)} models")
        for m in models[:limit or len(models)]:
            lines.append(f"- {m}")
        lines.append("")
    if not lines:
        return "No endpoints configured or reachable. Add one to config.json."
    return "\n".join(lines).rstrip()


def tool_probe(ctx: Context, args: dict) -> str:
    endpoint = (args.get("endpoint") or "").strip()
    model = (args.get("model") or "").strip()
    contains = (args.get("contains") or "").strip().lower()
    limit = int(args.get("limit") or 20)
    check_tools = bool(args.get("check_tools"))

    targets: list[tuple[Endpoint, str]] = []
    for ep in ctx.endpoints:
        if not ep.enabled:
            continue
        if endpoint and endpoint != ep.name:
            continue
        models = discover_models(ep)
        if model:
            models = [model] if model in models else []
        elif contains:
            models = [m for m in models if contains in m.lower()]
        targets.extend((ep, m) for m in models[:limit])

    if not targets:
        return "No models matched. Check endpoint name / model / contains."

    results = []
    for ep, m in targets:
        row = probe_model(ep, m)
        if check_tools and row["ok"]:
            row["tools_ok"] = probe_tools(ep, m)
        results.append(row)
        mark = "ok" if row["ok"] else f"FAIL({row['status']})"
        extra = f" tools={'yes' if row.get('tools_ok') else 'no'}" if check_tools else ""
        print(f"  {ep.name}/{m}: {mark} {row['latency_ms']}ms{extra}", file=sys.stderr)

    ctx.store.add(results)
    ctx.store.save()

    ok = sum(1 for r in results if r["ok"])
    lines = [f"Probed {len(results)} models: {ok} ok, {len(results) - ok} failed.", ""]
    for r in sorted(results, key=lambda x: (not x["ok"], x["latency_ms"] or 1e12)):
        line = f"- {r['endpoint']}/{r['model']}: " + (
            f"ok {r['latency_ms']}ms" if r["ok"] else f"FAIL http={r['status']} {r['error']}")
        if r.get("stream_ok") is not None:
            line += f" stream={'yes' if r['stream_ok'] else 'no'}"
        if r.get("tools_ok") is not None:
            line += f" tools={'yes' if r['tools_ok'] else 'no'}"
        lines.append(line)
    return "\n".join(lines)


def tool_ranking(ctx: Context, args: dict) -> str:
    ranking = build_ranking(ctx.store, ctx.endpoints)
    if not ranking:
        return "No models discovered. Run probe first."
    limit = int(args.get("limit") or 20)
    show_all = str(args.get("all", "")).lower() in ("1", "true", "yes")

    tested = [r for r in ranking if r["attempts"] > 0]
    untested = [r for r in ranking if r["attempts"] == 0]
    tested.sort(key=lambda r: (-r["score"], r["id"]))

    lines = [f"{'model':58s} {'score':>6} {'succ':>6} {'cancel':>7} {'median':>9} {'n':>4}  verdict"]
    for r in tested[:limit]:
        med = f"{r['median_latency_ms']:.0f}ms" if r["median_latency_ms"] is not None else "-"
        lines.append(f"{r['id']:58s} {r['score']:6.1f} {r['success_rate']:5.1f}% "
                     f"{r['cancel_rate']:6.1f}% {med:>9} {r['attempts']:4d}  {r['verdict']}")
    if show_all:
        lines.append("")
        lines.append(f"## Untested ({len(untested)})")
        for r in untested[:limit]:
            lines.append(f"- {r['id']}")
    else:
        lines.append("")
        lines.append(f"({len(untested)} more untested — pass all=true to list them)")
    return "\n".join(lines)


def tool_recommend(ctx: Context, args: dict) -> str:
    ranking = build_ranking(ctx.store, ctx.endpoints)
    res = recommend(ranking, args.get("profile") or "capable")
    if not res["picks"]:
        return res["note"]
    lines = [f"Profile: {res['profile']}  ({res['criteria']})", ""]
    for p in res["picks"]:
        med = f"{p['median_latency_ms']:.0f}ms" if p["median_latency_ms"] is not None else "-"
        lines.append(f"{p['rank']}. {p['id']} — score {p['score']}, "
                     f"success {p['success_rate']}%, median {med}, {p['verdict']} (n={p['attempts']})")
    return "\n".join(lines)


def tool_report(ctx: Context, args: dict) -> str:
    ranking = build_ranking(ctx.store, ctx.endpoints)
    if not ranking:
        return "No models discovered yet."
    total = len(ranking)
    verdicts: dict[str, int] = {}
    for r in ranking:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1

    lines = ["# ModelLens report", ""]
    lines.append(f"- endpoints: {len([e for e in ctx.endpoints if e.enabled])}")
    lines.append(f"- models discovered: {total}")
    lines.append(f"- probes recorded: {len(ctx.store.rows)}")
    lines.append("")
    lines.append("## Verdicts")
    for k in ("healthy", "mixed", "unreliable", "quota_blocked", "dead", "untested"):
        if verdicts.get(k):
            lines.append(f"- {k}: {verdicts[k]}")
    lines.append("")
    lines.append("## Top 15 by score")
    lines.append("")
    lines.append("| model | score | success | cancel | median | n | verdict |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in ranking[:15]:
        med = f"{r['median_latency_ms']:.0f}ms" if r["median_latency_ms"] is not None else "-"
        lines.append(f"| {r['id']} | {r['score']} | {r['success_rate']}% "
                     f"| {r['cancel_rate']}% | {med} | {r['attempts']} | {r['verdict']} |")
    return "\n".join(lines)


TOOLS = [
    ("list_models", "List the models each configured endpoint advertises.",
     {"contains": "optional substring filter", "limit": "optional max per endpoint"},
     tool_list_models),
    ("probe", "Probe models with real requests: latency, success, streaming, tool calls.",
     {"endpoint": "optional endpoint name", "model": "exact model id",
      "contains": "substring filter", "limit": "max models (default 20)",
      "check_tools": "also test function calling"},
     tool_probe),
    ("ranking", "Quality ranking built from recorded probe history.",
     {"limit": "rows to show (default 30)"},
     tool_ranking),
    ("recommend", "Best model for a task profile.",
     {"profile": "fast | cheap | capable"},
     tool_recommend),
    ("report", "The whole picture as markdown.", {}, tool_report),
]


# --------------------------------------------------------------------------
# MCP server (stdio, newline-delimited JSON-RPC)
# --------------------------------------------------------------------------

def handle(ctx: Context, msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return ok(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "ModelLens: discover, probe, rank and recommend models "
                            "across OpenAI-compatible endpoints.",
        })
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return ok(msg_id, {})
    if method == "tools/list":
        return ok(msg_id, {"tools": [
            {"name": n, "description": d,
             "inputSchema": {
                 "type": "object",
                 "properties": {k: {"type": "string", "description": v}
                                for k, v in spec.items()},
                 "required": [],
             }}
            for n, d, spec, _ in TOOLS
        ]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        for n, _, _, fn in TOOLS:
            if n == name:
                try:
                    text = fn(ctx, args)
                except Exception as exc:
                    return ok(msg_id, {"content": [{"type": "text",
                                                     "text": f"tool error: {exc}"}],
                                      "isError": True})
                return ok(msg_id, {"content": [{"type": "text", "text": text}]})
        return err(msg_id, -32602, f"unknown tool: {name}")
    if method in ("resources/list", "prompts/list"):
        return ok(msg_id, {method.split("/")[0]: []})
    if msg_id is not None:
        return err(msg_id, -32601, f"method not found: {method}")
    return None


def ok(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def serve(ctx: Context) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            sys.stderr.write("modellens: ignoring non-JSON line\n")
            continue
        try:
            resp = handle(ctx, msg)
        except Exception as exc:
            resp = err(msg.get("id"), -32603, f"internal error: {exc}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def run_once(ctx: Context, tool: str, args: dict) -> int:
    for name, _, _, fn in TOOLS:
        if name == tool:
            print(fn(ctx, args))
            return 0
    sys.stderr.write(f"modellens: unknown tool: {tool} "
                     f"(one of {', '.join(n for n, _, _, _ in TOOLS)})\n")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="ModelLens MCP server")
    ap.add_argument("--config", default=os.environ.get("MODELLENS_CONFIG", ""),
                    help="path to config.json")
    ap.add_argument("--data", default=os.environ.get("MODELLENS_DATA", ""),
                    help="path to the probe history JSON")
    ap.add_argument("--once", metavar="TOOL",
                    help="run one tool against the CLI and exit "
                         f"({', '.join(n for n, _, _, _ in TOOLS)})")
    args = ap.parse_args()

    if args.config and not os.path.exists(args.config):
        sys.stderr.write(f"modellens: config not found: {args.config}\n")
    ctx = Context(args.config, args.data)
    if not ctx.endpoints:
        sys.stderr.write("modellens: no endpoints configured; "
                         "write config.json or set MODELLENS_CONFIG\n")
    if args.once:
        return run_once(ctx, args.once, {})
    return serve(ctx)


if __name__ == "__main__":
    raise SystemExit(main())