// ModelLens x402 hosted MCP — Cloudflare Worker (always-on, no suspend).
//
// Paid tools (x402, USDC on Base):
//   list_models  free   -> reads KV snapshot
//   ranking      $0.01  -> reads KV history
//   recommend    $0.01  -> reads KV history
//   report       $0.02  -> reads KV history
//   probe        $0.05  -> forwards to BACKEND_MCP (real upstream requests)
//
// KV keys:
//   history  = modellens history.json (rows[]), pushed by being every 2h
//   models   = { "endpoint": "magpie", "models": [...] } snapshot
//
// Ingest: POST /ingest with header X-Ingest-Secret to update KV.

const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const NETWORK = 'eip155:8453';

const PRICES = {
  list_models: 0,
  ranking: 10000,
  recommend: 10000,
  report: 20000,
  probe: 50000,
};

const TOOLS = [
  {
    name: 'list_models',
    description:
      'List the models each configured endpoint advertises (from the latest KV snapshot). Read-only, free. Use first, then ranking/recommend.',
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: 'object',
      properties: {
        contains: { type: 'string', description: 'optional substring filter (matches model id)' },
        limit: { type: 'string', description: 'optional max models per endpoint' },
      },
      required: [],
    },
    x402Price: 'free',
  },
  {
    name: 'probe',
    description:
      'Probe models with REAL chat-completion requests: latency, success, streaming, tool calling. Side effects: consumes upstream tokens/quota (open-world). Forwarded to the live backend. Paid $0.05.',
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        endpoint: { type: 'string', description: 'optional endpoint name' },
        model: { type: 'string', description: 'exact model id' },
        contains: { type: 'string', description: 'substring filter on model id' },
        limit: { type: 'string', description: 'max models (default 20)' },
        check_tools: { type: 'string', description: 'also test function calling' },
      },
      required: [],
    },
    x402Price: '$0.05',
  },
  {
    name: 'ranking',
    description:
      'Quality ranking computed from recorded probe history (read-only, from KV). Ordered best-first: score = success 50% + median latency 30% + cancellation 20%. Paid $0.01.',
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: 'object',
      properties: { limit: { type: 'string', description: 'rows to show (default 30)' } },
      required: [],
    },
    x402Price: '$0.01',
  },
  {
    name: 'recommend',
    description:
      "Return the single best model for a task profile: 'fast' (lowest latency), 'cheap', or 'capable' (highest quality). Read-only from KV. Paid $0.01.",
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: 'object',
      properties: { profile: { type: 'string', description: 'fast | cheap | capable (optional)' } },
      required: [],
    },
    x402Price: '$0.01',
  },
  {
    name: 'report',
    description:
      'Return the whole picture as markdown: endpoints, verdict counts, top models by score, full per-model table. Read-only from KV. Paid $0.02.',
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: { type: 'object', properties: {}, required: [] },
    x402Price: '$0.02',
  },
];

function jsonrpc(id, result) {
  return { jsonrpc: '2.0', id, result };
}
function jsonrpcError(id, code, message, data) {
  return { jsonrpc: '2.0', id, error: { code, message, ...(data ? { data } : {}) } };
}

