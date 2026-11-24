// Worker scoring must match modellens.py score_model. These tests pin the
// invariants the old hand-rolled JS got wrong (mean vs median, cancellation
// detection, score-0-on-failure, dead/quota_blocked verdicts).
// Run: node --test test/   (from worker/, zero dependencies)
import test from 'node:test';
import assert from 'node:assert/strict';
import { median, scoreModel, aggregate, round1, formatAge, parseLimit, wilsonLower } from '../src/index.js';

const row = (ok, status = 200, lat = null, probed_at) => ({ model: 'm', endpoint: 'e', ok, status, latency_ms: lat, ...(probed_at ? { probed_at } : {}) });
const NOW = Date.parse('2026-11-21T12:00:00Z');
const isoHoursAgo = (h) => new Date(NOW - h * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');

test('median, not mean', () => {
  assert.equal(median([100, 200, 10000]), 200); // mean would be 3433
  assert.equal(median([100, 200, 300, 400]), 250);
  assert.equal(median([]), null);
});

test('empty is untested, score 0', () => {
  const s = scoreModel([]);
  assert.equal(s.verdict, 'untested');
  assert.equal(s.score, 0);
});

test('all 404 is dead, score 0 (old bug gave ~20)', () => {
  const s = scoreModel(Array.from({ length: 5 }, () => row(false, 404)));
  assert.equal(s.verdict, 'dead');
  assert.equal(s.score, 0);
});

test('all 429 is quota_blocked, score 0', () => {
  const s = scoreModel(Array.from({ length: 5 }, () => row(false, 429)));
  assert.equal(s.verdict, 'quota_blocked');
  assert.equal(s.score, 0);
});

test('0% success scores 0 even with other failures', () => {
  const s = scoreModel([row(false, 500, 10)]);
  assert.equal(s.score, 0);
});

test('cancellation is status 0/499, not a string', () => {
  const rows = [...Array.from({ length: 7 }, () => row(true, 200, 100)),
                ...Array.from({ length: 3 }, () => row(false, 0))];
  const s = scoreModel(rows);
  assert.equal(s.cancel_rate, 30);
  assert.equal(s.verdict, 'unreliable');
});

test("error:'cancelled' string is NOT a cancellation (regression)", () => {
  const s = scoreModel([{ model: 'm', ok: false, status: 500, error: 'cancelled', latency_ms: null }]);
  assert.equal(s.cancel_rate, 0);
});

test('healthy threshold', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000)));
  assert.equal(s.verdict, 'healthy');
});

test('high latency is mixed, not healthy', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 5000)));
  assert.equal(s.verdict, 'mixed');
});

test('score formula matches Python (76.9: success on Wilson lower bound)', () => {
  const s = scoreModel(Array.from({ length: 20 }, () => row(true, 200, 4000)));
  assert.equal(s.score, 76.9);
});

test('aggregate sorts best-first and groups by endpoint+model', () => {
  const rows = [row(true, 200, 5000), { ...row(true, 200, 500), model: 'fast' }];
  const agg = aggregate(rows);
  assert.equal(agg[0].model, 'fast');
  assert.equal(agg[0].endpoint, 'e');
  assert.ok(agg[0].score > agg[1].score);
});

test('the same model on two endpoints is two rows (not merged)', () => {
  // Merging them hides that one gateway is broken: both would look "mixed".
  const rows = [
    ...Array.from({ length: 5 }, () => ({ ...row(true, 200, 100), endpoint: 'good' })),
    ...Array.from({ length: 5 }, () => ({ ...row(false, 500), endpoint: 'bad' })),
  ];
  const agg = aggregate(rows);
  assert.equal(agg.length, 2);
  const good = agg.find((r) => r.endpoint === 'good');
  const bad = agg.find((r) => r.endpoint === 'bad');
  assert.equal(good.verdict, 'healthy');
  assert.equal(bad.verdict, 'failing');
  assert.equal(bad.score, 0);
});

test('an endpoint literally named "default" is not merged with no endpoint', () => {
  // The old `${endpoint || 'default'}/${model}` key collapsed these two.
  const agg = aggregate([
    { ...row(true, 200, 100), endpoint: 'default' },
    { ...row(true, 200, 500), endpoint: undefined },
  ]);
  assert.equal(agg.length, 2);
  assert.deepEqual(agg.map((r) => r.attempts).sort(), [1, 1]);
});

