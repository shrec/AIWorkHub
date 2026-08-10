'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { DashboardEventConsumer, MIN_POLL_INTERVAL_MS } = require('../runtime-events');

function collectConsumer(overrides) {
  const calls = [];
  const consumer = new DashboardEventConsumer({
    onEvents: (events, meta) => calls.push({ type: 'events', events, meta }),
    onResync: () => calls.push({ type: 'resync' }),
    pollIntervalMs: 0,
    ...(overrides || {})
  });
  return { consumer, calls };
}

test('coalesces bursts into one callback', () => {
  const { consumer, calls } = collectConsumer();
  consumer.handleBatch({ events: [{ seq: 1, kind: 'a' }] });
  consumer.handleBatch({ events: [{ seq: 2, kind: 'b' }] });
  assert.strictEqual(calls.length, 0);
  consumer.flush();
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].type, 'events');
  assert.deepStrictEqual(calls[0].events.map((event) => event.seq), [1, 2]);
  assert.strictEqual(consumer.lastSeq, 2);
});

test('detects gaps and requests authoritative resync', () => {
  const { consumer, calls } = collectConsumer();
  consumer.handleBatch({ events: [{ seq: 1, kind: 'a' }] });
  consumer.flush();
  assert.strictEqual(calls.length, 1);
  consumer.handleBatch({ events: [{ seq: 3, kind: 'b' }] });
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].type, 'resync');
  assert.strictEqual(consumer.lastSeq, 0);
});

test('overflow flag forces resync and clears stale state', () => {
  const { consumer, calls } = collectConsumer();
  consumer.handleBatch({ events: [{ seq: 1, kind: 'a' }] });
  consumer.flush();
  consumer.handleBatch({ events: [{ seq: 2, kind: 'b' }], resync_required: true });
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].type, 'resync');
  assert.strictEqual(consumer.lastSeq, 0);
});

test('reconnect and runtime replacement force resync', () => {
  const { consumer, calls } = collectConsumer();
  consumer.resetAfterReconnect();
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].type, 'resync');
  consumer.replaceRuntime();
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].type, 'resync');
});

test('slow bounded polling fallback is enforced', () => {
  let intervalMs = 0;
  const { consumer } = collectConsumer({
    pollIntervalMs: 1,
    setInterval: (fn, ms) => { intervalMs = ms; return { fn, ms }; },
    clearInterval: () => {}
  });
  consumer.start();
  assert.ok(intervalMs >= MIN_POLL_INTERVAL_MS);
});
