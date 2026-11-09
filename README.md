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
| `probe` | Real requests: latency, success, streaming, tool calls |
| `ranking` | Quality ranking from recorded history |
| `recommend` | Best model for a profile: `fast` \| `cheap` \| `capable` |
| `report` | The whole picture as markdown |

### Example

```
probe(limit=20, check_tools=true)
ranking(limit=10)
recommend(profile="fast")
```

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
verifies/settles, and the tool result comes back with a `PAYMENT-RESPONSE`.
Self-host the same server anywhere with `python3 server_x402.py`
(set `X402_PAY_TO` to your receiving address; see `server_x402.py` header).

---

## How it scores

Every probe is a real request, so the numbers are evidence, not estimates.

| Signal | Weight | Notes |
|---|---|---|
| Success rate | 50% | 0% success ⇒ score 0, regardless of anything else |
| Median latency | 30% | Linear to 0 at 8 s |
| Cancellation rate | 20% | Timeouts and client cancellations |

Verdicts: `healthy` (≥95% success, <3 s median) · `mixed` · `unreliable`
(≥30% cancelled) · `quota_blocked` (all 429) · `dead` (all 404) · `untested`.

History is kept per model (newest 200 probes) in `history.json`, so rankings
improve as you use it.

---

## CLI

The server doubles as a command-line tool:

```bash
python3 modellens.py --config config.json --data history.json --once probe --limit 20
python3 modellens.py --config config.json --data history.json --once ranking
```

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
transports, per-endpoint cost accounting, alerting webhooks, and a hosted
multi-team version.

---

## License

MIT. Use it, ship it, charge for your own hosted version.

---

## Honest notes

- **Probing costs tokens.** Each probe is a real completion. Keep `limit`
  sensible and don't probe a hundred models every minute.
- **A single probe is weak evidence.** The score is meaningful after several
  probes; the report says `n=1` so you can see the sample size.
- **Latency depends on you.** Your network, not just the provider, is in the
  measurement. Run it from where your agents run.

## Hosted / paid

- Hosted landing page: [`LANDING_EN.md`](LANDING_EN.md)
- MCP server listing (MCPize): [`MCPIZE_SUBMISSION.md`](MCPIZE_SUBMISSION.md)
- x402 buyer guide: [`X402_BUYER_GUIDE.md`](X402_BUYER_GUIDE.md)