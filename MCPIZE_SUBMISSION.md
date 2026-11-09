# Modellens — MCPize Submission

## Server name

modellens

## Tagline

See which of your models actually works. (46 chars)

## Long description

Finding the right model for your gateway is hard. Provider catalogs list dozens of models, but the advertised names don't tell you which ones actually respond, how fast they are, or whether they produce correct output for your prompts. Routing requests blindly to a slow or failing model wastes time and money.

Modellens is an MCP server that discovers, probes (with real requests), ranks, and recommends models across OpenAI-compatible gateways. It exposes five MCP tools:

- **list_models** — enumerate every model a gateway advertises.
- **probe** — send real requests to models and measure latency, errors, and output quality.
- **ranking** — order models by measured performance.
- **recommend** — pick the best model for your use case.
- **report** — generate a shareable summary of probe results for your team.

Real-world impact: one gateway advertised 89 models. Its default routing group put an 87-second model first and a 0.9-second model third. ModelLens found the fast model in a single pass.

Five tools, per-call pricing via x402 (USDC on Base). List is free; ranked discovery and recommendations cost cents per call. Self-hosting is also free via `pip install modellens-mcp`.

## Pricing table

| Tool | Price (per call) | Payment |
|------|------------------|---------|
| list_models | Free | — |
| ranking | $0.01 | x402, USDC on Base |
| recommend | $0.01 | x402, USDC on Base |
| report | $0.02 | x402, USDC on Base |
| probe | $0.05 | x402, USDC on Base |

Note: All paid tools are charged per call via x402 (USDC on Base). No subscriptions.

## Endpoint + manifest URLs

- Hosted MCP endpoint: https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp
- x402 manifest: https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/.well-known/x402

## Categories & tags

**Category:** AI & Machine Learning

**Tags:** API Testing, Monitoring, Developer Tools

## Why pay for this

Gateway model lists are advertised, not verified. Modellens sends real requests to measure latency, errors, and quality so you route traffic to models that actually work. For a few cents per call you avoid slow timeouts, failed generations, and wasted spend on underperforming models — and get a shareable report your team can trust.

## Self-host alternative

`pip install modellens-mcp`

Self-hosting Modellens is free. Use the hosted endpoint if you want managed access with x402 billing; self-host if you prefer to run it on your own infrastructure.

## Suggested listing checklist

- Server name: paste "modellens"
- Registry name: paste "io.github.Greatbeing/modellens"
- Tagline: paste "See which of your models actually works."
- Long description: paste the Long description section above (<=300 words)
- Pricing: enter each tool and price from the Pricing table; mark per-call, x402, USDC on Base
- Endpoint: paste the hosted MCP endpoint URL
- Manifest: paste the x402 manifest URL
- Category: select "AI & Machine Learning"
- Tags: add "API Testing", "Monitoring", "Developer Tools"
- Self-host info: mention `pip install modellens-mcp` and that self-hosting is free