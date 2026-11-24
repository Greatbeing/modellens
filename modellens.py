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
import calendar
import contextlib
import hashlib
import json
import math
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from statistics import median

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "modellens"
SERVER_VERSION = "0.2.1"

# How hard a probe pushes a model: enough to expose broken routing, cheap enough
# to run often. A probe is a real request, so it costs a few tokens.
PROBE_PROMPT = "Reply with exactly: OK"
PROBE_MAX_TOKENS = 512
PROBE_TIMEOUT = 90
PROBE_WORKERS_DEFAULT = 1  # sequential by default: concurrent probes can self-induce 429s
PROBE_WORKERS_MAX = 16     # hard ceiling: workers=500 would be a thread storm
PROBE_MAX_LIMIT = 200      # ceiling for probe limit; a negative would slice backwards
RANKING_DEFAULT_LIMIT = 20
LIST_MODELS_MAX_LIMIT = 1000

# Scoring weights for the 0-100 quality score.
W_SUCCESS = 50.0
W_LATENCY = 30.0
W_CANCEL = 20.0
LATENCY_REFERENCE_MS = 8000.0  # a median at or above this scores 0 on latency
MIN_EVIDENCE = 5  # fewer probes than this is flagged low_evidence
WILSON_Z = 1.96   # 95% confidence


