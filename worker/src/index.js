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

// Scoring mirrors modellens.py score_model exactly: median latency (not mean),
// cancellation = status 0/499 (not a 'cancelled' string), score 0 on 0% success,
// dead/quota_blocked verdicts, and the stale verdict (evidence older than
// STALE_AFTER_HOURS is too old to route on). The hosted worker must not drift
// from the CLI/MCP core — same data must yield the same numbers.
const STALE_AFTER_HOURS = 6;

function parseTs(s) {
  if (typeof s !== 'string') return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

function formatAge(ageHours) {
  if (ageHours == null) return '-';
  if (ageHours < 1) return Math.max(1, Math.round(ageHours * 60)) + 'm';
  if (ageHours < 48) return Math.round(ageHours) + 'h';
  return Math.round(ageHours / 24) + 'd';
}
function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Match Python round(x, 1) for the decision fields (score / success / cancel).
// toFixed correctly rounds the actual binary value; at an exact .5 tenth apply
// ties-to-even like Python. The x*10 tie test is only reliable below ~1000 —
// above that, float ulp makes x*10 land on .5 spuriously — so large medians fall
// back to toFixed. Those can differ from Python by <=0.1ms at exact representable
// ties: a display-only nuance that never affects score, ranking or recommendation.
function round1(x) {
  const s = x * 10, f = Math.floor(s);
  if (x < 1000 && s - f === 0.5) return (f % 2 === 0 ? f : f + 1) / 10;
  return Number(x.toFixed(1));
}

function scoreModel(rows, nowMs) {
  const now = nowMs == null ? Date.now() : nowMs;
  const attempts = rows.length;
  const oks = rows.filter((r) => r.ok);
  const cancelled = rows.filter((r) => !r.ok && (r.status === 0 || r.status === 499)).length;
  const latencies = oks.map((r) => r.latency_ms).filter((x) => typeof x === 'number');
  const success_rate = attempts ? (oks.length / attempts) * 100 : 0;
  const cancel_rate = attempts ? (cancelled / attempts) * 100 : 0;
  const med = median(latencies);

  let lastTs = null;
  for (const r of rows) {
    const t = parseTs(r.probed_at);
    if (t != null && (lastTs == null || t > lastTs)) lastTs = t;
  }
  const age_hours = lastTs == null ? null : Math.max(0, Math.round(((now - lastTs) / 3600000) * 10) / 10);
  const last_probed_at = lastTs == null ? null : new Date(lastTs).toISOString().replace(/\.\d{3}Z$/, 'Z');

  let verdict;
  if (attempts === 0) verdict = 'untested';
  else if (success_rate === 0 && rows.every((r) => r.status === 404)) verdict = 'dead';
  else if (success_rate === 0 && rows.every((r) => r.status === 429)) verdict = 'quota_blocked';
  else if (cancel_rate >= 30) verdict = 'unreliable';
  else if (success_rate >= 95 && (med == null ? 9e9 : med) < 3000) verdict = 'healthy';
  else verdict = 'mixed';

  if (attempts && success_rate > 0 && (verdict === 'healthy' || verdict === 'mixed')
      && age_hours != null && age_hours > STALE_AFTER_HOURS) verdict = 'stale';

  if (attempts === 0 || success_rate === 0) {
    return { attempts, ok: 0, failed: attempts, cancelled, success_rate: 0,
             cancel_rate: round1(cancel_rate), median_latency_ms: null, score: 0, verdict,
             last_probed_at, age_hours };
  }
  const latency_score = Math.max(0, Math.min(1, 1 - med / 8000)) * 100;
  const score = (success_rate * 50 + latency_score * 30 + Math.max(0, 100 - cancel_rate * 2) * 20) / 100;
  return { attempts, ok: oks.length, failed: attempts - oks.length, cancelled,
           success_rate: round1(success_rate), cancel_rate: round1(cancel_rate),
           median_latency_ms: med == null ? null : round1(med), score: round1(score), verdict,
           last_probed_at, age_hours };
}

function aggregate(rows, nowMs) {
  const byModel = {};
  for (const r of rows || []) {
    if (!r.model) continue;
    (byModel[r.model] = byModel[r.model] || []).push(r);
  }
  const out = [];
  for (const [m, rs] of Object.entries(byModel)) out.push({ model: m, ...scoreModel(rs, nowMs) });
  out.sort((a, b) => b.score - a.score
    || (a.median_latency_ms ?? 1e12) - (b.median_latency_ms ?? 1e12)
    || a.model.localeCompare(b.model));
  return out;
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
  const rows = aggregate(JSON.parse(raw).rows);
  const limit = parseInt(args.limit || '30', 10);
  const lines = ['| model | score | success | cancel | median | n | verdict | age |', '| --- | --- | --- | --- | --- | --- | --- | --- |'];
  for (const r of rows.slice(0, limit)) {
    lines.push(`| ${r.model} | ${r.score} | ${r.success_rate}% | ${r.cancel_rate}% | ${r.median_latency_ms != null ? Math.round(r.median_latency_ms) + 'ms' : '-'} | ${r.attempts} | ${r.verdict} | ${formatAge(r.age_hours)} |`);
  }
  const stale = rows.filter((r) => r.verdict === 'stale').length;
  if (stale) lines.push(`\n(${stale} stale — not probed in the last ${STALE_AFTER_HOURS}h, never recommended)`);
  return lines.join('\n');
}

async function toolRecommend(env, args) {
  const raw = await env.DATA.get('history');
  if (!raw) return 'no probe history yet';
  const rows = aggregate(JSON.parse(raw).rows);
  let usable = rows.filter((r) => r.score > 0 && r.verdict !== 'dead' && r.verdict !== 'quota_blocked' && r.verdict !== 'stale');
  if (!usable.length) {
    if (rows.some((r) => r.verdict === 'stale')) return `all models with a passing score are stale (not probed in the last ${STALE_AFTER_HOURS}h) — probe again for a fresh recommendation`;
    return 'no model has a passing score yet';
  }
  const profile = (args.profile || 'capable').trim().toLowerCase();
  let note = '';
  if (profile === 'fast') {
    const ok = usable.filter((r) => r.success_rate >= 90);
    if (!ok.length) return "no model meets the 90% success bar for 'fast'";
    ok.sort((a, b) => (a.median_latency_ms ?? 1e12) - (b.median_latency_ms ?? 1e12));
    usable = ok;
  } else if (profile === 'cheap') {
    usable.sort((a, b) => b.success_rate - a.success_rate || (a.median_latency_ms ?? 1e12) - (b.median_latency_ms ?? 1e12));
    note = '\n\nNo pricing in snapshot; fell back to reliability then latency (true cost not collected).';
  } else {
    usable.sort((a, b) => b.score - a.score || (a.median_latency_ms ?? 1e12) - (b.median_latency_ms ?? 1e12));
  }
  const pick = usable[0];
  const runner = usable[1];
  return `**Recommended (${profile}): \`${pick.model}\`** — score ${pick.score}, success ${pick.success_rate}%${pick.median_latency_ms != null ? ', median ' + Math.round(pick.median_latency_ms) + 'ms' : ''}, verdict ${pick.verdict} (n=${pick.attempts}, probed ${formatAge(pick.age_hours)} ago).\n\nRunner-up: ${runner ? '`' + runner.model + '` (score ' + runner.score + ')' : 'n/a'}.${note}`;
}

async function toolReport(env) {
  const raw = await env.DATA.get('history');
  if (!raw) return 'no probe history yet';
  const h = JSON.parse(raw);
  const rows = aggregate(h.rows);
  const counts = {};
  for (const r of rows) counts[r.verdict] = (counts[r.verdict] || 0) + 1;
  const lines = [
    '# ModelLens report',
    '',
    `- endpoints: 1`,
    `- models discovered: ${rows.length}`,
    `- probes recorded: ${(h.rows || []).length}`,
    '',
    '## Verdicts',
    ...['healthy', 'mixed', 'unreliable', 'quota_blocked', 'dead', 'stale', 'untested'].filter((k) => counts[k]).map((k) => `- ${k}: ${counts[k]}`),
    '',
    '## Top 15 by score',
    '',
    '| model | score | success | cancel | median | n | verdict | age |',
    '| --- | --- | --- | --- | --- | --- | --- | --- |',
    ...rows.slice(0, 15).map((r) => `| ${r.model} | ${r.score} | ${r.success_rate}% | ${r.cancel_rate}% | ${r.median_latency_ms != null ? Math.round(r.median_latency_ms) + 'ms' : '-'} | ${r.attempts} | ${r.verdict} | ${formatAge(r.age_hours)} |`),
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
      serverInfo: { name: 'modellens-x402', version: '0.1.5' },
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
      return Response.json({ ok: true, server: 'modellens-x402-worker', version: '0.1.5' });
    }

    // ingest: being pushes history + models snapshots
    if (url.pathname === '/ingest' && req.method === 'POST') {
      const secret = env.INGEST_SECRET;
      if (!secret || req.headers.get('X-Ingest-Secret') !== secret) {
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

// Named exports for unit tests (wrangler only uses the default export).
export { median, scoreModel, aggregate, round1 };