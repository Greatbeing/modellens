# ModelLens

<!-- mcp-name: io.github.Greatbeing/modellens -->

**See which of your models actually works.**

An MCP server that turns any OpenAI-compatible gateway into an observable,
rankable set of models. It discovers what a provider advertises, probes what
works *right now* — latency, success, cancellation, streaming, tool calling —
keeps the history, and answers **"which model should I use?"**

Zero third-party dependencies. Python 3.9+ standard library only.

[![PyPI version](https://img.shields.io/pypi/v/modellens-mcp.svg)](https://pypi.org/project/modellens-mcp/)
[![PyPI downloads](https://img.shields.io/pypi/dm/modellens-mcp.svg)](https://pypistats.org/packages/modellens-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.Greatbeing%2Fmodellens-blue)](https://registry.modelcontextprotocol.io/servers/io.github.Greatbeing/modellens)
[![Glama](https://img.shields.io/badge/Glama-Modellens-orange)](https://glama.ai/mcp/servers/Greatbeing/modellens)
[![Live status](https://img.shields.io/badge/status-live_probe_report-blue)](https://greatbeing.github.io/modellens/)
[![Tests](https://github.com/Greatbeing/modellens/actions/workflows/test.yml/badge.svg)](https://github.com/Greatbeing/modellens/actions/workflows/test.yml)

- 中文落地页：[LANDING_ZH.md](LANDING_ZH.md)

---

## Why

If you run more than one model provider, you already have this problem:

- The gateway says it serves 89 models. How many actually answer?
- Which one is fast *today*? Yesterday's answer is already stale.
- Your router picks the first entry in a list — and the first entry is slow.
- Failures are invisible until a user complains.

ModelLens makes that visible. It is the difference between trusting a model
list and measuring it.

Real example from a live gateway (89 models, 16 probed):

```
magpie/kilo/nvidia/nemotron-3.5-super-vl:free     97.6  100.0%    0.0%      628ms   healthy
magpie/auto/beam-free                             96.6  100.0%    0.0%      909ms   healthy
magpie/longcat/LongCat-2.5                        88.4  100.0%    0.0%     3104ms   mixed
magpie/group/daily                                70.0  100.0%    0.0%    77345ms   mixed
magpie/radeon-cloud/DeepSeek-V4.1-Flash           70.0  100.0%    0.0%    87388ms   mixed
magpie/nvidia/deepseek-ai/deepseek-v4.1-flash     0.0    0.0%  100.0%         -    unreliable
magpie/modelscope/Qwen/Qwen3.8-27B                 0.0    0.0%    0.0%         -    quota_blocked
```

The default routing group was putting an 87-second model first and a 0.9-second
model third. ModelLens found that in one pass.

---

## Install

```bash
# from PyPI
pip install modellens-mcp
modellens --config config.json --data history.json --once ranking
```

or straight from source — no dependencies, no install step:

```bash
curl -fsSL https://raw.githubusercontent.com/Greatbeing/modellens/main/modellens.py -o modellens.py
python3 modellens.py --config config.json --data history.json --once ranking
```

## Configure

```json
{
  "endpoints": [
    {"name": "magpie",  "base_url": "http://127.0.0.1:3425/v1", "api_key": ""},
    {"name": "openai",  "base_url": "https://api.openai.com/v1", "api_key": "sk-..."},
    {"name": "ollama",  "base_url": "http://127.0.0.1:11434/v1"}
  ]
}
```

`api_key` may be empty for a localhost gateway. `models` optionally filters the
advertised list; `enabled: false` disables an endpoint without deleting it.

## Run as an MCP server

Point any MCP client at it:

```json
{
  "mcpServers": {
    "modellens": {
      "command": "python3",
      "args": ["/path/to/modellens.py",
               "--config", "/path/to/config.json",
               "--data",   "/path/to/history.json"]
    }
  }
}
```

Works with Claude Desktop, Cursor, Windsurf, VS Code, and any other MCP client.

### DeepSeek Harness

Register it in the profile's `cordis.patch.yml` (note: a new entry needs
`- insert:`, not a bare `- id:` — a bare one means "modify an existing entry"):

```yaml
- insert:
    - id: modellens
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "modellens"
        transport: "stdio"
        command: "python3"
        args:
          - "/data/workspace/modellens/modellens.py"
          - "--config"
          - "/data/workspace/modellens/config.json"
          - "--data"
          - "/data/workspace/modellens/history.json"
```

Then restart `dsh web`. Verified: the server runs as a child process of
`dsh-web` (`pgrep -P $(systemctl --user show dsh-web -p MainPID)`).

### Tools

| Tool | What it does |
|---|---|
| `list_models` | What each endpoint advertises (optional substring filter) |
| `probe` | Real requests: latency, success, streaming + TTFT, tool calls (`workers` optional) |
| `ranking` | Quality ranking from recorded history |
| `recommend` | Best model for a profile: `fast` (≥90% success) \| `cheap` (zero-priced if advertised) \| `capable` |
| `report` | The whole picture as markdown |

`ranking` / `recommend` / `report` score from recorded history but also do a
live read-only `GET /models` on each endpoint (no inference, no token cost).

### Example

```
probe(limit=20, check_tools=true, check_stream=true)
ranking(limit=10)
recommend(profile="fast")
```

`probe(workers=4)` runs probes concurrently (faster, but see the honest notes:
concurrency can self-induce 429s). Default is `workers=1` — sequential. `limit`
and `workers` are clamped (1–200, 1–16): `limit=-5` would otherwise slice
backwards and `workers=500` would fork 500 threads.

A probe only counts as success when the response is an actual completion. An
HTTP 200 that carries `{"error": ...}` (gateways wrap upstream failures like
this), `choices: []`, empty content, or a non-JSON body is recorded as a
failure with the reason — a status code alone is not evidence.

`check_stream` (on by default) opens a real `stream: true` request and measures
time-to-first-token (TTFT); `stream_ok` means the SSE stream delivered at least
one real token chunk and contained no error event.

### Hosted / paid (x402, USDC on Base)

A hosted endpoint runs the same tools over MCP streamable-HTTP and charges
per call with [x402](https://x402.org) — agents pay gasless USDC on Base, no
accounts or API keys:

- Endpoint: `https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp`
- Manifest: `/.well-known/x402` (payTo address, prices, facilitator)
- Prices: `list_models` free · `ranking`/`recommend` $0.01 · `report` $0.02 · `probe` $0.05
  (probe makes real upstream requests, so it costs the most)

An unpaid paid-tool call returns `HTTP 402` + `PAYMENT-REQUIRED`; the agent
signs an EIP-3009 USDC transfer and retries with `X-PAYMENT`, the facilitator
verifies the signature, the tool runs, and only then is the payment settled —
so a call that fails on our side (bad arguments, unknown model, upstream 5xx)
never costs the caller money. A successful call returns `PAYMENT-RESPONSE`.
Self-host the same server anywhere with `python3 server_x402.py`;
`X402_PAY_TO` (your receiving address) is **required** — the server refuses to
start without it rather than quietly paying a hardcoded address. Hosted-mode
caps: `X402_MAX_PROBE_LIMIT` (default 20) and `X402_MAX_PROBE_WORKERS`
(default 4) clamp what a paid caller can ask for, and browsers get CORS
headers plus an `OPTIONS` preflight on `/mcp`.

Verdicts: `healthy` (≥95% success, <3 s median) · `mixed` · `unreliable`
(≥30% cancelled) · `quota_blocked` (all 429) · `auth_error` (all 401/403 — a
key/permission problem, not a flaky model) · `dead` (all 404) · `failing` (0%
success from real errors such as 5xx/timeouts) · `stale` (not probed in the
last 6 h — the evidence is too old to route on, so the model is never
recommended) · `untested`.

---

## How it scores

Every probe is a real request, so the numbers are evidence, not estimates.

| Signal | Weight | Notes |
|---|---|---|
| Success rate | 50% | 0% success ⇒ score 0, regardless of anything else |
| Median latency | 30% | Linear to 0 at 8 s; unmeasured latency earns no credit |
| Cancellation rate | 20% | Timeouts and client cancellations |

History is kept per model (newest 200 probes) in `history.json`, so rankings
improve as you use it. One file can be shared by several writers — the CLI, the
MCP server and the hosted x402 server coordinate through an advisory file lock
and merge on write, so a concurrent probe batch is never clobbered; a history
file that cannot be parsed is copied aside as `history.json.corrupt-<ts>`
instead of being overwritten.

---

## Proxy: route on evidence

`modellens_proxy.py` is an OpenAI-compatible `/v1/chat/completions` proxy that
picks the model from what ModelLens measured, then fails over when upstream
misbehaves. Point your client at it and send `model: "auto"`:

```bash
python3 modellens.py --once probe --limit 20 --check-tools   # seed the evidence
python3 modellens_proxy.py --config config.json --data history.json
# → modellens-proxy on http://127.0.0.1:8766

curl -s localhost:8766/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto:fast","messages":[{"role":"user","content":"hi"}]}'
```

A **real model id passes through untouched**, so the proxy is drop-in: existing
clients keep working, and only `auto*` requests are routed.

| alias | picks |
|---|---|
| `auto` | the configured default profile |
| `auto:fast` | lowest median latency among models ≥90% success |
| `auto:capable` | highest quality score |
| `auto:cheap` | zero-priced models when the endpoint advertises pricing |
| `auto:balanced` | score per second of latency; with ≥20 samples the p95 tail counts too (median + 0.5 × (p95 − median)), so a steady model beats a spiky one |

Every response says what happened, so a routing decision is never a mystery:

```
X-Modellens-Model: gpt-4o          X-Modellens-Reason: best|sticky|explore
X-Modellens-Endpoint: a            X-Modellens-Attempts: 1
X-Modellens-Profile: fast          X-Modellens-Evidence: probe n=12, age=0.1h
X-Modellens-Fallback-From: a/mini  (only when a retry happened)
X-Modellens-Tokens: 1000+500       (non-stream only, from the upstream usage block)
X-Modellens-Cost-USD: 0.00125      (only when the endpoint advertises pricing)
```

`config.json` gains an optional `proxy` section:

```json
{
  "endpoints": [
    {"name": "a", "base_url": "http://127.0.0.1:3425/v1", "api_key": "sk-...",
     "capabilities": {"gpt-4o": {"tools": true, "json": true, "vision": true,
                                 "ctx": 128000}}}
  ],
  "proxy": {
    "listen": "127.0.0.1:8766",
    "profile": "capable",
    "table_ttl_s": 30,
    "max_attempts": 3,
    "explore_ratio": 0.05,
    "sticky_ttl_s": 1800,
    "probe": {"enabled": true, "interval_s": 900, "limit": 3, "check_tools": true},
    "pool": {"max_idle": 8, "idle_s": 60},
    "deny": ["*-preview"]
  }
}
```

Operational endpoints: `GET /modellens/table` (every candidate plus why it was
blocked), `GET /modellens/explain` (dry-run a decision), `GET /modellens/health`
(spend, breakers, pool), `GET /modellens/metrics` (Prometheus: requests,
failovers, tokens, cost per model, pool hits). `X-Modellens-Dry-Run: 1` returns
the decision without calling any upstream — the safe way to try it in production.

Behaviour worth knowing:

- **Failover only before the first byte.** After bytes reach the client a retry
  would duplicate output and the token bill, so the error is surfaced instead.
- **A 4xx is never retried** and never recorded as a model failure: it is the
  request's or the key's fault, and recording it would poison the score.
- **Capabilities are three-state.** `stream`/`tools` come from probes; `json`,
  `vision` and the context window cannot be measured, so declare them under
  `capabilities` (or set `permissive_caps: true` to route anyway). Requests that
  need a tool call skip models whose `tools` was never verified — enable
  `probe.check_tools` and the prober turns that "unknown" into a real answer.
- **Spend is accounted, not guessed.** Token counts come from the upstream
  `usage` block (including the final SSE chunk when the gateway sends one) and
  cost from the endpoint's advertised per-million pricing. Missing either yields
  no number rather than an invented one; totals land in `/modellens/metrics` and
  in each history row (`prompt_tokens`, `completion_tokens`, `cost_usd`).
- **Connections are pooled.** Chat requests reuse a keep-alive connection per
  endpoint (`pool.max_idle`, `pool.idle_s`), so the proxy does not pay a TCP/TLS
  handshake per call; a connection the peer closed while idle is retried once on
  a fresh socket instead of surfacing as an upstream failure.
- **Sessions stick.** A conversation stays on one model (`X-Modellens-Session`
  or a hash of the system prompt) so multi-turn quality does not flap.
- **Exploration is deliberate and bounded** (top-K, above the success bar), and
  the background prober re-measures the stalest models — without that the
  runner-up starves, goes stale and is gone exactly when it is needed.
- **Proxy traffic is recorded** with `source: "proxy"`, so routing decisions
  improve from real traffic while probe evidence stays the honest baseline.

---

### Per-request cost ceiling

```json
"proxy": { "budget": { "max_cost_usd": 0.05, "default_max_tokens": 1024, "allow_unpriced": false } }
```

Before forwarding, each candidate's worst-case cost is estimated as
`ctx_estimate x input_price + max_tokens x output_price` (per-million pricing; `default_max_tokens`
when the client sent no `max_tokens`). Candidates above the ceiling are skipped with `over_budget`;
candidates without both prices are skipped with `unpriced` unless `allow_unpriced` is set.
Clients can tighten (never loosen) the ceiling per request with `X-Modellens-Max-Cost: 0.002`;
`/modellens/explain?max_cost=...` shows the effect without calling upstream. No ceiling configured
and no header = no budget gate.

### Deploy it

- **Docker**: `docker compose -f deploy/docker-compose.yml up -d --build` — non-root
  image, read-only root, `/data` volume for history, healthcheck on
  `/modellens/health`. CI builds and runs the image on every push, so the
  Dockerfile cannot rot silently.
- **systemd**: `deploy/modellens-proxy.service` — hardened unit (`ProtectSystem=strict`,
  `StateDirectory=modellens`, `NoNewPrivileges`), listening on `127.0.0.1:8766`.
- **Seed first**: a fresh proxy has no history and answers `503 no_candidate`
  until something is probed — run `--once probe --limit 20 --check-tools` once.

Full steps, including the cron line that keeps evidence fresh: [`deploy/README.md`](deploy/README.md).

## CLI

The server doubles as a command-line tool:

```bash
python3 modellens.py --config config.json --data history.json --once probe --limit 20
python3 modellens.py --config config.json --data history.json --once ranking
python3 modellens.py --once recommend --profile fast
```

`--once` accepts the tool arguments as flags: `--limit N`, `--workers N`,
`--contains S`, `--model M`, `--endpoint E`, `--check-tools`, `--no-stream`,
`--profile P`, `--all`. Without `--once` the server speaks MCP over stdio.

---

## Use cases

- **Before you pick a model** — `recommend(profile="fast")`
- **When a provider goes quiet** — `ranking` shows it dropping before your users do
- **When your router misbehaves** — compare group members, find the slow one
- **Cost control** — spot the expensive model that is never chosen
- **Onboarding a new provider** — probe once, see what actually works

---

## Status and roadmap

Working today: discovery, probing, streaming/tool-call checks, scoring, ranking,
recommendation, markdown report, MCP stdio transport, CLI.

Roadmap: OpenAI-compatible `/v1/models` only for now; Anthropic and Gemini
transports, richer per-token cost accounting across more gateway field shapes,
alerting webhooks, and a hosted multi-team version.

---

## License

MIT. Use it, ship it, charge for your own hosted version.

---

## Honest notes

- **Probing costs tokens.** Each probe is a real completion. Keep `limit`
  sensible and don't probe a hundred models every minute.
- **A single probe is weak evidence.** The score is meaningful after several
  probes; the report says `n=1` so you can see the sample size.
- **A recommendation is only as fresh as its last probe.** A model not probed
  in the last 6 hours is marked `stale` and never recommended — on free tiers
  a model that was healthy this morning can be quota-blocked by noon. The
  ranking table shows how long ago each model was last probed; failure
  verdicts (`dead` / `quota_blocked` / `unreliable`) are kept even when old,
  because they say what the last probe found.
- **Latency depends on you.** Your network, not just the provider, is in the
  measurement. Run it from where your agents run.
- **`fast` keeps a ≥90% success bar.** If nothing qualifies it says so instead
  of handing you a flaky "fast" model.
- **`cheap` is honest about pricing.** It prefers models the endpoint advertises
  as $0 input/output; when no pricing is advertised it falls back to reliability
  then latency and says so — true per-token cost is not collected.
- **Probing is sequential by default.** `workers>1` is faster, but concurrent
  probes can self-induce 429s that misread a model's real quality.
- **HTTP 200 is not success.** Gateways often return 200 with `{"error": ...}`,
  empty `choices`, or an HTML page. Those are recorded as failures (with the
  transport reason, e.g. `timeout` / `dns_error`), because a status code alone
  would make a broken model look healthy.
- **`ranking`/`recommend`/`report` are read-only but not offline.** They GET
  each endpoint's `/models` list live (no inference, no token cost).

## Development

Zero dependencies, standard library only. Run the unit tests (network is
stubbed, so they are fast and deterministic):

```bash
python -m unittest discover -s tests -v
```

`tests/test_modellens.py` covers scoring, verdicts, p95, the history store,
recommendations, discovery/pricing parsing, response validation, the streaming
probe, argument parsing and the MCP wire protocol; `tests/test_server_x402.py`
exercises the hosted server over real HTTP and proves the payment ordering
(verify → execute → settle). CI runs the same suite on Python 3.9 and 3.12 on
every push and PR.

The hosted Cloudflare Worker (`worker/`) serves the read-only tools from KV; its
scoring is kept in lock-step with `modellens.py` (median latency, cancellation =
status 0/499, score 0 on 0% success, same verdicts including `auth_error` /
`failing`, grouping per endpoint+model). `worker/test/scoring.test.mjs` pins the
scoring and `worker/test/http.test.mjs` the HTTP layer (CORS, error hygiene,
ingest auth, payment ordering). Run `node --test` in `worker/`. Deploying
the worker requires `wrangler secret put INGEST_SECRET`; the secret is not
committed (and any value previously committed must be rotated).

## Hosted / paid

- Hosted landing page: [`LANDING_EN.md`](LANDING_EN.md)
- MCP server listing (MCPize): [`MCPIZE_SUBMISSION.md`](MCPIZE_SUBMISSION.md)
- x402 buyer guide: [`X402_BUYER_GUIDE.md`](X402_BUYER_GUIDE.md)
