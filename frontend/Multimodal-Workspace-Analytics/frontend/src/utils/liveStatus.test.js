// Unit tests for the single-user live status pill. Run with `npm test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { describeLiveStatus } from "./liveStatus.js";


const NOW = new Date(2026, 9, 15, 14, 0, 0).getTime();

function errorWith(code, message = `${code} error`) {
  return Object.assign(new Error(message), { code });
}

function state(overrides = {}) {
  return { loading: false, error: null, dataAsOf: null, lastUpdated: null, status: null, ...overrides };
}


test("server reachable but no processed data (404) is not 'Offline'", () => {
  const noData = errorWith("no-data", "No processed data yet.");

  const failing = describeLiveStatus(state({
    error: noData,
    status: { collector_stale: true, pipeline: { last_error: "FileNotFoundError: Raw dataset not found" } },
  }), NOW);
  assert.equal(failing.label, "Pipeline error");
  assert.equal(failing.tone, "warn");
  assert.equal(failing.title, "FileNotFoundError: Raw dataset not found");

  const noCollector = describeLiveStatus(state({ error: noData, status: { collector_stale: true, pipeline: null } }), NOW);
  assert.equal(noCollector.label, "Waiting for collector");

  const waiting = describeLiveStatus(state({ error: noData, status: { collector_stale: false, pipeline: null } }), NOW);
  assert.equal(waiting.label, "Waiting for data");
  assert.equal(waiting.title, "No processed data yet.");
});


test("no answer at all is 'Offline', a silent server is 'Not responding'", () => {
  assert.equal(describeLiveStatus(state({ error: errorWith("unreachable") }), NOW).label, "Offline");
  assert.equal(describeLiveStatus(state({ error: errorWith("server") }), NOW).label, "Offline");

  const timeout = describeLiveStatus(state({ error: errorWith("timeout") }), NOW);
  assert.equal(timeout.label, "Not responding");
  assert.equal(timeout.tone, "error");
});


test("with data: a failed poll is 'Reconnecting', fresh data is 'Live'", () => {
  const dataAsOf = new Date(NOW - 8000);

  const reconnecting = describeLiveStatus(state({ dataAsOf, error: errorWith("timeout") }), NOW);
  assert.equal(reconnecting.label, "Reconnecting");

  const live = describeLiveStatus(state({ dataAsOf, status: { collector_stale: false, pipeline: null } }), NOW);
  assert.equal(live.label, "Live");
  assert.equal(live.tone, "live");

  const stopped = describeLiveStatus(state({ dataAsOf, status: { collector_stale: true, pipeline: null } }), NOW);
  assert.equal(stopped.label, "Collector stopped");
});


test("a history refreshed less often (large file) is not called delayed too early", () => {
  const dataAsOf = new Date(NOW - 90e3);
  const status = { collector_stale: false, pipeline: null };

  // Every 10 s: 90 s old data means processing is behind.
  assert.equal(describeLiveStatus(state({ dataAsOf, status }), NOW).label, "Processing delayed");
  assert.equal(describeLiveStatus(state({ dataAsOf, status, refreshSeconds: 10 }), NOW).label, "Processing delayed");

  // Downloaded every 60 s: up to 60 + 50 s is normal.
  assert.equal(describeLiveStatus(state({ dataAsOf, status, refreshSeconds: 60 }), NOW).label, "Live");
  assert.equal(
    describeLiveStatus(state({ dataAsOf: new Date(NOW - 120e3), status, refreshSeconds: 60 }), NOW).label,
    "Processing delayed"
  );

  // Without /api/status the same allowance applies to "Collector stopped".
  assert.equal(describeLiveStatus(state({ dataAsOf, refreshSeconds: 60 }), NOW).label, "Live");
  assert.equal(describeLiveStatus(state({ dataAsOf }), NOW).label, "Collector stopped");
});
