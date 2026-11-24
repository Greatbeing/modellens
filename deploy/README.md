# Deploying the ModelLens proxy

Two supported shapes. Both keep `history.json` on persistent storage: that file
is the evidence the router learns from, and throwing it away means starting from
"no data" (a fresh proxy answers `503 no_candidate` until something is probed).

## Docker / Compose

```bash
cp config.example.json deploy/config.json     # then edit endpoints
docker compose -f deploy/docker-compose.yml up -d --build
curl -s localhost:8766/modellens/health
```

Or without Compose:

```bash
docker build -f deploy/proxy.Dockerfile -t modellens-proxy .
docker run --rm -p 127.0.0.1:8766:8766 \
  -v "$PWD/config.json:/config/config.json:ro" \
  -v modellens-data:/data \
  modellens-proxy
```

The image runs as uid 10001 (non-root), keeps only `/data` writable, and has a
`HEALTHCHECK` against `/modellens/health`. CI builds and runs this image on every
push (`.github/workflows/test.yml`, job `docker`), so a broken Dockerfile does
not reach `main` silently.

## systemd

```bash
sudo useradd --system --home /var/lib/modellens --shell /usr/sbin/nologin modellens
sudo install -d -o modellens -g modellens /etc/modellens /var/lib/modellens
sudo cp config.example.json /etc/modellens/config.json      # edit endpoints
sudo install -m 0644 deploy/modellens-proxy.service /etc/systemd/system/
sudo install -m 0644 deploy/modellens-proxy.env.example /etc/modellens/proxy.env
sudo systemctl daemon-reload && sudo systemctl enable --now modellens-proxy
systemctl status modellens-proxy --no-pager
```

The unit runs as `modellens`, keeps `ProtectSystem=strict` with only
`/var/lib/modellens` writable (`StateDirectory=` creates it), and needs nothing
beyond TCP. It listens on `127.0.0.1:8766`; put a reverse proxy in front if the
endpoint must be reachable from other hosts, and set `proxy.admin_token` in
`config.json` when you do — `/modellens/*` is otherwise open.

## Seeding evidence before the first request

With `proxy.probe.enabled: true` (as in `config.example.json`) the proxy probes
up to `probe.warmup_limit` models (default 10) right after start-up, then tops
up every `interval_s`, thinnest evidence first. Expect `503 no_candidate` only
for the few seconds that first round takes.

With probing disabled, a brand-new proxy has no history and `auto` requests get
`503 no_candidate` until you probe once with the same config:

```bash
modellens --config /etc/modellens/config.json \
          --data /var/lib/modellens/history.json \
          --once probe --limit 20 --check-tools
```

Then either leave `probe.enabled: true` for scheduled top-ups, or let the probe
run from cron:

```
*/30 * * * * modellens --config /etc/modellens/config.json --data /var/lib/modellens/history.json --once probe --limit 5 --check-tools
```

Concurrent writers are safe: the history store locks and merges on write, so a
cron probe and the running proxy can share one file.
