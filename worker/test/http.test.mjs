// HTTP layer: CORS, error hygiene, ingest auth and the two-phase payment order.
// Run: node --test test/   (from worker/, zero dependencies)
import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { timingSafeEqual, paymentRequirements, PROBE_TIMEOUT_S } from '../src/index.js';

const PAYLOAD = btoa(JSON.stringify({ x402Version: 1, scheme: 'exact', payload: { sig: '0xdead' } }));

function memoryKV() {
  const store = new Map();
  return {
    store,
    async get(k) { return store.has(k) ? store.get(k) : null; },
    async put(k, v) { store.set(k, v); },
  };
}

function env(over = {}) {
  return {
    DATA: memoryKV(),
    X402_PAY_TO: '0x000000000000000000000000000000000000dEaD',
    X402_FACILITATOR: 'https://facilitator.test',
    BACKEND_MCP: 'https://backend.test/mcp',
    INGEST_SECRET: 's3cret',
    ...over,
  };
}

const mcpReq = (body, headers = {}) =>
  new Request('https://worker.test/mcp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });

test('CORS preflight answers 204 instead of 404', async () => {
  const res = await worker.fetch(new Request('https://worker.test/mcp', { method: 'OPTIONS' }), env());
  assert.equal(res.status, 204);
  assert.equal(res.headers.get('Access-Control-Allow-Origin'), '*');
  assert.match(res.headers.get('Access-Control-Allow-Headers'), /X-PAYMENT/);
});

test('MCP responses are readable from a browser', async () => {
  const res = await worker.fetch(mcpReq({ jsonrpc: '2.0', id: 1, method: 'initialize' }), env());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('Access-Control-Allow-Origin'), '*');
  assert.match(res.headers.get('Access-Control-Expose-Headers'), /PAYMENT-REQUIRED/);
});

test('a crash returns a bare error, never a stack trace', async () => {
  // env has no DATA binding, so a free tool throws inside _fetch.
  const res = await worker.fetch(mcpReq({
    jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'list_models', arguments: {} },
  }), {});
  assert.equal(res.status, 500);
  const body = await res.text();
  assert.equal(body, 'worker error');
  assert.doesNotMatch(body, /at |\.js:\d+|stack/i);
});

test('non-object JSON-RPC is rejected without throwing', async () => {
  for (const bad of [[], 'hi', 7, null]) {
    const res = await worker.fetch(mcpReq(bad), env());
    assert.equal(res.status, 200, JSON.stringify(bad));
    const out = await res.json();
    assert.equal(out.error.code, -32600, JSON.stringify(bad));
  }
});

test('timingSafeEqual compares correctly', async () => {
  assert.equal(await timingSafeEqual('abc', 'abc'), true);
  assert.equal(await timingSafeEqual('abc', 'abd'), false);
  assert.equal(await timingSafeEqual('abc', 'abcdef'), false); // length must not leak
  assert.equal(await timingSafeEqual('', ''), true);
});

test('ingest requires the secret and accepts the right one', async () => {
  const e = env();
  const post = (secret) => worker.fetch(new Request('https://worker.test/ingest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(secret == null ? {} : { 'X-Ingest-Secret': secret }) },
    body: JSON.stringify({ models: { models: ['m'] } }),
  }), e);

  assert.equal((await post(null)).status, 401);
  assert.equal((await post('wrong')).status, 401);
  assert.equal((await post('s3cret')).status, 200);
  assert.ok(e.DATA.store.has('models'));
});

test('ingest is closed when the secret is unset', async () => {
  const res = await worker.fetch(new Request('https://worker.test/ingest', {
    method: 'POST',
    headers: { 'X-Ingest-Secret': '' },
    body: '{}',
  }), env({ INGEST_SECRET: undefined }));
  assert.equal(res.status, 401);
});

test('paid tool without payment returns 402 with a challenge', async () => {
  const res = await worker.fetch(mcpReq({
    jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'probe', arguments: {} },
  }), env());
  assert.equal(res.status, 402);
  assert.ok(res.headers.get('PAYMENT-REQUIRED'));
});

test('probe authorization outlives the call; quick tools stay short', () => {
  // Settlement happens after execution: a 60 s window expired mid-probe and
  // the operator was never paid.
  const probe = paymentRequirements(50000, 'https://w/mcp', 'ModelLens probe',
                                    '0xabc', PROBE_TIMEOUT_S);
  assert.ok(probe.maxTimeoutSeconds >= 300, `got ${probe.maxTimeoutSeconds}`);
  const quick = paymentRequirements(10000, 'https://w/mcp', 'ModelLens ranking', '0xabc');
  assert.equal(quick.maxTimeoutSeconds, 60);
});

test('payment is verified, then the tool runs, then it settles', async () => {
  const order = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.endsWith('/verify')) { order.push('verify'); return Response.json({ isValid: true }); }
    if (u.endsWith('/settle')) { order.push('settle'); return Response.json({ success: true, transaction: '0xtx' }); }
    order.push('execute');
    return new Response('probe output', { status: 200 });
  };
  try {
    const res = await worker.fetch(mcpReq({
      jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'probe', arguments: { limit: '1' } },
    }, { 'X-PAYMENT': PAYLOAD }), env());
    assert.equal(res.status, 200);
    assert.deepEqual(order, ['verify', 'execute', 'settle']);
    assert.ok(res.headers.get('PAYMENT-RESPONSE'));
  } finally {
    globalThis.fetch = origFetch;
  }
});

test('an invalid payment never reaches the backend', async () => {
  const order = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.endsWith('/verify')) { order.push('verify'); return Response.json({ isValid: false }); }
    order.push('other');
    return Response.json({});
  };
  try {
    const res = await worker.fetch(mcpReq({
      jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'probe', arguments: {} },
    }, { 'X-PAYMENT': PAYLOAD }), env());
    assert.equal(res.status, 402);
    assert.deepEqual(order, ['verify']);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test('ranking renders one row per endpoint/model', async () => {
  const e = env();
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  e.DATA.store.set('history', JSON.stringify({ rows: [
    { endpoint: 'a', model: 'gpt-4o-mini', ok: true, status: 200, latency_ms: 100, probed_at: now },
    { endpoint: 'b', model: 'gpt-4o-mini', ok: false, status: 500, latency_ms: null, probed_at: now },
  ] }));
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.endsWith('/verify')) return Response.json({ isValid: true });
    if (u.endsWith('/settle')) return Response.json({ success: true, transaction: '0xtx' });
    return new Response('nope', { status: 500 });
  };
  try {
    const res = await worker.fetch(mcpReq({
      jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'ranking', arguments: {} },
    }, { 'X-PAYMENT': PAYLOAD }), e);
    assert.equal(res.status, 200);
    const text = (await res.json()).result.content[0].text;
    assert.match(text, /\| a \| gpt-4o-mini \|/);
    assert.match(text, /\| b \| gpt-4o-mini \|/);
    assert.match(text, /failing/);
  } finally {
    globalThis.fetch = origFetch;
  }
});
