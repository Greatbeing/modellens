# Changelog

## 0.2.1 - 2026-11-24

### Changed
- Scoring: success now counts as the 95% Wilson lower bound, not the raw rate,
  so one lucky probe (1/1, ~21%) can no longer outrank a model proven over 40
  probes. Rows carry `success_lower_bound` and `low_evidence` (< 5 probes).
  The hosted worker scores identically.
- Proxy prober: probes once right after start-up (`probe.warmup_limit`,
  default 10, 0 = off) so a fresh proxy stops answering `503 no_candidate`
  for a whole interval, and now targets untested/stale models too, thinnest
  evidence first.

### Added
- `deploy/`: a proxy Dockerfile (non-root, read-only root, `/data` volume,
  healthcheck), a Compose file, a hardened systemd unit and install notes. CI
  builds and smoke-runs the image on every push (job `docker`), and the
  Dockerfile's packaging steps were verified locally (wheel → console scripts →
  `modellens-proxy --once`).

### Changed
- **modellens-proxy**: `auto:balanced` now penalises latency tails. Once a
  model has ≥20 latency samples, its effective latency is
  `median + 0.5 × (p95 − median)` (`P95_TAIL_WEIGHT`), so at equal medians a
  steady model outranks a spiky one. Without a p95 the median stands alone.

## 0.2.0 - 2026-11-23

### Added
- **modellens-proxy**: OpenAI-compatible proxy that routes on measured evidence.
  `auto`, `auto:fast|cheap|capable|balanced` profiles, three-state capability
  gating, sticky sessions, bounded exploration, circuit breaker, failover before
  the first byte (4xx is relayed, never retried), chunk-by-chunk SSE relay with
  TTFT, dry-run, `/modellens/table|explain|health|metrics`.
- Background prober can learn tool support (`probe.check_tools`).
- Token and USD cost accounting per request (history rows, response headers,
  metrics). Unknown prices yield no number rather than a guess.
- Keep-alive upstream connection pool (`pool.max_idle`, `pool.idle_s`) with one
  retry on a stale pooled connection.
- Per-request cost ceiling (`budget.max_cost_usd`, `X-Modellens-Max-Cost`);
  the header can only tighten the configured cap.

### Fixed
- Shared history file is safe for several writers (lock + atomic write).
- x402: two-phase verify -> execute -> settle; never re-charge delivered work.
- Aggregation groups by (endpoint, model) without string keys or sentinels.
- Honesty, safety and transport fixes across core, hosted server and worker.