function b64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function unb64url(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function paymentRequirements(amount, resource, description, payTo) {
  return {
    scheme: 'exact',
    network: NETWORK,
    maxAmountRequired: String(amount),
    resource,
    description,
    mimeType: 'application/json',
    payTo,
    maxTimeoutSeconds: 60,
    asset: USDC_BASE,
    extra: { name: 'USDC', version: '2' },
  };
}

async function facilitator(env, path, body) {
  const r = await fetch(env.X402_FACILITATOR + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

async function verifyAndSettle(env, xPayment, req) {
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(unb64url(xPayment)));
  } catch (e) {
    return { ok: false, error: 'bad X-PAYMENT: ' + e.message };
  }
  const body = { x402Version: 1, paymentPayload: payload, paymentRequirements: req };
  try {
    const v = await facilitator(env, '/verify', body);
    if (!v.isValid) return { ok: false, error: 'invalid payment: ' + JSON.stringify(v).slice(0, 200) };
    const s = await facilitator(env, '/settle', body);
    if (!s.success) return { ok: false, tx: s.transaction, error: 'settle failed: ' + JSON.stringify(s).slice(0, 200) };
    return { ok: true, tx: s.transaction };
  } catch (e) {
    return { ok: false, error: 'facilitator error: ' + e.message };
  }
}

// ---- read-only tools (from KV) ----

function aggregate(rows) {
  const agg = {};
  for (const r of rows || []) {
    const m = r.model;
    if (!m) continue;
    const a = (agg[m] = agg[m] || { ok: 0, n: 0, latSum: 0, latN: 0, cancels: 0 });
    a.n += 1;
    if (r.ok) {
      a.ok += 1;
      if (typeof r.latency_ms === 'number') {
        a.latSum += r.latency_ms;
        a.latN += 1;
      }
    }
    if (r.status === 'cancelled' || r.error === 'cancelled') a.cancels += 1;
  }
  const out = [];
  for (const [m, a] of Object.entries(agg)) {
    const succ = a.n ? (a.ok / a.n) * 100 : 0;
    const lat = a.latN ? a.latSum / a.latN : null;
    const score = a.n ? succ * 0.5 + (lat != null ? Math.max(0, 100 - (lat / 8000) * 100) * 0.3 : 0) + (100 - (a.cancels / a.n) * 100) * 0.2 : 0;
    out.push({ model: m, score: +score.toFixed(1), success: +succ.toFixed(1), latency: lat, n: a.n });
  }
  out.sort((a, b) => b.score - a.score || (a.latency ?? 1e9) - (b.latency ?? 1e9));
  return out;
}

function verdict(r) {
  if (r.success >= 95 && (r.latency ?? 0) < 3000) return 'healthy';
  if (r.success === 0 && r.n > 0) return 'unreliable';
  if (r.success < 50) return 'unreliable';
  if (r.success < 95) return 'mixed';
  return 'healthy';
}

async function toolListModels(env, args) {
  const raw = await env.DATA.get('models');
  if (!raw) return 'no model snapshot yet (being pushes every 2h)';
  const snap = JSON.parse(raw);
  const pattern = (args.contains || '').trim().toLowerCase();
  let models = snap.models || [];
  if (pattern) models = models.filter((m) => m.toLowerCase().includes(pattern));
  const limit = parseInt(args.limit || '0', 10);
  if (limit) models = models.slice(0, limit);
  return `## ${snap.endpoint || 'endpoint'}  (${snap.base_url || ''}) — ${models.length} models\n` + models.map((m) => '- ' + m).join('\n');
}

async function toolRanking(env, args) {
  const raw = await env.DATA.get('history');
  if (!raw) return 'no probe history yet';
  const h = JSON.parse(raw);
  const rows = aggregate(h.rows);
  const limit = parseInt(args.limit || '30', 10);
  const top = rows.slice(0, limit);
  const lines = ['| model | score | success | median | n | verdict |', '| --- | --- | --- | --- | --- | --- |'];
  for (const r of top) lines.push(`| ${r.model} | ${r.score} | ${r.success}% | ${r.latency != null ? Math.round(r.latency) + 'ms' : '-'} | ${r.n} | ${verdict(r)} |`);
  return lines.join('\n');
}

async function toolRecommend(env, args) {
  const raw = await env.DATA.get('history');
  if (!raw) return 'no probe history yet';
  const rows = aggregate(JSON.parse(raw).rows);
  if (!rows.length) return 'no data';
  const profile = (args.profile || 'capable').trim().toLowerCase();
  let pick;
  if (profile === 'fast') {
    const withLat = rows.filter((r) => r.latency != null);
    pick = withLat.sort((a, b) => a.latency - b.latency)[0];
  } else {
    pick = rows[0];
  }
  return `**Recommended (${profile}): \`${pick.model}\`** — score ${pick.score}, success ${pick.success}%${pick.latency != null ? ', median ' + Math.round(pick.latency) + 'ms' : ''}, verdict ${verdict(pick)} (n=${pick.n}).\n\nRunner-up: ${rows[1] ? '`' + rows[1].model + '` (score ' + rows[1].score + ')' : 'n/a'}.`;
}

async function toolReport(env) {
  const raw = await env.DATA.get('history');
  if (!raw) return 'no probe history yet';
  const h = JSON.parse(raw);
  const rows = aggregate(h.rows);
  const counts = { healthy: 0, mixed: 0, unreliable: 0 };
  for (const r of rows) counts[verdict(r)] = (counts[verdict(r)] || 0) + 1;
  const lines = [
    '# ModelLens report',
    '',
    `- endpoints: 1`,
    `- models discovered: ${rows.length}`,
    `- probes recorded: ${(h.rows || []).length}`,
    '',
    '## Verdicts',
    ...Object.entries(counts).map(([k, v]) => `- ${k}: ${v}`),
    '',
    '## Top 15 by score',
    '',
    '| model | score | success | median | n | verdict |',
    '| --- | --- | --- | --- | --- | --- |',
    ...rows.slice(0, 15).map((r) => `| ${r.model} | ${r.score} | ${r.success}% | ${r.latency != null ? Math.round(r.latency) + 'ms' : '-'} | ${r.n} | ${verdict(r)} |`),
    '',
    `_Snapshot pushed ${h.updated_at || 'unknown'}_`,
  ];
  return lines.join('\n');
}

async function toolProbe(env, args) {
  // Forward the probe to the live backend (which can reach the local magpie gateway).
  const backend = env.BACKEND_MCP;
  try {
    const r = await fetch(backend, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'probe', arguments: args } }),
    });
    const text = await r.text();
    if (r.status === 402) return 'backend requires its own payment: ' + text.slice(0, 200);
    return text.slice(0, 4000);
  } catch (e) {
    return 'backend offline (being suspended?). probe is unavailable right now: ' + e.message;
  }
}

