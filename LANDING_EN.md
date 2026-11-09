# ModelLens

**See which of your models actually works.**

ModelLens is an MCP server that probes, ranks, and recommends AI models so your gateway stops routing traffic to slow or broken models.

## The problem

A gateway once advertised 89 models. Its default routing group put an 87-second model first and a 0.9-second model third — users waited minutes for answers that should have taken under a second. ModelLens found the fast model in one pass.

## Features

| Tool | Price | What it returns |
|------|-------|-----------------|
| `list_models` | Free | The models your gateway advertises, with basic metadata. |
| `ranking` | $0.01 | Models ranked by latency, reliability, and cost. |
| `recommend` | $0.01 | A shortlist of models best suited to your routing group. |
| `report` | $0.02 | A detailed per-model probe report with timings and failures. |
| `probe` | $0.05 | A deep probe of a single model: latency, status codes, headers, body. |

## Pricing

Every call is metered per-call via x402. There is no subscription.

| Tool | Price |
|------|-------|
| `list_models` | Free |
| `ranking` | $0.01 per call |
| `recommend` | $0.01 per call |
| `report` | $0.02 per call |
| `probe` | $0.05 per call |

## How payment works

1. **Call** — Your MCP client sends a tool request to the hosted endpoint.
2. **402 challenge** — If the call requires payment, the server returns an HTTP 402 Payment Required with an x402 payment challenge.
3. **Agent signs** — Your agent signs an EIP-3009 USDC payment authorization on Base (eip155:8453) payable to `0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50`.
4. **Settle** — The facilitator settles the USDC transfer, and you receive the result plus the `PAYMENT-RESPONSE` header.

Network: Base (eip155:8453). Asset: USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
x402 manifest: https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/.well-known/x402

## Quick start

```ts
import { fetch } from "undici";

const res = await fetch(
  "https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "probe", arguments: { model: "gpt-4" } },
    }),
  }
);

// Paid tools return HTTP 402 with an x402 payment challenge.
// Inspect the challenge, sign an EIP-3009 USDC authorization on Base,
// then retry with the payment header. Signing logic omitted here.
console.log(res.status, await res.text());
```

## Self-host

Install from PyPI:

```bash
pip install modellens-mcp
```

Add ModelLens to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "modellens": {
      "command": "modellens-mcp",
      "args": []
    }
  }
}
```

ModelLens is also listed in the MCP Registry as `io.github.Greatbeing/modellens`.

## FAQ

**Do I need a subscription?**
No. Every call is priced individually via x402; there is no subscription. `list_models` is free; the other tools cost $0.01–$0.05 per call.

**Which blockchain does payment use?**
Base (eip155:8453), paying in USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) to `0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50`.

**What if the hosted backend is down?**
Self-host ModelLens with `pip install modellens-mcp` and point your MCP client at your own instance instead.

**How do I self-host?**
Run `pip install modellens-mcp`, then configure your MCP client to launch the `modellens-mcp` command (see the Self-host section), or add it from the MCP Registry as `io.github.Greatbeing/modellens`.

No revenue or uptime promises are made.