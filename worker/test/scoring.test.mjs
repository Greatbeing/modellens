// Worker scoring must match modellens.py score_model. These tests pin the
// invariants the old hand-rolled JS got wrong (mean vs median, cancellation
// detection, score-0-on-failure, dead/quota_blocked verdicts).
// Run: node --test test/   (from worker/, zero dependencies)
import test from 'node:test';
import assert from 'node:assert/strict';
import { median, scoreModel, aggregate, round1 } from '../src/index.js';

const row = (ok, status = 200, lat = null) => ({ model: 'm', endpoint: 'e', ok, status, latency_ms: lat });

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

test('score formula matches Python (85.0)', () => {
  const s = scoreModel(Array.from({ length: 20 }, () => row(true, 200, 4000)));
  assert.equal(s.score, 85);
});

test('aggregate sorts best-first and groups by model', () => {
  const rows = [row(true, 200, 5000), { ...row(true, 200, 500), model: 'fast' }];
  const agg = aggregate(rows);
  assert.equal(agg[0].model, 'fast');
  assert.ok(agg[0].score > agg[1].score);
});

test('round1 ties-to-even matches Python round(x,1)', () => {
  assert.equal(round1(81.25), 81.2); // toFixed would give 81.3
  assert.equal(round1(99.25), 99.2);
  assert.equal(round1(81.35), 81.4); // non-tie: nearest
  assert.equal(round1(33.333333), 33.3);
});

test('score at an exact tie rounds like Python (81.25 -> 81.2)', () => {
  const s = scoreModel(Array.from({ length: 10 }, () => row(true, 200, 5000)));
  assert.equal(s.score, 81.2);
});