const FREE_TOOLS = { list_models: toolListModels, ranking: toolRanking, recommend: toolRecommend, report: toolReport };
const PAID_TOOLS = { probe: toolProbe };

async function runTool(env, name, args) {
  if (FREE_TOOLS[name]) return FREE_TOOLS[name](env, args);
  if (PAID_TOOLS[name]) return PAID_TOOLS[name](env, args);
  return 'unknown tool';
}

async function handleMcp(env, req, msg) {
  const method = msg.method;
  const id = msg.id;
  const host = new URL(req.url).host;
  const resource = `https://${host}/mcp`;

  if (method === 'initialize') {
    return jsonrpc(id, {
      protocolVersion: '2025-06-18',
      capabilities: { tools: {} },
      serverInfo: { name: 'modellens-x402', version: '0.1.3' },
    });
  }
  if (method && method.startsWith('notifications/')) return null;
  if (method === 'ping') return jsonrpc(id, {});
  if (method === 'tools/list') return jsonrpc(id, { tools: TOOLS });

  if (method === 'tools/call') {
    const name = (msg.params || {}).name;
    const args = (msg.params || {}).arguments || {};
    const price = PRICES[name] || 0;

    if (price > 0) {
      const xpay = req.headers.get('X-PAYMENT') || req.headers.get('x-payment');
      const req402 = paymentRequirements(price, resource, `ModelLens ${name}`, env.X402_PAY_TO);
      if (!xpay) {
        const challenge = { x402Version: 1, accepts: [req402], error: 'payment required' };
        return {
          status: 402,
          headers: { 'PAYMENT-REQUIRED': b64url(new TextEncoder().encode(JSON.stringify(challenge))) },
          body: jsonrpcError(id, 402, 'payment required', { x402: challenge }),
        };
      }
      const res = await verifyAndSettle(env, xpay, req402);
      if (!res.ok) return { status: 402, body: jsonrpcError(id, 402, res.error) };
      const text = await runTool(env, name, args);
      return {
        status: 200,
        headers: { 'PAYMENT-RESPONSE': b64url(new TextEncoder().encode(JSON.stringify({ x402Version: 1, transaction: res.tx, network: NETWORK }))) },
        body: jsonrpc(id, { content: [{ type: 'text', text }] }),
      };
    }

    const text = await runTool(env, name, args);
    return { status: 200, body: jsonrpc(id, { content: [{ type: 'text', text }] }) };
  }
  return jsonrpcError(id, -32601, 'method not found');
}

export default {
  async fetch(req, env) {
    try {
      return await this._fetch(req, env);
    } catch (e) {
      return new Response(
        'worker error: ' + (e && e.stack ? e.stack : String(e)),
        { status: 500, headers: { 'Content-Type': 'text/plain' } }
      );
    }
  },
  async _fetch(req, env) {
    const url = new URL(req.url);

    if (url.pathname === '/.well-known/x402' && req.method === 'GET') {
      return Response.json({
        x402Version: 1,
        operator: 'Greatbeing/modellens',
        network: NETWORK,
        asset: USDC_BASE,
        payTo: env.X402_PAY_TO,
        facilitator: env.X402_FACILITATOR,
        endpoint: `https://${url.host}/mcp`,
        tools: Object.fromEntries(TOOLS.map((t) => [t.name, t.x402Price])),
      });
    }

    if (url.pathname === '/health' && req.method === 'GET') {
      return Response.json({ ok: true, server: 'modellens-x402-worker', version: '0.1.3' });
    }

    // ingest: being pushes history + models snapshots
    if (url.pathname === '/ingest' && req.method === 'POST') {
      if (req.headers.get('X-Ingest-Secret') !== env.INGEST_SECRET) {
        return new Response('unauthorized', { status: 401 });
      }
      const body = await req.json();
      if (body.history) await env.DATA.put('history', JSON.stringify(body.history));
      if (body.models) await env.DATA.put('models', JSON.stringify(body.models));
      return Response.json({ ok: true });
    }

    if (url.pathname === '/mcp' && req.method === 'POST') {
      let msg;
      try {
        msg = await req.json();
      } catch {
        return Response.json({ error: 'bad json' }, { status: 400 });
      }
      const out = await handleMcp(env, req, msg);
      if (out === null) return new Response(null, { status: 202 });
      return Response.json(out.body, { status: out.status || 200, headers: out.headers || {} });
    }

    return new Response('not found', { status: 404 });
  },
};