test('junk rows do not take the whole ranking down', () => {
  // KV is written by an ingest endpoint; a null/hand-edited entry must not 500.
  const agg = aggregate([null, undefined, 42, 'x', {}, { model: '' },
                         { ...row(true, 200, 100), endpoint: 'a' }]);
  assert.equal(agg.length, 1);
  assert.equal(agg[0].model, 'm');
});

test('integer-looking model ids keep a stable order', () => {
  const agg = aggregate([{ ...row(true, 200, 100), model: '10' },
                         { ...row(true, 200, 100), model: '2' }]);
  assert.deepEqual(agg.map((r) => r.model).sort(), ['10', '2']); // not object-key order
});

test('auth errors get their own verdict', () => {
  const s = scoreModel([row(false, 401), row(false, 403)]);
  assert.equal(s.verdict, 'auth_error');
  assert.equal(s.score, 0);
});

test('5xx failures are "failing", not "mixed"', () => {
  const s = scoreModel([row(false, 500), row(false, 502), row(false, 503)]);
  assert.equal(s.verdict, 'failing');
  assert.equal(s.score, 0);
});

test('zero latency is healthy, missing latency is mixed', () => {
  assert.equal(scoreModel(Array.from({ length: 5 }, () => row(true, 200, 0))).verdict, 'healthy');
  const noLat = scoreModel(Array.from({ length: 5 }, () => row(true, 200, null)));
  assert.equal(noLat.verdict, 'mixed');
  assert.equal(noLat.median_latency_ms, null);
  assert.equal(noLat.score, 48.3); // Wilson(5/5) success + 0 latency + 20 cancel, no crash
});

test('parseLimit clamps NaN / negative / huge', () => {
  assert.equal(parseLimit(undefined, 20, 200), 20);
  assert.equal(parseLimit('abc', 20, 200), 20);
  assert.equal(parseLimit('-5', 20, 200), 1);
  assert.equal(parseLimit('9999', 20, 200), 200);
  assert.equal(parseLimit('7', 20, 200), 7);
});

test('formatAge keeps minute precision', () => {
  assert.equal(formatAge(null), '-');
  assert.equal(formatAge(17 / 60), '17m'); // 0.28h — not rounded to 18m first
  assert.equal(formatAge(3), '3h');
  assert.equal(formatAge(72), '3d');
});

test('round1 ties-to-even matches Python round(x,1)', () => {
  assert.equal(round1(81.25), 81.2); // toFixed would give 81.3
  assert.equal(round1(99.25), 99.2);
  assert.equal(round1(81.35), 81.4); // non-tie: nearest
  assert.equal(round1(33.333333), 33.3);
});

test('one lucky probe does not outrank a proven model', () => {
  const lucky = scoreModel([row(true, 200, 150)]);
  const proven = scoreModel(Array.from({ length: 40 }, () => row(true, 200, 400)));
  assert.ok(proven.score > lucky.score);
  assert.ok(Math.abs(wilsonLower(1, 1) - 0.2065) < 1e-3);
});

// ---- staleness (mirrors modellens.py) ----

test('fresh success stays healthy', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000, isoHoursAgo(0.5))), NOW);
  assert.equal(s.verdict, 'healthy');
  assert.equal(s.age_hours, 0.5);
});

test('old success becomes stale, score kept', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000, isoHoursAgo(7))), NOW);
  assert.equal(s.verdict, 'stale');
  assert.ok(s.score > 0);
  assert.equal(s.age_hours, 7);
});

test('stale boundary is 6h', () => {
  assert.equal(scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000, isoHoursAgo(5.9))), NOW).verdict, 'healthy');
  assert.equal(scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000, isoHoursAgo(6.1))), NOW).verdict, 'stale');
});

test('dead / quota_blocked / unreliable keep their verdict when old', () => {
  assert.equal(scoreModel(Array.from({ length: 5 }, () => row(false, 404, null, isoHoursAgo(48))), NOW).verdict, 'dead');
  assert.equal(scoreModel(Array.from({ length: 5 }, () => row(false, 429, null, isoHoursAgo(48))), NOW).verdict, 'quota_blocked');
  const rows = [...Array.from({ length: 7 }, () => row(true, 200, 100, isoHoursAgo(48))),
                ...Array.from({ length: 3 }, () => row(false, 0, null, isoHoursAgo(48)))];
  assert.equal(scoreModel(rows, NOW).verdict, 'unreliable');
});

test('missing probed_at is never stale', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 2000)), NOW);
  assert.equal(s.verdict, 'healthy');
  assert.equal(s.age_hours, null);
});