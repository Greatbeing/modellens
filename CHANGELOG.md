# Changelog

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