def wilson_lower(successes: int, n: int, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval, in [0, 1].

    Scoring on this instead of the raw rate means one lucky probe (1/1) cannot
    outrank a model that succeeded 40 times out of 40.
    """
    if n <= 0:
        return 0.0
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / (1 + z2 / n))
FAST_MIN_SUCCESS = 90.0  # 'fast' only routes to models at or above this success rate
STALE_AFTER_HOURS = 6.0  # evidence older than this is too old to route on:
# the probe cron runs every 2h, so 6h is three missed cycles. A model that was
# healthy this morning can be quota-blocked by noon on a free tier.


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _classify_network_error(exc: Exception) -> tuple[str, str]:
    """Map a network exception to (error_type, message) for the history row.

    urllib wraps most transport failures in URLError with a .reason; the
    concrete reason class says *why* the request died (timeout vs refused vs
    DNS vs TLS), so history records the cause instead of a bare "status 0".
    """
    reason = getattr(exc, "reason", None)
    if reason is None:
        reason = exc
    msg = str(reason) or str(exc)
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in msg.lower():
        return "timeout", msg
    if isinstance(reason, ConnectionRefusedError):
        return "conn_refused", msg
    if isinstance(reason, ConnectionResetError):
        return "conn_reset", msg
    if isinstance(reason, ConnectionError):
        return "conn_error", msg
    if isinstance(reason, socket.gaierror):
        return "dns_error", msg
    if isinstance(reason, ssl.SSLError):
        return "ssl_error", msg
    return "network_error", msg


def _network_error_body(exc: Exception) -> dict:
    """Body shape returned when a transport-level error swallowed the request."""
    etype, msg = _classify_network_error(exc)
    return {"__error__": {"type": etype, "message": msg[:300]}}


def _post(url: str, payload: dict, timeout: float, headers: dict | None = None) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    started = time.monotonic()  # monotonic: NTP jumps must not make latency negative
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw), (time.monotonic() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw), (time.monotonic() - started) * 1000.0
    except Exception as exc:
        return 0, _network_error_body(exc), (time.monotonic() - started) * 1000.0


def _get(url: str, timeout: float, headers: dict | None = None) -> tuple[int, dict, float]:
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(raw), (time.monotonic() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw), (time.monotonic() - started) * 1000.0
    except Exception as exc:
        return 0, _network_error_body(exc), (time.monotonic() - started) * 1000.0


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
        # Manual truth for what probes cannot measure (json mode, vision, context
        # window). The proxy gates on capabilities, and guessing would either
        # block a working model or route a request that must fail.
        caps = cfg.get("capabilities")
        self.capabilities = caps if isinstance(caps, dict) else {}

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


def _parse_pricing(item: dict) -> dict:
    """Best-effort read of per-million-token pricing from a /models entry.

    Providers disagree on the field name and shape. Anything we cannot read is
    reported as None so `cheap` can fall back honestly instead of guessing.
    """
    raw = item.get("priced") or item.get("pricing") or {}
    if not isinstance(raw, dict):
        return {"input": None, "output": None}

    def num(v):
        try:
            f = float(v)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    return {"input": num(raw.get("input")), "output": num(raw.get("output"))}


def _parse_cap_meta(item: dict) -> dict:
    """Best-effort context window / vision flags from a /models entry.

    Providers disagree about both the field names and whether they publish them
    at all, so every value is Optional: unknown must stay unknown rather than
    becoming a false "no" that gates a capable model out of routing.
    """
    ctx = None
    for key in ("context_length", "context_window", "max_context_length",
                "max_model_len", "max_input_tokens"):
        v = item.get(key)
        if isinstance(v, (int, float)) and v > 0:
            ctx = int(v)
            break
    if ctx is None:
        top = item.get("top_provider")
        if isinstance(top, dict) and isinstance(top.get("context_length"), (int, float)):
            ctx = int(top["context_length"]) or None
    vision = None
    arch = item.get("architecture")
    if isinstance(arch, dict):
        mods = arch.get("input_modalities")
        if isinstance(mods, list):
            vision = any(str(m).lower() in ("image", "vision") for m in mods)
    if vision is None:
        for key in ("supports_vision", "vision"):
            if isinstance(item.get(key), bool):
                vision = item[key]
                break
    return {"context_length": ctx, "vision": vision}


def discover_models_detailed(ep: Endpoint, timeout: float = 20) -> list[dict]:
    """Advertised models with best-effort metadata.

    [{id, pricing, context_length, vision}] — the extra keys are Optional and
    do not change the shape existing callers rely on. The proxy passes a short
    timeout: it rebuilds its routing table in the background and must not hang
    a request behind a slow /models.
    """
    status, body, _ = _get(ep.models_url, timeout=timeout, headers=ep.headers())
    if status != 200 or not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            mid = str(item["id"])
            pricing = _parse_pricing(item)
            meta = _parse_cap_meta(item)
        elif isinstance(item, str):
            mid = item
            pricing = {"input": None, "output": None}
            meta = {"context_length": None, "vision": None}
        else:
            continue
        if mid in seen:
            continue
        if ep.models_filter and not any(f in mid for f in ep.models_filter):
            continue
        seen.add(mid)
        found.append({"id": mid, "pricing": pricing, **meta})
    found.sort(key=lambda d: d["id"])
    return found


def discover_models(ep: Endpoint) -> list[str]:
    return [d["id"] for d in discover_models_detailed(ep)]


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def _parse_bool(val, default: bool = False) -> bool:
    """Booleans as MCP clients actually send them.

    The tool schema declares strings, so clients pass true / "true" / "1" /
    "false" / "0". bool("false") is True in Python — parse explicitly instead.
    Unknown strings fall back to the default.
    """
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    """Coerce a tool argument to an int clamped to [lo, hi].

    -5 would become a Python negative slice and 50000 workers a thread storm,
    so both clamp; unparseable values (None, "", "abc") fall back to default.
    """
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _is_valid_chat_response(body) -> tuple[bool, str]:
    """Strictly validate an OpenAI-compatible chat response.

    HTTP 200 lies in three common ways: gateways (OneAPI/NewAPI/OpenRouter)
    wrap an upstream error as {"error": ...} with status 200, they return
    choices: [], and non-JSON pages (502 HTML, empty bodies) come back wrapped
    as {"raw": ...} by _maybe_json — which isinstance(body, dict) would accept.
    """
    if not isinstance(body, dict):
        return False, "non_json"
    if "error" in body and body["error"]:
        return False, "body_error"
    if set(body.keys()) == {"raw"} or not body:
        return False, "non_json_raw"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, "empty_choices"
    first = choices[0] or {}
    if first.get("finish_reason") == "error":
        return False, "finish_error"
    msg = first.get("message") or {}
    has_payload = bool(
        (isinstance(msg.get("content"), str) and msg["content"].strip())
        or (isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"].strip())
        or msg.get("tool_calls")
    )
    return (True, "") if has_payload else (False, "empty_content")


def _failure_message(status: int, body, reason: str) -> tuple[str, str | None]:
    """Human error text + transport error_type for a failed probe."""
    if isinstance(body, dict):
        net = body.get("__error__")
        if isinstance(net, dict):
            return (f"{net.get('type')}: {net.get('message')}")[:300], str(net.get("type"))
        err = body.get("error")
        if isinstance(err, dict):
            m = str(err.get("message") or err.get("code") or "")
            if m:
                return m[:300], None
        elif err:
            return str(err)[:300], None
    if status == 200:
        return f"HTTP 200 without a valid completion ({reason or 'invalid body'})", None
    return f"HTTP {status}", None


def _open_stream(ep: Endpoint, model: str):
    """Open a stream:true chat completion. Returns (status, resp|None, ms, err).

    err is None on success; on failure it is {"type": ..., "message": ...} or a
    short text. Separated from the SSE loop so tests can stub the transport.
    """
    payload = {"model": model,
               "messages": [{"role": "user", "content": PROBE_PROMPT}],
               "max_tokens": 32,
               "stream": True}
    req = urllib.request.Request(
        ep.chat_url, data=json.dumps(payload).encode("utf-8"),
        headers={**ep.headers(), "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST")
    started = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)
        return resp.status, resp, (time.monotonic() - started) * 1000.0, None
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            raw = ""
        return exc.code, None, (time.monotonic() - started) * 1000.0, raw or f"HTTP {exc.code}"
    except Exception as exc:
        etype, msg = _classify_network_error(exc)
        return 0, None, (time.monotonic() - started) * 1000.0, {"type": etype, "message": msg}


def _probe_stream_sse(ep: Endpoint, model: str) -> dict:
    """Streaming probe: validate SSE blocks, measure TTFT and total latency.

    stream_ok means HTTP 200 AND at least one delta chunk carrying real output
    AND no error block inside the stream. Reading stops after a few valid token
    chunks — enough to prove the stream works without burning more tokens.
    """
    out = {"ok": False, "status": 0, "ttft_ms": None, "latency_ms": None, "error": None}
    status, resp, open_ms, err = _open_stream(ep, model)
    out["status"] = status
    if resp is None:
        out["latency_ms"] = round(open_ms, 1)
        if isinstance(err, dict):
            out["error"] = f"{err.get('type')}: {err.get('message')}"[:300]
        else:
            out["error"] = str(err or f"HTTP {status}")[:300]
        return out
    t0 = time.monotonic()
    ttft_ms = None
    chunks = 0
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                ev = json.loads(data_str)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("error"):
                e = ev["error"]
                out["error"] = ((e.get("message") if isinstance(e, dict) else str(e))
                                or "stream error")[:200]
                break
            choices = ev.get("choices") if isinstance(ev, dict) else None
            delta = (choices[0].get("delta")
                     if isinstance(choices, list) and choices
                     and isinstance(choices[0], dict) else None) or {}
            content = delta.get("content")
            reasoning = delta.get("reasoning_content")
            has_token = ((isinstance(content, str) and content.strip())
                         or (isinstance(reasoning, str) and reasoning.strip())
                         or delta.get("tool_calls"))
            if has_token:
                if ttft_ms is None:
                    ttft_ms = (time.monotonic() - t0) * 1000.0
                chunks += 1
                if chunks >= 4:  # a few valid tokens prove the stream is healthy
                    break
        out["latency_ms"] = round(open_ms + (time.monotonic() - t0) * 1000.0, 1)
        if chunks > 0:
            out["ok"] = True
            out["ttft_ms"] = round(ttft_ms, 1)
        elif not out["error"]:
            out["error"] = "no token chunk received (gateway may not stream)"
    except Exception as exc:
        etype, _ = _classify_network_error(exc)
        out["error"] = f"stream aborted ({etype})"
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return out


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
        "error_type": None,
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

    valid, reason = _is_valid_chat_response(body)
    if status == 200 and valid:
        out["ok"] = True
        msg = body["choices"][0].get("message") or {}
        content = msg.get("content")
        out["reply"] = (content or "")[:80] if isinstance(content, str) else None
    else:
        text, etype = _failure_message(status, body, reason)
        out["error"] = text
        out["error_type"] = etype

    if check_stream and out["ok"]:
        sres = _probe_stream_sse(ep, model)
        out["stream_ok"] = sres["ok"]
        out["ttft_ms"] = sres["ttft_ms"]
        if not sres["ok"]:
            out["stream_error"] = sres["error"]

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
    if status != 200:
        return None
    valid, _reason = _is_valid_chat_response(body)
    if not valid:
        return None
    msg = body["choices"][0].get("message") or {}
    calls = msg.get("tool_calls") or []
    return any(c.get("function", {}).get("name") == "modellens_ping" for c in calls)


# --------------------------------------------------------------------------
# History store
# --------------------------------------------------------------------------

class Store:
    """JSON store of probe results, bounded to the last N rows per model.

    Safe with several threads AND several processes writing the same file (the
    MCP server, the CLI batch, and the hosted x402 server all open one):

    * an RLock for threads, plus an advisory file lock for processes — the
      read-modify-write in append() would otherwise lose a whole probe batch
      when two writers interleave (each reloads, each extends, last one wins);
    * change detection by CONTENT HASH, not mtime: a restored backup, `rsync -t`
      or a coarse filesystem can leave mtime equal or older, and `mtime >` then
      never notices a foreign write;
    * a UNIQUE temp file + fsync + os.replace (a fixed ".tmp" name races two
      concurrent saves: one moves a half-written or already-moved file);
    * on Windows os.replace raises PermissionError while ANY other process holds
      the file open, so the rename retries for ~1 s;
    * a corrupt file is copied aside instead of being overwritten.
    """

    def __init__(self, path: str, keep: int = 200):
        self.path = path
        self.keep = keep
        self.rows: list[dict] = []
        self._lock = threading.RLock()
        self._hash: str | None = None   # sha256 of the bytes we last read/wrote
        self._unsaved: list[dict] = []  # added since the last successful write
        self._load()

    # -- reading ---------------------------------------------------------
    def _read_bytes(self) -> bytes | None:
        if not self.path:
            return None
        try:
            with open(self.path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _load_from(self, raw: bytes | None) -> None:
        if not raw:
            self.rows, self._hash = [], None
            return
        try:
            data = json.loads(raw.decode("utf-8"))
            rows = data.get("rows") if isinstance(data, dict) else data
            self.rows = list(rows or []) if isinstance(rows, (list, type(None))) else []
        except Exception:
            self._keep_corrupt(raw)
            self.rows = []
        self._hash = hashlib.sha256(raw).hexdigest()

    def _keep_corrupt(self, raw: bytes) -> None:
        """Copy an unreadable history aside so a later save cannot destroy it."""
        if not self.path:
            return
        target = f"{self.path}.corrupt-{int(time.time())}"
        try:
            with open(target, "xb") as fh:
                fh.write(raw)
            sys.stderr.write(f"modellens: unreadable history; kept a copy at {target}\n")
        except OSError:
            pass

    def _load(self) -> None:
        self._load_from(self._read_bytes())

    def _reload_if_changed(self) -> bool:
        raw = self._read_bytes()
        if raw is None:
            return False
        if hashlib.sha256(raw).hexdigest() == self._hash:
            return False
        self._load_from(raw)
        return True

    # -- cross-process lock ----------------------------------------------
    @contextlib.contextmanager
    def _file_lock(self):
        if not self.path:
            yield
            return
        lock_path = self.path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        fh = open(lock_path, "a+b")
        try:
            if fh.tell() == 0:          # msvcrt needs a byte to lock
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                # LK_LOCK retries once per SECOND on Windows, so a contended
                # save stalls in 1 s steps. Poll with LK_NBLCK instead.
                deadline = time.monotonic() + 30.0
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.005)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    # -- writing ---------------------------------------------------------
    def _payload(self) -> bytes:
        return json.dumps({"version": 1, "rows": self.rows},
                          ensure_ascii=False).encode("utf-8")

    def _write(self, payload: bytes) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".modellens-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if os.name != "nt":             # make the rename itself durable
            try:
                dfd = os.open(d, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass

    @staticmethod
    def _replace(tmp: str, path: str, attempts: int = 100) -> None:
        """os.replace with a ~1 s retry.

        On POSIX the rename is atomic and never fails this way. On Windows it
        raises PermissionError while any process holds a handle on the target
        (readers do not share delete), and an antivirus/indexer can hold it for
        a few milliseconds, so retry instead of failing the whole probe batch.
        """
        last = None
        for i in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                if i == attempts - 1:
                    break
                time.sleep(0.01)
        raise last if last else OSError(f"could not replace {path}")

    # -- public API ------------------------------------------------------
    def add(self, rows: list[dict]) -> None:
        """Buffer rows in memory (call save() to persist)."""
        with self._lock:
            self.rows.extend(rows)
            self._unsaved.extend(rows)
            self._trim()

    def save(self) -> None:
        """Persist buffer + anything a concurrent writer added meanwhile."""
        if not self.path:
            self._unsaved = []
            return
        with self._lock, self._file_lock():
            if self._reload_if_changed():
                # Someone else wrote while we held unsaved rows: merge instead
                # of clobbering. Their rows are in self.rows, ours are re-applied.
                self.rows.extend(self._unsaved)
                self._trim()
            payload = self._payload()
            self._write(payload)
            self._hash = hashlib.sha256(payload).hexdigest()
            self._unsaved = []

    def append(self, rows: list[dict]) -> None:
        """add + save as one atomic operation (use this from request threads)."""
        self.add(rows)
        self.save()

    def refresh(self) -> bool:
        """Pick up rows another process wrote (CLI batch, hosted server, ingest).

        A long-running server keeps its rows in memory; without this it would
        only ever see its own writes. Returns True when the file had changed.
        """
        if not self.path:
            return False
        with self._lock:
            return self._reload_if_changed()

    def _trim(self) -> None:
        """Keep the newest `keep` rows per (endpoint, model), preserving order.

        Selected by POSITION, not by re-sorting on probed_at: rows without a
        timestamp (imported history, rows pushed by the worker, hand-edited
        files) all compare equal, so sorting a newest-first bucket left the
        bucket permanently in reverse and every later trim evicted the middle
        of the history — 10 rows with keep=2 collapsed to [newest, oldest].
        """
        keep_idx: dict[tuple, list[int]] = {}
        for idx in range(len(self.rows) - 1, -1, -1):
            row = self.rows[idx]
            # A tuple, not "endpoint/model": an OpenRouter-style id
            # ("openai/gpt-4o") encodes exactly like endpoint "openai" + model
            # "gpt-4o", so a string key would trim two models as one.
            key = (row.get("endpoint"), row.get("model"))
            bucket = keep_idx.setdefault(key, [])
            if len(bucket) < self.keep:
                bucket.append(idx)
        kept = {i for bucket in keep_idx.values() for i in bucket}
        if len(kept) != len(self.rows):
            self.rows = [r for i, r in enumerate(self.rows) if i in kept]

    def for_model(self, endpoint: str, model: str) -> list[dict]:
        with self._lock:               # snapshot: callers must not mutate state
            return [r for r in self.rows
                    if r.get("endpoint") == endpoint and r.get("model") == model]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def _parse_ts(value) -> float | None:
    """Parse a probe 'probed_at' ISO-8601 UTC timestamp to epoch seconds.

    Returns None for missing or unparseable values: age unknown means
    "not proven stale", never "stale by default".
    """
    if not isinstance(value, str):
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _format_age(age_hours: float | None) -> str:
    """Short human age for a table column: '12m' / '3h' / '2d' / '-'.

    Takes raw (unrounded) hours so minutes land on real minutes, not on the
    6-minute grid a pre-rounded 0.1h value would impose (1m/6m/12m/...).
    """
    if age_hours is None:
        return "-"
    minutes = age_hours * 60.0
    if minutes < 60:
        return f"{max(1, round(minutes))}m"
    if age_hours < 48:
        return f"{age_hours:.0f}h"
    return f"{age_hours / 24:.0f}d"


def score_model(rows: list[dict], now: float | None = None) -> dict:
    if now is None:
        now = time.time()
    attempts = len(rows)
    oks = [r for r in rows if r.get("ok")]
    failures = attempts - len(oks)
    cancelled = len([r for r in rows if not r.get("ok") and r.get("status") in (0, 499)])
    # bool is an int subclass: a latency_ms of True would corrupt the median.
    latencies = [r["latency_ms"] for r in oks
                 if isinstance(r["latency_ms"], (int, float))
                 and not isinstance(r["latency_ms"], bool)]

    success_rate = (len(oks) / attempts * 100.0) if attempts else 0.0
    cancel_rate = (cancelled / attempts * 100.0) if attempts else 0.0
    med = median(latencies) if latencies else None

    # Freshness of the evidence: the newest probe in these rows. Raw seconds —
    # do NOT round first, or _format_age can only ever show 6-minute steps.
    last_ts = None
    for r in rows:
        ts = _parse_ts(r.get("probed_at"))
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts
    age_hours = (max(0.0, now - last_ts) / 3600.0
                 if last_ts is not None else None)
    last_probed_at = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_ts))
                      if last_ts is not None else None)

    if attempts == 0:
        verdict = "untested"
    elif success_rate == 0 and all(r.get("status") == 404 for r in rows):
        verdict = "dead"
    elif success_rate == 0 and all(r.get("status") == 429 for r in rows):
        verdict = "quota_blocked"
    elif success_rate == 0 and all(r.get("status") in (401, 403) for r in rows):
        verdict = "auth_error"  # a key/permission problem, not a flaky model
    elif cancel_rate >= 30:
        verdict = "unreliable"
    elif success_rate >= 95 and med is not None and med < 3000:
        verdict = "healthy"  # med == 0 must read as fast, and None as unmeasured
    elif success_rate == 0:
        verdict = "failing"  # real failures (500/502/503/timeout), not mixed health
    else:
        verdict = "mixed"

    if (attempts and success_rate > 0 and verdict in ("healthy", "mixed")
            and age_hours is not None and age_hours > STALE_AFTER_HOURS):
        # Old evidence is not bad evidence — it is no evidence about *now*.
        # Failure verdicts (dead/quota_blocked/auth_error/unreliable/failing)
        # are kept: they say what the last probe found, and the age column
        # says how long ago.
        verdict = "stale"

    if attempts == 0 or success_rate == 0:
        # No evidence, or only failures: nothing to recommend. Keeping this at 0
        # stops a dead or quota-blocked model outranking a merely mediocre one.
        return {
            "attempts": attempts, "ok": 0, "failed": attempts,
            "cancelled": cancelled,
            "success_rate": 0.0, "cancel_rate": round(cancel_rate, 1),
            "success_lower_bound": 0.0, "low_evidence": attempts < MIN_EVIDENCE,
            "median_latency_ms": None, "p95_latency_ms": None,
            "score": 0.0, "verdict": verdict,
            "last_probed_at": last_probed_at, "age_hours": age_hours,
        }

    # Unknown latency (ok rows with no latency recorded) earns no latency
    # credit — honest, and it also keeps None out of the arithmetic.
    latency_score = (0.0 if med is None
                     else max(0.0, min(1.0, 1.0 - (med / LATENCY_REFERENCE_MS))) * 100.0)
    # Success is scored on the Wilson lower bound, so sample size counts:
    # thin evidence scores lower until more probes confirm it.
    success_evidence = wilson_lower(len(oks), attempts) * 100.0
    score = (success_evidence * W_SUCCESS + latency_score * W_LATENCY
             + max(0.0, 100.0 - cancel_rate * 2.0) * W_CANCEL) / (
        W_SUCCESS + W_LATENCY + W_CANCEL)

    return {
        "attempts": attempts,
        "ok": len(oks),
        "failed": failures,
        "cancelled": cancelled,
        "success_rate": round(success_rate, 1),
        # Lower bound of the 95% Wilson interval: 1/1 ok is ~21%, 20/20 is ~84%.
        "success_lower_bound": round(wilson_lower(len(oks), attempts) * 100.0, 1),
        "low_evidence": attempts < MIN_EVIDENCE,
        "cancel_rate": round(cancel_rate, 1),
        "median_latency_ms": round(med, 1) if med is not None else None,
        "p95_latency_ms": round(
            sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)], 1)
            if len(latencies) >= 20 else None,
        "score": round(score, 1),
        "verdict": verdict,
        "last_probed_at": last_probed_at,
        "age_hours": age_hours,
    }


def build_ranking(store: Store, endpoints: list[Endpoint], now: float | None = None) -> list[dict]:
    store.refresh()  # rankings must include rows another process wrote meanwhile
    if now is None:
        now = time.time()
    # Keyed by the (endpoint, model) TUPLE: "endpoint/model" is ambiguous once
    # a model id contains a slash (openrouter "openai/gpt-4o" vs endpoint
    # "openai" + model "gpt-4o" would collide and one model would vanish).
    models: dict[tuple, dict] = {}
    for ep in endpoints:
        if not ep.enabled:
            continue
        for info in discover_models_detailed(ep):
            mid = info["id"]
            models[(ep.name, mid)] = {
                "endpoint": ep.name, "model": mid,
                "id": f"{ep.name}/{mid}",   # display label, never a key
                "pricing": info["pricing"],
            }
    out = []
    for key, info in models.items():
        stats = score_model(store.for_model(info["endpoint"], info["model"]), now=now)
        out.append({**info, **stats})
    out.sort(key=lambda r: (-r["score"], r["id"]))
    return out


PROFILE_HINTS = {
    "fast": f"lowest median latency among models with success rate >= {FAST_MIN_SUCCESS:.0f}%",
    "cheap": "zero-priced models when the endpoint advertises pricing; otherwise "
             "reliability then latency (true per-token cost is not collected)",
    "capable": "highest quality score, then lowest latency",
}


def _zero_priced(row: dict) -> bool:
    p = row.get("pricing") or {}
    return p.get("input") == 0 and p.get("output") == 0


def usable_for_routing(ranking: list[dict]) -> list[dict]:
    """Rows that may be routed to at all: some success, no failure verdict."""
    return [r for r in ranking
            if r.get("score", 0) > 0
            and r.get("verdict") not in ("dead", "quota_blocked", "auth_error",
                                         "failing", "stale", "untested")]


def order_for_profile(rows: list[dict], profile: str) -> tuple[list[dict], str | None]:
    """Order routing candidates for a profile; second value is an honest note.

    Shared by recommend() (what to suggest) and the proxy (what to route to), so
    the two can never disagree about which model is "fastest" or "cheapest".
    Returns ([], note) when nothing meets the profile's bar.
    """
    profile = (profile or "capable").lower()
    if profile == "fast":
        ok = [r for r in rows if r.get("success_rate", 0) >= FAST_MIN_SUCCESS]
        if not ok:
            return [], f"no model meets the {FAST_MIN_SUCCESS:.0f}% success bar for 'fast'"
        return sorted(ok, key=lambda r: _latency_key(r)), None
    if profile == "cheap":
        free = [r for r in rows if _zero_priced(r)]
        if free:
            return (sorted(free, key=lambda r: (-r.get("success_rate", 0), _latency_key(r))),
                    "zero-priced models (endpoint advertises $0 input/output)")
        return (sorted(rows, key=lambda r: (-r.get("success_rate", 0), _latency_key(r))),
                "no zero-priced model advertised; fell back to reliability then "
                "latency - true per-token cost is not collected")
    return sorted(rows, key=lambda r: (-r.get("score", 0), _latency_key(r))), None


def _latency_key(row: dict):
    lat = row.get("median_latency_ms")
    return (0, lat) if lat is not None else (1, 0.0)


def recommend(ranking: list[dict], profile: str) -> dict:
    profile = (profile or "capable").lower()
    criteria = PROFILE_HINTS.get(profile, PROFILE_HINTS["capable"])
    usable = usable_for_routing(ranking)
    if not usable:
        if any(r.get("verdict") == "stale" for r in ranking):
            return {"profile": profile, "criteria": criteria, "picks": [],
                    "note": f"all models with a passing score are stale "
                            f"(not probed in the last {STALE_AFTER_HOURS:.0f}h) — "
                            f"probe again for a fresh recommendation"}
        return {"profile": profile, "criteria": criteria, "picks": [],
                "note": "no model has a passing score yet"}

    ordered, note = order_for_profile(usable, profile)
    if not ordered:
        return {"profile": profile, "criteria": criteria, "picks": [], "note": note}

    return {
        "profile": profile,
        "criteria": criteria,
        "note": note,
        "picks": [
            {"rank": i + 1, "id": r["id"], "score": r["score"],
             "median_latency_ms": r["median_latency_ms"],
             "success_rate": r["success_rate"], "verdict": r["verdict"],
             "attempts": r["attempts"], "age_hours": r.get("age_hours")}
            for i, r in enumerate(ordered[:5])
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
    limit = _clamp_int(args.get("limit"), 0, 0, LIST_MODELS_MAX_LIMIT)
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


def _probe_one(ep: Endpoint, model: str, check_tools: bool, check_stream: bool = True) -> dict:
    row = probe_model(ep, model, check_stream=check_stream)
    if check_tools and row["ok"]:
        row["tools_ok"] = probe_tools(ep, model)
    mark = "ok" if row["ok"] else f"FAIL({row['status']})"
    extra = f" tools={'yes' if row.get('tools_ok') else 'no'}" if check_tools else ""
    stream_extra = ""
    if row.get("stream_ok") is not None:
        stream_extra = f" stream={'yes' if row['stream_ok'] else 'no'}"
        if row.get("ttft_ms") is not None:
            stream_extra += f" ttft={row['ttft_ms']:.0f}ms"
    print(f"  {ep.name}/{model}: {mark} {row['latency_ms']}ms{stream_extra}{extra}",
          file=sys.stderr)
    return row


def tool_probe(ctx: Context, args: dict) -> str:
    endpoint = (args.get("endpoint") or "").strip()
    model = (args.get("model") or "").strip()
    contains = (args.get("contains") or "").strip().lower()
    limit = _clamp_int(args.get("limit"), 20, 1, PROBE_MAX_LIMIT)
    check_tools = _parse_bool(args.get("check_tools"))
    check_stream = _parse_bool(args.get("check_stream"), default=True)
    workers = _clamp_int(args.get("workers"), PROBE_WORKERS_DEFAULT, 1, PROBE_WORKERS_MAX)

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

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(
                lambda t: _probe_one(t[0], t[1], check_tools, check_stream), targets))
    else:
        results = [_probe_one(ep, m, check_tools, check_stream) for ep, m in targets]

    # One atomic append: merge what other processes wrote meanwhile, add these
    # rows, and persist. add()+save() as two calls would leave a window where a
    # concurrent writer's batch can be clobbered.
    ctx.store.append(results)

    ok = sum(1 for r in results if r["ok"])
    lines = [f"Probed {len(results)} models: {ok} ok, {len(results) - ok} failed.", ""]
    for r in sorted(results, key=lambda x: (not x["ok"],
                                            x["latency_ms"] if x["latency_ms"] is not None else 1e12)):
        line = f"- {r['endpoint']}/{r['model']}: " + (
            f"ok {r['latency_ms']}ms" if r["ok"] else f"FAIL http={r['status']} {r['error']}")
        if r.get("stream_ok") is not None:
            line += f" stream={'yes' if r['stream_ok'] else 'no'}"
            if r.get("ttft_ms") is not None:
                line += f" ttft={r['ttft_ms']:.0f}ms"
        if r.get("tools_ok") is not None:
            line += f" tools={'yes' if r['tools_ok'] else 'no'}"
        lines.append(line)
    return "\n".join(lines)


def tool_ranking(ctx: Context, args: dict) -> str:
    ranking = build_ranking(ctx.store, ctx.endpoints)
    if not ranking:
        return "No models discovered. Run probe first."
    limit = _clamp_int(args.get("limit"), RANKING_DEFAULT_LIMIT, 1, 200)
    show_all = _parse_bool(args.get("all"))

    tested = [r for r in ranking if r["attempts"] > 0]
    untested = [r for r in ranking if r["attempts"] == 0]
    tested.sort(key=lambda r: (-r["score"], r["id"]))

    lines = [f"{'model':58s} {'score':>6} {'succ':>6} {'cancel':>7} {'median':>9} {'n':>4}  {'age':>5}  verdict"]
    for r in tested[:limit]:
        med = f"{r['median_latency_ms']:.0f}ms" if r["median_latency_ms"] is not None else "-"
        lines.append(f"{r['id']:58s} {r['score']:6.1f} {r['success_rate']:5.1f}% "
                     f"{r['cancel_rate']:6.1f}% {med:>9} {r['attempts']:4d}  "
                     f"{_format_age(r.get('age_hours')):>5}  {r['verdict']}")
    if show_all:
        lines.append("")
        lines.append(f"## Untested ({len(untested)})")
        for r in untested[:limit]:
            lines.append(f"- {r['id']}")
    else:
        lines.append("")
        lines.append(f"({len(untested)} more untested — pass all=true to list them)")
    stale = [r for r in ranking if r["verdict"] == "stale"]
    if stale:
        lines.append(f"({len(stale)} stale — not probed in the last "
                     f"{STALE_AFTER_HOURS:.0f}h, never recommended)")
    return "\n".join(lines)


def tool_recommend(ctx: Context, args: dict) -> str:
    ranking = build_ranking(ctx.store, ctx.endpoints)
    res = recommend(ranking, args.get("profile") or "capable")
    if not res["picks"]:
        return res["note"]
    lines = [f"Profile: {res['profile']}  ({res['criteria']})", ""]
    for p in res["picks"]:
        med = f"{p['median_latency_ms']:.0f}ms" if p["median_latency_ms"] is not None else "-"
        age = _format_age(p.get("age_hours"))
        lines.append(f"{p['rank']}. {p['id']} — score {p['score']}, "
                     f"success {p['success_rate']}%, median {med}, {p['verdict']} "
                     f"(n={p['attempts']}, probed {age} ago)")
    if res.get("note"):
        lines += ["", res["note"]]
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
    for k in ("healthy", "mixed", "unreliable", "quota_blocked",
              "auth_error", "failing", "dead", "stale", "untested"):
        if verdicts.get(k):
            lines.append(f"- {k}: {verdicts[k]}")
    lines.append("")
    lines.append("## Top 15 by score")
    lines.append("")
    lines.append("| model | score | success | cancel | median | n | verdict | age |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in ranking[:15]:
        med = f"{r['median_latency_ms']:.0f}ms" if r["median_latency_ms"] is not None else "-"
        lines.append(f"| {r['id']} | {r['score']} | {r['success_rate']}% "
                     f"| {r['cancel_rate']}% | {med} | {r['attempts']} | {r['verdict']} | "
                     f"{_format_age(r.get('age_hours'))} |")
    return "\n".join(lines)


TOOLS = [
    ("list_models",
     "List the models each configured endpoint advertises (GET {base_url}/models). "
     "Read-only network call to your own configured endpoints — no inference, no token cost. "
     "Use this FIRST, before probe/ranking, to see what is available. "
     "Returns model ids grouped by endpoint name; empty list if an endpoint is unreachable.",
     {"contains": "optional substring filter (matches model id)",
      "limit": "optional max models per endpoint"},
     tool_list_models),
    ("probe",
     "Probe models by sending REAL chat-completion requests; measures latency, success rate, "
     "streaming and tool calling. Side effects: consumes upstream tokens/quota and hits the "
     "network (open-world) — every probe is a real, billed completion. "
     "Use to measure what works RIGHT NOW, then call ranking. "
     "Returns per-model probe rows (ok, latency_ms, status, stream_ok, tools_ok).",
     {"endpoint": "optional endpoint name to restrict probing to",
      "model": "probe exactly this model id",
      "contains": "substring filter on model id",
      "limit": "max models to probe (default 20)",
      "check_tools": "also test function calling (slower)",
      "check_stream": "also test streaming and measure time-to-first-token (default on)",
      "workers": "parallel probe workers (default 1 = sequential; >1 is faster but "
                 "can self-induce 429s that misread model quality)"},
     tool_probe),
    ("ranking",
     "Return the quality ranking computed from RECORDED probe history (read-only scoring; "
     "also does a live read-only GET of each endpoint's /models list — no inference, no new "
     "probes, no token cost). Ordered best-first by score = success 50% + median latency 30% + "
     "cancellation 20%. Use after probe to see the ranked table; use recommend for a routing "
     "pick. Rows include model, score, success%, cancel%, median latency, verdict.",
     {"limit": "rows to show (default 20)",
      "all": "also list untested models (true/false)"},
     tool_ranking),
    ("recommend",
     "Return the best model(s) for a task profile: 'fast' (lowest latency among models with "
     ">=90% success), 'cheap' (zero-priced when the endpoint advertises pricing, else a "
     "reliability fallback), or 'capable' (highest score). Read-only: scores from recorded "
     "history plus a live read-only /models GET; omit profile to use the default. Use when you "
     "need ONE model to route to right now.",
     {"profile": "fast | cheap | capable (optional, default = capable)"},
     tool_recommend),
    ("report",
     "Return the whole picture as a markdown report: endpoints, verdict counts, top models by "
     "score, and the full per-model table. Read-only scoring plus live read-only /models "
     "discovery (no inference, no token cost). Use to inspect or share overall gateway health "
     "(e.g. paste into a STATUS.md).",
     {},
     tool_report),
]

# MCP tool annotations (MCP spec 2025-06-18): tell agents the side-effect profile of each tool.
TOOL_ANNOTATIONS = {
    "list_models": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
    "probe":       {"readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": True},
    # ranking/recommend/report score from recorded history but still GET each
    # endpoint's advertised /models list, so they are read-only yet open-world
    # (no inference, no token cost, no writes).
    "ranking":     {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
    "recommend":   {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
    "report":      {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
}


# --------------------------------------------------------------------------
# MCP server (stdio, newline-delimited JSON-RPC)
# --------------------------------------------------------------------------

def handle(ctx: Context, msg) -> dict | None:
    if not isinstance(msg, dict):
        # Valid JSON but not an object (array, string, number): there is no
        # method to dispatch. Without this guard, msg.get("id") below raises
        # AttributeError and the stdio server dies on a stray "[]".
        return err(None, -32600, "invalid request: JSON-RPC message must be an object")
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
             "annotations": TOOL_ANNOTATIONS.get(n),
             "inputSchema": {
                 "type": "object",
                 "properties": {k: {"type": "string", "description": v}
                                for k, v in spec.items()},
                 "required": [],
             }}
            for n, d, spec, _ in TOOLS
        ]})
    if method == "tools/call":
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        name = params.get("name")
        if not isinstance(name, str):
            return err(msg_id, -32602, "missing tool name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
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
            # handle() rejects non-object messages itself, so this is a last
            # resort; err(None, ...) is safe whatever the line contained.
            resp = err(None, -32603, f"internal error: {exc}")
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
    ap.add_argument("--version", action="version",
                    version=f"modellens {SERVER_VERSION}")
    ap.add_argument("--config", default=os.environ.get("MODELLENS_CONFIG", ""),
                    help="path to config.json")
    ap.add_argument("--data", default=os.environ.get("MODELLENS_DATA", ""),
                    help="path to the probe history JSON")
    ap.add_argument("--once", metavar="TOOL",
                    help="run one tool against the CLI and exit "
                         f"({', '.join(n for n, _, _, _ in TOOLS)})")
    # Arguments for --once. The README has documented `--once probe --limit 20`
    # since the first release while argparse rejected it, so the advertised
    # command failed with "unrecognized arguments".
    ap.add_argument("--limit", metavar="N", help="max models / rows (tool default applies)")
    ap.add_argument("--workers", metavar="N",
                    help=f"parallel probe workers (1-{PROBE_WORKERS_MAX}, default 1)")
    ap.add_argument("--contains", metavar="S", help="substring filter on model id")
    ap.add_argument("--model", metavar="M", help="probe exactly this model id")
    ap.add_argument("--endpoint", metavar="E", help="restrict to one endpoint name")
    ap.add_argument("--check-tools", action="store_true", help="also test function calling")
    ap.add_argument("--no-stream", action="store_true",
                    help="skip the streaming/TTFT probe")
    ap.add_argument("--profile", metavar="P",
                    help="recommend profile: fast | cheap | capable")
    ap.add_argument("--all", action="store_true",
                    help="ranking: also list untested models")
    args = ap.parse_args()

    if args.config and not os.path.exists(args.config):
        sys.stderr.write(f"modellens: config not found: {args.config}\n")
    ctx = Context(args.config, args.data)
    if not ctx.endpoints:
        sys.stderr.write("modellens: no endpoints configured; "
                         "write config.json or set MODELLENS_CONFIG\n")
    if args.once:
        tool_args: dict = {}
        for key in ("limit", "workers", "contains", "model", "endpoint", "profile"):
            val = getattr(args, key)
            if val is not None:
                tool_args[key] = val
        if args.check_tools:
            tool_args["check_tools"] = True
        if args.no_stream:
            tool_args["check_stream"] = False
        if args.all:
            tool_args["all"] = True
        return run_once(ctx, args.once, tool_args)
    return serve(ctx)


if __name__ == "__main__":
    raise SystemExit(main())