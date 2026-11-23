#!/usr/bin/env python3
"""ModelLens proxy — an OpenAI-compatible gateway that routes on evidence.

Drop it in front of your existing gateway and send `model: "auto"` (or
`auto:fast` / `auto:cheap` / `auto:capable`): the proxy picks the model from
what ModelLens actually measured (success rate, latency, verdicts) instead of
whatever the gateway happens to list first. A real model id is passed through
untouched, so existing clients keep working unchanged.

    client ──POST /v1/chat/completions──▶ proxy ──▶ endpoint A / model X
                                          │      └▶ endpoint B / model Y (fallback)
                                          ├─ routing table, rebuilt every TTL
                                          └─ outcomes written back to history

Design notes (see README "Proxy"):
  * the routing table is built off the request path; `build_ranking()` does a
    live GET /models per endpoint, which must never run per request;
  * ordering reuses modellens.order_for_profile(), so `recommend` and the proxy
    can never disagree about "fastest" or "cheapest";
  * failover only happens BEFORE the first byte is written to the client —
    retrying after that would duplicate output and duplicate the token bill;
  * pure winner-takes-all would starve the runner-up until it goes stale and
    disappears exactly when it is needed, so a little exploration and a
    scheduled probe keep every candidate measured.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import http.client
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modellens as ml  # noqa: E402

MAX_BODY = 16 * 1024 * 1024      # chat bodies can carry images; still bounded
MAX_RESPONSE = 64 * 1024 * 1024  # non-stream response cap

# Routing-eligible verdicts. Everything else means "do not send a user here".
ROUTABLE_VERDICTS = ("healthy", "mixed", "unreliable")

AUTO_ALIASES = ("auto", "auto:fast", "auto:cheap", "auto:capable", "auto:balanced")
PROFILES = ("fast", "cheap", "capable", "balanced")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers":
        "Content-Type, Authorization, X-Modellens-Profile, X-Modellens-Session, "
        "X-Modellens-Dry-Run, X-Modellens-Model, Idempotency-Key",
    "Access-Control-Expose-Headers":
        "X-Modellens-Model, X-Modellens-Endpoint, X-Modellens-Profile, "
        "X-Modellens-Reason, X-Modellens-Attempts, X-Modellens-Score, "
        "X-Modellens-Evidence, X-Modellens-Fallback-From, X-Modellens-Degraded",
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

class ProxyConfig:
    """The `proxy` section of config.json, with defaults that are safe to run."""

    def __init__(self, raw: dict | None):
        raw = raw or {}
        self.listen = str(raw.get("listen") or "127.0.0.1:8766")
        self.profile = (raw.get("profile") or "capable").lower()
        self.table_ttl_s = float(raw.get("table_ttl_s", 30))
        self.discovery_ttl_s = float(raw.get("discovery_ttl_s", 300))
        self.discovery_timeout_s = float(raw.get("discovery_timeout_s", 5))
        self.max_attempts = max(1, int(raw.get("max_attempts", 3)))
        self.attempt_timeout_s = float(raw.get("attempt_timeout_s", 60))
        self.stream_timeout_s = float(raw.get("stream_timeout_s", 300))
        self.explore_ratio = max(0.0, min(1.0, float(raw.get("explore_ratio", 0.05))))
        self.explore_top_k = max(1, int(raw.get("explore_top_k", 3)))
        self.explore_min_success = float(raw.get("explore_min_success", 85))
        self.sticky_ttl_s = float(raw.get("sticky_ttl_s", 1800))
        self.allow_stale_fallback = bool(raw.get("allow_stale_fallback", False))
        self.permissive_caps = bool(raw.get("permissive_caps", False))
        self.min_probe_rows = max(0, int(raw.get("min_probe_rows", 3)))
        self.deny = [str(p) for p in (raw.get("deny") or [])]
        self.allow = [str(p) for p in (raw.get("allow") or [])]
        self.admin_token = str(raw.get("admin_token") or "")
        self.forward_client_auth = bool(raw.get("forward_client_auth", True))
        probe = raw.get("probe") or {}
        self.probe_enabled = bool(probe.get("enabled", False))
        self.probe_interval_s = float(probe.get("interval_s", 900))
        self.probe_limit = max(1, int(probe.get("limit", 3)))
        self.probe_check_stream = bool(probe.get("check_stream", True))
        breaker = raw.get("breaker") or {}
        self.breaker_threshold = max(1, int(breaker.get("fail_threshold", 3)))
        self.breaker_open_s = float(breaker.get("open_s", 30))


def load_proxy_config(path: str) -> tuple[ProxyConfig, list[ml.Endpoint]]:
    raw = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:
            sys.stderr.write(f"modellens-proxy: cannot read config {path}: {exc}\n")
            raw = {}
    section = raw.get("proxy") if isinstance(raw, dict) else None
    return ProxyConfig(section if isinstance(section, dict) else {}), ml.load_config(path)


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def _newest_known(rows: list[dict], key: str):
    """Newest non-None value of a probe field, or None if never measured."""
    best_ts, best_val = None, None
    for row in rows:
        val = row.get(key)
        if val is None:
            continue
        ts = ml._parse_ts(row.get("probed_at"))
        if best_ts is None or (ts is not None and ts >= best_ts):
            if ts is not None:
                best_ts = ts
            best_val = val
    return best_val


def resolve_caps(rows: list[dict], info: dict, overrides: dict) -> dict:
    """Three-state capabilities: True / False / None (unknown).

    Unknown must never become False: that would silently gate out a model that
    works, and the operator would never learn why.
    """
    o = overrides or {}
    caps = {
        "stream": _newest_known(rows, "stream_ok"),
        "tools": _newest_known(rows, "tools_ok"),
        "json": None,
        "vision": info.get("vision"),
        "ctx": info.get("context_length"),
    }
    for key in ("stream", "tools", "json", "vision", "ctx"):
        if key in o and o[key] is not None:
            caps[key] = o[key]
    return caps


# --------------------------------------------------------------------------
# Candidates and the routing table
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Cand:
    endpoint: str
    model: str
    score: float
    verdict: str
    success_rate: float
    median_ms: float | None
    ttft_ms: float | None
    attempts: int
    age_hours: float | None
    pricing: dict
    caps: dict
    probe_rows: int
    proxy_rows: int

    @property
    def id(self) -> str:
        return f"{self.endpoint}/{self.model}"

    def as_row(self) -> dict:
        """The shape modellens.order_for_profile() expects."""
        return {
            "id": self.id, "endpoint": self.endpoint, "model": self.model,
            "score": self.score, "verdict": self.verdict,
            "success_rate": self.success_rate,
            "median_latency_ms": self.median_ms,
            "attempts": self.attempts, "age_hours": self.age_hours,
            "pricing": self.pricing,
        }

    def to_json(self) -> dict:
        return {**self.as_row(), "ttft_ms": self.ttft_ms, "caps": self.caps,
                "evidence": {"probe": self.probe_rows, "proxy": self.proxy_rows}}


class Table:
    """Immutable snapshot of what may be routed to, ordered per profile."""

    def __init__(self, built_at: float, candidates: list[Cand],
                 ordered: dict[str, list[Cand]], degraded: dict[str, bool],
                 notes: dict[str, str | None], skipped: list[dict]):
        self.built_at = built_at
        self.candidates = candidates
        self._ordered = ordered
        self.degraded = degraded
        self.notes = notes
        self.skipped = skipped          # excluded before ordering, with reason

    def ordered(self, profile: str) -> list[Cand]:
        return self._ordered.get(profile) or self._ordered.get("capable") or []

    def by_id(self, cid: str) -> Cand | None:
        for cand in self.candidates:
            if cand.id == cid:
                return cand
        return None

    def verdict_counts(self) -> dict:
        counts: dict[str, int] = {}
        for cand in self.candidates:
            counts[cand.verdict] = counts.get(cand.verdict, 0) + 1
        return counts


class Discovery:
    """TTL cache for /models, so N requests never mean N discovery calls."""

    def __init__(self, cfg: ProxyConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.calls = 0

    def models(self, ep: ml.Endpoint) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(ep.name)
            if hit and now - hit[0] < self.cfg.discovery_ttl_s:
                return hit[1]
        found = ml.discover_models_detailed(ep, timeout=self.cfg.discovery_timeout_s)
        with self._lock:
            self.calls += 1
            self._cache[ep.name] = (now, found)
        return found

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()


def _order_balanced(candidates: list[Cand]) -> list[Cand]:
    # Score per second of median latency: a 90-score model at 2 s beats a
    # 95-score model at 10 s for interactive use.
    return sorted(candidates,
                  key=lambda c: (-(c.score / max(1.0, c.median_ms or 1000.0)), c.id))


def build_table(ctx, cfg: ProxyConfig, discovery: Discovery,
                now: float | None = None) -> Table:
    """Score every advertised model, then order it per profile. Off the request path."""
    now = time.time() if now is None else now
    ctx.store.refresh()
    candidates: list[Cand] = []
    skipped: list[dict] = []
    for ep in ctx.endpoints:
        if not ep.enabled:
            continue
        for info in discovery.models(ep):
            mid = info["id"]
            rows = ctx.store.for_model(ep.name, mid)
            stats = ml.score_model(rows, now=now)
            overrides = (getattr(ep, "capabilities", {}) or {}).get(mid, {})
            cand = Cand(
                endpoint=ep.name, model=mid,
                score=stats["score"], verdict=stats["verdict"],
                success_rate=stats["success_rate"],
                median_ms=stats["median_latency_ms"],
                ttft_ms=_newest_known(rows, "ttft_ms"),
                attempts=stats["attempts"], age_hours=stats["age_hours"],
                pricing=info.get("pricing") or {},
                caps=resolve_caps(rows, info, overrides),
                probe_rows=len([r for r in rows if r.get("source") != "proxy"]),
                proxy_rows=len([r for r in rows if r.get("source") == "proxy"]),
            )
            if stats["verdict"] not in ROUTABLE_VERDICTS:
                skipped.append({"id": cand.id, "verdict": cand.verdict,
                                "reason": f"verdict:{cand.verdict}"})
                continue
            candidates.append(cand)

    ordered: dict[str, list[Cand]] = {"balanced": _order_balanced(candidates)}
    degraded: dict[str, bool] = {"balanced": False}
    notes: dict[str, str | None] = {"balanced": "score per second of median latency"}
    for profile in ("fast", "cheap", "capable"):
        rows = [c.as_row() for c in candidates]
        ordered_rows, note = ml.order_for_profile(rows, profile)
        by_id = {c.id: c for c in candidates}
        if ordered_rows:
            ordered[profile] = [by_id[r["id"]] for r in ordered_rows if r["id"] in by_id]
            degraded[profile] = False
        else:
            # `fast` with nothing above the success bar: still route, but say so
            # in the response headers so nobody believes it is fast by evidence.
            fallback = sorted(rows, key=ml._latency_key)
            ordered[profile] = [by_id[r["id"]] for r in fallback if r["id"] in by_id]
            # Nothing at all is not "degraded": there is simply no evidence yet.
            degraded[profile] = bool(candidates)
        notes[profile] = note
    return Table(now, candidates, ordered, degraded, notes, skipped)


# --------------------------------------------------------------------------
# Request → need → candidate ladder (pure, unit-tested)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Need:
    stream: bool = False
    tools: bool = False
    json_mode: bool = False
    vision: bool = False
    ctx: int = 0


def estimate_tokens(body: dict) -> int:
    """Cheap context estimate: characters / 3.5 + reply headroom."""
    total = 0
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
    return int(total / 3.5) + 256


def need_from_body(body: dict) -> Need:
    tools = bool(body.get("tools") or body.get("tool_choice") or body.get("functions"))
    vision = False
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and str(part.get("type", "")).lower() in (
                        "image_url", "input_image", "image"):
                    vision = True
    return Need(stream=bool(body.get("stream")), tools=tools,
                json_mode="response_format" in body, vision=vision,
                ctx=estimate_tokens(body))


def resolve_profile(model_field, header_profile, default: str) -> tuple[str, str | None]:
    """Returns (profile, pinned_model_id). A pinned id wins over any profile."""
    if header_profile:
        p = str(header_profile).strip().lower()
        if p in PROFILES:
            return p, None
    model = str(model_field or "").strip()
    if not model:
        return default, None
    if model.startswith("auto:"):
        p = model.split(":", 1)[1].strip().lower()
        return (p if p in PROFILES else default), None
    if model == "auto":
        return default, None
    return default, model          # a real model id is a pin, not a request to route


def gate_reason(cand: Cand, need: Need, cfg: ProxyConfig) -> str | None:
    """Why this candidate must not serve this request (None = it may)."""
    if cand.verdict == "stale":
        # Staleness is not "broken", so it deserves its own reason and its own
        # opt-in (allow_stale_fallback) rather than being lumped with dead.
        if not cfg.allow_stale_fallback:
            return "stale"
    elif cand.verdict not in ROUTABLE_VERDICTS:
        return f"verdict:{cand.verdict}"
    if need.stream and cand.caps.get("stream") is False:
        return "no_stream"
    if need.tools and cand.caps.get("tools") is not True and not cfg.permissive_caps:
        return "tools_unverified"
    if need.json_mode and cand.caps.get("json") is not True and not cfg.permissive_caps:
        return "json_unverified"
    if need.vision and cand.caps.get("vision") is not True and not cfg.permissive_caps:
        return "vision_unverified"
    ctx = cand.caps.get("ctx")
    if need.ctx and isinstance(ctx, int) and ctx > 0 and need.ctx > ctx:
        return "context_too_small"
    if not _allowed(cand.model, cfg):
        return "denied"
    return None


def _allowed(model: str, cfg: ProxyConfig) -> bool:
    if cfg.allow and not any(fnmatch.fnmatch(model, pat) for pat in cfg.allow):
        return False
    if any(fnmatch.fnmatch(model, pat) for pat in cfg.deny):
        return False
    return True


class Breaker:
    """Per-candidate fast-fail state: history is minutes, this is seconds."""

    def __init__(self, cfg: ProxyConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def is_open(self, cid: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            st = self._state.get(cid)
            if not st or st["opens_at"] is None:
                return False
            if now >= st["opens_at"]:
                st["opens_at"] = None          # half-open: allow one trial
                st["fails"] = 0
                return False
            return True

    def record(self, cid: str, ok: bool, retryable: bool = True,
               now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            st = self._state.setdefault(cid, {"fails": 0, "opens_at": None})
            if ok:
                st["fails"] = 0
                st["opens_at"] = None
                return
            if not retryable:
                return
            st["fails"] += 1
            if st["fails"] >= self.cfg.breaker_threshold:
                st["opens_at"] = now + self.cfg.breaker_open_s

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            return {cid: {"fails": st["fails"],
                          "open_for_s": round(st["opens_at"] - now, 1)}
                    for cid, st in self._state.items() if st["opens_at"] is not None}


class Sticky:
    """Keep a conversation on one model: switching mid-chat costs quality."""

    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._map: dict[str, tuple[str, float]] = {}

    def get(self, key: str | None, now: float | None = None) -> str | None:
        if not key:
            return None
        now = time.time() if now is None else now
        with self._lock:
            hit = self._map.get(key)
            if not hit:
                return None
            cid, expires = hit
            if expires < now:
                self._map.pop(key, None)
                return None
            return cid

    def set(self, key: str | None, cid: str, now: float | None = None) -> None:
        if not key:
            return
        now = time.time() if now is None else now
        with self._lock:
            if len(self._map) > 4096:                 # bound memory
                for k, (_, exp) in list(self._map.items()):
                    if exp < now:
                        self._map.pop(k, None)
            self._map[key] = (cid, now + self.ttl_s)

    def __len__(self) -> int:
        return len(self._map)


def session_key(headers, body: dict) -> str | None:
    explicit = headers.get("X-Modellens-Session") if headers is not None else None
    if explicit:
        return hashlib.sha256(str(explicit).encode()).hexdigest()[:16]
    msgs = body.get("messages") or []
    seed = ""
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("role") in ("system", "developer"):
            seed += str(msg.get("content") or "")
            break
    if not seed and msgs and isinstance(msgs[0], dict):
        seed = str(msgs[0].get("content") or "")[:64]
    return hashlib.sha256(seed.encode()).hexdigest()[:16] if seed else None


def explore_pick(cands: list[Cand], cfg: ProxyConfig, roll: float) -> Cand | None:
    """Deterministic exploration: only from the top-K and only above the bar."""
    if not cands or cfg.explore_ratio <= 0 or roll >= cfg.explore_ratio:
        return None
    pool = [c for c in cands[:cfg.explore_top_k]
            if c.success_rate >= cfg.explore_min_success]
    if len(pool) < 2:
        return None
    return min(pool, key=lambda c: (c.probe_rows, c.attempts, c.id))


def build_ladder(table: Table, cfg: ProxyConfig, profile: str, need: Need,
                 breaker: Breaker, sticky: Sticky, sticky_key: str | None,
                 roll: float, now: float | None = None) -> dict:
    """Ordered attempt list + the reason, ready to serialize for /explain."""
    now = time.time() if now is None else now
    eligible: list[Cand] = []
    skipped: list[dict] = []
    for cand in table.ordered(profile):
        reason = gate_reason(cand, need, cfg)
        if reason:
            skipped.append({"id": cand.id, "reason": reason})
            continue
        if breaker.is_open(cand.id, now):
            skipped.append({"id": cand.id, "reason": "breaker_open"})
            continue
        eligible.append(cand)

    ladder: list[Cand] = []
    reason = "no_candidate" if not eligible else "best"
    if eligible:
        pinned = sticky.get(sticky_key, now)
        sticky_cand = next((c for c in eligible if c.id == pinned), None)
        if sticky_cand is not None:
            ladder.append(sticky_cand)
            reason = "sticky"
        else:
            explore = explore_pick(eligible, cfg, roll)
            ladder.append(explore or eligible[0])
            reason = ("explore" if explore
                      else ("best" if len(eligible) > 1 else "only_candidate"))
        ladder.extend(c for c in eligible if c.id != ladder[0].id)

    return {
        "profile": profile,
        "reason": reason,
        "degraded": bool(table.degraded.get(profile)),
        "note": table.notes.get(profile),
        "ladder": ladder[:cfg.max_attempts],
        "skipped": skipped,
        "eligible": len(eligible),
        "need": {"stream": need.stream, "tools": need.tools,
                 "json": need.json_mode, "vision": need.vision,
                 "ctx": need.ctx},
    }


# --------------------------------------------------------------------------
# Upstream transport
# --------------------------------------------------------------------------

@dataclass
class Upstream:
    conn: http.client.HTTPConnection | None
    resp: http.client.HTTPResponse | None
    model: str
    status: int
    opened_ms: float
    error: str | None = None
    error_type: str | None = None


def _conn_for(ep: ml.Endpoint, timeout: float):
    parts = urllib.parse.urlsplit(ep.base_url)
    path = (parts.path or "").rstrip("/") + "/chat/completions"
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=timeout,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    return conn, path


def open_upstream(ep: ml.Endpoint, model: str, payload: bytes, timeout: float,
                  client_auth: str | None, cfg: ProxyConfig) -> Upstream:
    """POST upstream. Always returns a handle; transport failure lands in .error."""
    started = time.monotonic()
    conn = None
    try:
        conn, path = _conn_for(ep, timeout)
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream, application/json"}
        auth = client_auth if (client_auth and cfg.forward_client_auth) else None
        headers["Authorization"] = auth or f"Bearer {ep.api_key or 'modellens'}"
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        return Upstream(conn, resp, model, resp.status,
                        (time.monotonic() - started) * 1000.0)
    except Exception as exc:
        err_type, _ = ml._classify_network_error(exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return Upstream(conn, None, model, 0,
                        (time.monotonic() - started) * 1000.0,
                        error=str(exc)[:300], error_type=err_type)


def read_chunk(resp: http.client.HTTPResponse, n: int = 8192) -> bytes:
    """One socket read's worth of data.

    `resp.read(n)` would block until n bytes or EOF (BufferedReader semantics),
    which turns a token-by-token SSE stream into one big delayed burst.
    """
    reader = getattr(resp, "read1", None)
    if reader is not None:
        return reader(n)
    return resp.read(n)


def close_upstream(up: Upstream | None) -> None:
    if up is None:
        return
    for closer in (up.resp, up.conn):
        try:
            if closer is not None:
                closer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Recording (off the request path)
# --------------------------------------------------------------------------

class Recorder:
    """Batch history writes: a file lock + fsync per request is too slow."""

    def __init__(self, store, flush_s: float = 2.0, batch: int = 50,
                 synchronous: bool = False):
        self.store = store
        self.flush_s = flush_s
        self.batch = batch
        self.synchronous = synchronous
        self.written = 0
        self.errors = 0
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="modellens-recorder",
                                        daemon=True)

    def start(self) -> None:
        if self.synchronous:
            return
        self._thread.start()

    def record(self, row: dict | None) -> None:
        if self.store is None or not row:
            return
        if self.synchronous:
            self._flush([row])
            return
        self._q.put(row)

    def _run(self) -> None:
        pending: list[dict] = []
        last = time.monotonic()
        while True:
            stop = self._stop.is_set()
            if not pending and stop and self._q.empty():
                return
            timeout = max(0.02, self.flush_s - (time.monotonic() - last))
            try:
                pending.append(self._q.get(timeout=timeout))
            except queue.Empty:
                pass
            while len(pending) < self.batch:
                try:
                    pending.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if pending and (len(pending) >= self.batch
                            or time.monotonic() - last >= self.flush_s
                            or stop):
                self._flush(pending)
                pending = []
                last = time.monotonic()

    def _flush(self, rows: list[dict]) -> None:
        try:
            self.store.append(rows)
            self.written += len(rows)
        except Exception as exc:          # never kill the recorder thread
            self.errors += 1
            sys.stderr.write(f"modellens-proxy: could not record outcomes: {exc}\n")

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------
# Background refresher / prober
# --------------------------------------------------------------------------

class Refresher(threading.Thread):
    def __init__(self, app):
        super().__init__(name="modellens-table", daemon=True)
        self.app = app
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.app.cfg.table_ttl_s):
            try:
                self.app.rebuild_table()
            except Exception as exc:
                sys.stderr.write(f"modellens-proxy: table rebuild failed: {exc}\n")

    def stop(self) -> None:
        self._stop.set()


class Prober(threading.Thread):
    """Keep the runner-ups measured; without this the pool collapses to one model."""

    def __init__(self, app):
        super().__init__(name="modellens-prober", daemon=True)
        self.app = app
        self._stop = threading.Event()
        self.last_run: float | None = None
        self._run_lock = threading.Lock()

    def run(self) -> None:
        while not self._stop.wait(self.app.cfg.probe_interval_s):
            if self.app.cfg.probe_enabled:
                self.probe_once()

    def probe_once(self) -> list[dict]:
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            table = self.app.table()
            target = sorted(table.candidates,
                            key=lambda c: (-(c.age_hours or 1e9), c.probe_rows, c.id)
                            )[:self.app.cfg.probe_limit]
            by_name = {ep.name: ep for ep in self.app.ctx.endpoints}
            results = []
            for cand in target:
                ep = by_name.get(cand.endpoint)
                if ep is None:
                    continue
                row = ml.probe_model(ep, cand.model,
                                     check_stream=self.app.cfg.probe_check_stream)
                row["source"] = "probe"
                results.append(row)
                self.app.record(row)
            self.last_run = time.time()
            if results:
                self.app.rebuild_table()
            return results
        finally:
            self._run_lock.release()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class App:
    def __init__(self, cfg: ProxyConfig, config_path: str, data_path: str,
                 endpoints: list[ml.Endpoint] | None = None,
                 synchronous_records: bool = False):
        self.cfg = cfg
        self.ctx = ml.Context(config_path, data_path)
        if endpoints is not None:
            self.ctx.endpoints = endpoints
        self.discovery = Discovery(cfg)
        self.breaker = Breaker(cfg)
        self.sticky = Sticky(cfg.sticky_ttl_s)
        self.recorder = Recorder(self.ctx.store, synchronous=synchronous_records)
        self._table: Table | None = None
        self._table_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._counters_lock = threading.Lock()
        self._roll_counter = 0
        self.counters = {
            "requests": 0, "routed": 0, "pinned": 0, "dry_run": 0,
            "failovers": 0, "errors": 0, "by_reason": {}, "by_model": {},
        }
        self.refresher = Refresher(self)
        self.prober = Prober(self)

    # -- table ----------------------------------------------------------
    def rebuild_table(self) -> Table:
        with self._build_lock:                 # one builder at a time
            table = build_table(self.ctx, self.cfg, self.discovery)
            with self._table_lock:
                self._table = table
            return table

    def table(self) -> Table:
        with self._table_lock:
            table = self._table
        return table if table is not None else self.rebuild_table()

    def start(self) -> None:
        self.rebuild_table()
        self.recorder.start()
        self.refresher.start()
        if self.cfg.probe_enabled:
            self.prober.start()

    def stop(self) -> None:
        self.refresher.stop()
        self.prober.stop()
        self.recorder.close()

    # -- counters -------------------------------------------------------
    def bump(self, key: str, model: str | None = None, reason: str | None = None) -> None:
        with self._counters_lock:
            self.counters[key] = self.counters.get(key, 0) + 1
            if model:
                self.counters["by_model"][model] = \
                    self.counters["by_model"].get(model, 0) + 1
            if reason:
                self.counters["by_reason"][reason] = \
                    self.counters["by_reason"].get(reason, 0) + 1

    def snapshot_counters(self) -> dict:
        with self._counters_lock:
            return json.loads(json.dumps(self.counters))

    def next_roll(self, seed: str) -> float:
        """A 0..1 draw that is stable per body but varies across identical ones.

        Without the counter, the same prompt would always (or never) explore.
        """
        with self._counters_lock:
            self._roll_counter += 1
            n = self._roll_counter
        digest = hashlib.sha256(f"{seed}|{n}".encode()).hexdigest()
        return int(digest[:8], 16) / float(0xFFFFFFFF)

    def record(self, row: dict | None) -> None:
        self.recorder.record(row)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

def _json_error(message: str, err_type: str, code: int) -> dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "modellens-proxy"
    app: App  # bound by make_handler()

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *args):     # one compact line, never the body
        if os.environ.get("MODELLENS_PROXY_QUIET"):
            return
        sys.stderr.write("modellens-proxy: " + (fmt % args) + "\n")

    def _headers_out(self, extra: dict | None = None) -> dict:
        out = dict(CORS)
        out.update(extra or {})
        return out

    def _send_json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._headers_out(extra).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _authorized(self) -> bool:
        token = self.app.cfg.admin_token
        if not token:
            return True
        got = self.headers.get("X-Modellens-Token") or ""
        if not got:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                got = auth[7:]
        return got == token

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0 or length > MAX_BODY:
            return b""
        return self.rfile.read(length)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in self._headers_out().items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- GET ------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        admin = path.startswith("/modellens/")
        if admin and not self._authorized():
            return self._send_json(401, _json_error("unauthorized", "auth", 401))
        if path == "/modellens/table":
            return self._table_json()
        if path == "/modellens/health":
            table = self.app.table()
            return self._send_json(200, {
                "ok": True,
                "table_age_s": round(time.time() - table.built_at, 1),
                "candidates": len(table.candidates),
                "verdicts": table.verdict_counts(),
                "breakers": self.app.breaker.snapshot(),
                "sticky_sessions": len(self.app.sticky),
                "counters": self.app.snapshot_counters(),
            })
        if path == "/modellens/metrics":
            return self._metrics()
        if path == "/modellens/explain":
            return self._explain(body=b"")
        if path == "/v1/models":
            return self._models_json()
        return self._send_json(404, _json_error("not found", "not_found", 404))

    # -- POST -----------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/modellens/explain", "/modellens/probe") and not self._authorized():
            return self._send_json(401, _json_error("unauthorized", "auth", 401))
        if path == "/modellens/explain":
            return self._explain(body=self._body())
        if path == "/modellens/probe":
            rows = self.app.prober.probe_once()
            return self._send_json(200, {
                "probed": len(rows),
                "ids": [f"{r.get('endpoint')}/{r.get('model')}" for r in rows]})
        if path != "/v1/chat/completions":
            return self._send_json(404, _json_error("not found", "not_found", 404))
        return self._chat()

    # -- /v1/chat/completions -------------------------------------------
    def _chat(self):
        raw = self._body()
        if not raw:
            return self._send_json(413, _json_error(
                "empty body or over the size limit", "invalid_request", 413))
        try:
            body = json.loads(raw)
        except Exception:
            return self._send_json(400, _json_error("invalid JSON", "invalid_request", 400))
        if not isinstance(body, dict):
            return self._send_json(400, _json_error(
                "request body must be an object", "invalid_request", 400))

        self.app.bump("requests")
        need = need_from_body(body)
        profile, pinned = resolve_profile(body.get("model"),
                                          self.headers.get("X-Modellens-Profile"),
                                          self.app.cfg.profile)
        table = self.app.table()
        if pinned:
            decision = self._pinned_decision(table, pinned)
            if decision is None:
                self.app.bump("errors", reason="unknown_pin")
                return self._send_json(503, _json_error(
                    f"model {pinned!r} is not in the routing table",
                    "model_unavailable", 503))
        else:
            decision = build_ladder(
                table, self.app.cfg, profile, need, self.app.breaker,
                self.app.sticky, session_key(self.headers, body),
                roll=self.app.next_roll(raw[:2000].decode("utf-8", "replace")))

        if self._truthy(self.headers.get("X-Modellens-Dry-Run")):
            self.app.bump("dry_run")
            return self._send_json(200, self._decision_json(decision, table))

        if not decision["ladder"]:
            self.app.bump("errors", reason="no_candidate")
            return self._send_json(503, _json_error(
                self._no_candidate_message(decision), "no_candidate", 503),
                {"Retry-After": "30"})

        self.app.bump("pinned" if pinned else "routed")
        return self._forward(body, decision, need)

    def _pinned_decision(self, table: Table, model_id: str) -> dict | None:
        cand = self._candidate_for(table, model_id)
        if cand is None:
            return None
        return {"profile": "pinned", "reason": "pinned", "degraded": False,
                "note": None, "ladder": [cand], "skipped": [], "eligible": 1,
                "need": {}}

    def _candidate_for(self, table: Table, model_id: str) -> Cand | None:
        cid = table.by_id(model_id)
        if cid is not None:
            return cid
        matches = [c for c in table.candidates if c.model == model_id]
        if not matches:
            return None
        if len(matches) > 1:      # same model on several endpoints: healthiest
            return max(matches, key=lambda c: (c.score, -(c.median_ms or 1e12)))
        return matches[0]

    @staticmethod
    def _truthy(val) -> bool:
        return str(val or "").strip().lower() in ("1", "true", "yes", "on")

    def _no_candidate_message(self, decision: dict) -> str:
        reasons: dict[str, int] = {}
        for skip in decision["skipped"]:
            reasons[skip["reason"]] = reasons.get(skip["reason"], 0) + 1
        detail = ", ".join(f"{k}x{v}" for k, v in sorted(reasons.items())) \
            or "no models advertised"
        return (f"no model can serve this request ({detail}). Run `probe` "
                f"(with check_tools for tool calls) or declare capabilities in "
                f"config.json.")

    def _forward(self, body: dict, decision: dict, need: Need):
        ladder: list[Cand] = decision["ladder"]
        sticky_key = session_key(self.headers, body)
        client_auth = self.headers.get("Authorization")
        attempts: list[dict] = []
        fallback_from: str | None = None
        timeout = (self.app.cfg.stream_timeout_s if need.stream
                   else self.app.cfg.attempt_timeout_s)

        for cand in ladder:
            try:
                ep = self._endpoint(cand.endpoint)
            except KeyError:
                continue
            payload = json.dumps({**body, "model": cand.model}).encode()
            up = open_upstream(ep, cand.model, payload, timeout, client_auth,
                               self.app.cfg)
            attempts.append({"id": cand.id, "status": up.status,
                             "ms": round(up.opened_ms, 1), "error": up.error})

            retryable = up.status == 0 or up.status == 429 or up.status >= 500
            if retryable:
                self.app.breaker.record(cand.id, ok=False, retryable=True)
                self.app.record(self._outcome(cand, up, False, need, decision,
                                              len(attempts)))
                close_upstream(up)
                fallback_from = fallback_from or cand.id
                self.app.bump("failovers", reason="retryable")
                continue

            self.app.breaker.record(cand.id, ok=up.status < 400, retryable=False)
            if up.status >= 400:
                # 400/401/422 is the request's or the key's fault: replaying it
                # elsewhere only burns time and tokens, and counting it as a
                # model failure would poison the score. Relay it untouched.
                close_upstream(up)
                return self._send_json(up.status, _json_error(
                    f"upstream returned HTTP {up.status}",
                    "upstream_client_error", up.status),
                    {**{k: v for k, v in self._routing_headers(
                        cand, decision, attempts, fallback_from).items()}})

            self.app.sticky.set(sticky_key, cand.id)
            ok, total_ms, ttft_ms = self._relay(up, cand, decision, attempts,
                                                fallback_from, need)
            self.app.record(self._outcome(cand, up, ok, need, decision,
                                          len(attempts), total_ms, ttft_ms))
            return None

        self.app.bump("errors", reason="all_attempts_failed")
        return self._send_json(502, _json_error(
            "all routing attempts failed", "upstream_unavailable", 502),
            {"X-Modellens-Attempts": str(len(attempts))})

    def _endpoint(self, name: str) -> ml.Endpoint:
        for ep in self.app.ctx.endpoints:
            if ep.name == name:
                return ep
        raise KeyError(name)

    # -- relaying -------------------------------------------------------
    def _relay(self, up: Upstream, cand: Cand, decision: dict,
               attempts: list[dict], fallback_from: str | None,
               need: Need) -> tuple[bool, float, float | None]:
        started = time.monotonic()
        resp = up.resp
        headers = self._routing_headers(cand, decision, attempts, fallback_from)
        ctype = (resp.getheader("Content-Type") if resp is not None else None)
        ttft: float | None = None
        try:
            if need.stream:
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype or "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = read_chunk(resp)
                    if not chunk:
                        break
                    if ttft is None:
                        ttft = (time.monotonic() - started) * 1000.0
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                data = resp.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    self._send_json(502, _json_error(
                        "upstream response too large", "upstream_unavailable", 502))
                    return False, (time.monotonic() - started) * 1000.0, None
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype or "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            return True, up.opened_ms + (time.monotonic() - started) * 1000.0, ttft
        except (BrokenPipeError, ConnectionAbortedError):
            # The client left: stop paying for the rest of the stream.
            self.app.bump("errors", reason="client_cancelled")
            return False, (time.monotonic() - started) * 1000.0, ttft
        except Exception as exc:
            err_type, _ = ml._classify_network_error(exc)
            self.app.bump("errors", reason=f"relay_{err_type}")
            return False, (time.monotonic() - started) * 1000.0, ttft
        finally:
            close_upstream(up)

    def _routing_headers(self, cand: Cand, decision: dict,
                         attempts: list[dict], fallback_from: str | None) -> dict:
        age = cand.age_hours
        headers = {
            "X-Modellens-Model": cand.model,
            "X-Modellens-Endpoint": cand.endpoint,
            "X-Modellens-Profile": str(decision.get("profile")),
            "X-Modellens-Reason": str(decision.get("reason")),
            "X-Modellens-Attempts": str(len(attempts)),
            "X-Modellens-Score": str(cand.score),
            "X-Modellens-Evidence": (f"probe n={cand.probe_rows}"
                                     + (f", age={age:.2f}h" if age is not None else "")),
        }
        if decision.get("degraded"):
            headers["X-Modellens-Degraded"] = "1"
        if fallback_from:
            headers["X-Modellens-Fallback-From"] = fallback_from
        return headers

    def _outcome(self, cand: Cand, up: Upstream, ok: bool, need: Need,
                 decision: dict, attempt: int, latency_ms: float | None = None,
                 ttft_ms: float | None = None) -> dict:
        return {
            "endpoint": cand.endpoint, "model": cand.model,
            "ok": bool(ok), "status": up.status,
            "latency_ms": round(latency_ms if latency_ms is not None else up.opened_ms, 1),
            "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
            # stream_ok stays None: the proxy relays bytes, it does not parse
            # them, so claiming "stream verified" here would be a guess.
            "stream_ok": None,
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "proxy",
            "profile": decision.get("profile"),
            "reason": decision.get("reason"),
            "attempt": attempt,
            "stream": bool(need.stream),
            "error": up.error or (None if ok else f"HTTP {up.status}"),
            "error_type": up.error_type,
        }

    # -- admin ----------------------------------------------------------
    def _need_from_query(self) -> Need:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        truthy = lambda k: (q.get(k) or ["0"])[0].lower() in ("1", "true", "yes")
        return Need(stream=truthy("stream"), tools=truthy("tools"),
                    json_mode=truthy("json"), vision=truthy("vision"))

    def _table_json(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        profile = (q.get("profile") or [self.app.cfg.profile])[0]
        if profile not in PROFILES:
            profile = self.app.cfg.profile
        table = self.app.table()
        need = self._need_from_query()
        return self._send_json(200, {
            "built_at": table.built_at,
            "age_s": round(time.time() - table.built_at, 1),
            "profile": profile,
            "degraded": table.degraded.get(profile),
            "note": table.notes.get(profile),
            "need": need.__dict__,
            "candidates": [
                {**cand.to_json(), "blocked": gate_reason(cand, need, self.app.cfg)}
                for cand in table.ordered(profile)
            ],
            "excluded": table.skipped,
            "verdicts": table.verdict_counts(),
        })

    def _explain(self, body: bytes):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        payload: dict = {}
        if body:
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
        if not isinstance(payload, dict) or not payload:
            payload = {"model": (q.get("model") or ["auto"])[0],
                       "stream": (q.get("stream") or ["0"])[0].lower() in ("1", "true"),
                       "tools": (q.get("tools") or ["0"])[0].lower() in ("1", "true"),
                       "messages": [{"role": "user",
                                     "content": (q.get("prompt") or ["hi"])[0]}]}
        need = need_from_body(payload)
        profile, pinned = resolve_profile(payload.get("model"),
                                          (q.get("profile") or [None])[0],
                                          self.app.cfg.profile)
        table = self.app.table()
        if pinned:
            decision = self._pinned_decision(table, pinned)
            if decision is None:
                return self._send_json(200, {
                    "profile": "pinned", "reason": "unknown_pin", "ladder": [],
                    "skipped": [], "selected": None,
                    "table_age_s": round(time.time() - table.built_at, 1)})
        else:
            decision = build_ladder(
                table, self.app.cfg, profile, need, self.app.breaker,
                self.app.sticky, session_key(self.headers, payload),
                roll=self.app.next_roll(json.dumps(payload, sort_keys=True)[:2000]))
        return self._send_json(200, self._decision_json(decision, table))

    def _decision_json(self, decision: dict, table: Table) -> dict:
        return {
            "profile": decision["profile"],
            "reason": decision["reason"],
            "degraded": decision["degraded"],
            "note": decision["note"],
            "need": decision.get("need"),
            "selected": (decision["ladder"][0].to_json()
                         if decision["ladder"] else None),
            "ladder": [c.id for c in decision["ladder"]],
            "skipped": decision["skipped"],
            "eligible": decision["eligible"],
            "table_age_s": round(time.time() - table.built_at, 1),
        }

    def _models_json(self):
        table = self.app.table()
        data = [{"id": alias, "object": "model", "owned_by": "modellens"}
                for alias in AUTO_ALIASES]
        data += [{"id": c.model, "object": "model", "owned_by": c.endpoint}
                 for c in table.candidates]
        return self._send_json(200, {"object": "list", "data": data})

    def _metrics(self):
        c = self.app.snapshot_counters()
        lines = [
            "# HELP modellens_proxy_requests_total Requests seen by the proxy",
            "# TYPE modellens_proxy_requests_total counter",
            f"modellens_proxy_requests_total {c.get('requests', 0)}",
            "# TYPE modellens_proxy_routed_total counter",
            f"modellens_proxy_routed_total {c.get('routed', 0)}",
            "# TYPE modellens_proxy_pinned_total counter",
            f"modellens_proxy_pinned_total {c.get('pinned', 0)}",
            "# TYPE modellens_proxy_dry_run_total counter",
            f"modellens_proxy_dry_run_total {c.get('dry_run', 0)}",
            "# TYPE modellens_proxy_failovers_total counter",
            f"modellens_proxy_failovers_total {c.get('failovers', 0)}",
            "# TYPE modellens_proxy_errors_total counter",
            f"modellens_proxy_errors_total {c.get('errors', 0)}",
        ]
        for model, n in sorted(c.get("by_model", {}).items()):
            safe = re.sub(r'["\n\\]', "_", model)
            lines.append(f'modellens_proxy_model_requests_total{{model="{safe}"}} {n}')
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._headers_out().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, app: App):
        super().__init__(addr, handler)
        self.app = app


def make_handler(app: App):
    return type("BoundHandler", (Handler,), {"app": app})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ModelLens proxy (OpenAI-compatible)")
    ap.add_argument("--config", default=os.environ.get("MODELLENS_CONFIG", ""))
    ap.add_argument("--data", default=os.environ.get("MODELLENS_DATA", ""))
    ap.add_argument("--listen", default="")
    ap.add_argument("--profile", default="")
    ap.add_argument("--once", action="store_true",
                    help="print the routing table for the chosen profile and exit")
    args = ap.parse_args(argv)

    cfg, endpoints = load_proxy_config(args.config)
    if args.listen:
        cfg.listen = args.listen
    if args.profile:
        cfg.profile = args.profile
    if not endpoints:
        sys.stderr.write("modellens-proxy: no endpoints configured "
                         "(config.json endpoints[] or MODELLENS_CONFIG)\n")
    app = App(cfg, args.config, args.data, endpoints=endpoints,
              synchronous_records=args.once)
    app.start()
    if args.once:
        table = app.table()
        print(json.dumps({
            "profile": cfg.profile,
            "degraded": table.degraded.get(cfg.profile),
            "note": table.notes.get(cfg.profile),
            "candidates": [c.to_json() for c in table.ordered(cfg.profile)],
            "excluded": table.skipped,
        }, ensure_ascii=False, indent=2))
        app.stop()
        return 0

    host, _, port = cfg.listen.partition(":")
    srv = ProxyServer((host or "127.0.0.1", int(port or 8766)),
                      make_handler(app), app)
    print(f"modellens-proxy on http://{host or '127.0.0.1'}:{port or 8766}  "
          f"profile={cfg.profile}  candidates={len(app.table().candidates)}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
