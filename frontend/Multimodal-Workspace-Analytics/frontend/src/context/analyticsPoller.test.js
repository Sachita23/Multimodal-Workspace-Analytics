// Unit tests for the polling machinery behind AnalyticsProvider.
// Run with `npm test`.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  BACKGROUND_REFRESH_SECONDS,
  MAX_REFRESH_SECONDS,
  createAnalyticsPoller,
  followRange,
  rowsRefreshMs,
  startPolling,
} from "./analyticsPoller.js";
import { REFRESH_SECONDS } from "../data/analytics.js";


const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});


const CSV = [
  "timestamp,application,window_title,activity_status,session_id,active_duration_seconds,idle_duration_seconds,duration_seconds",
  "2026-10-15 13:00:00,Code,x,Active,0,5,0,5",
  "",
].join("\n");

const STATUS = { status: "ok", collector_stale: false, pipeline: null };


/**
 * A scripted /api server. `mode.analytics` is "ok" | "hang" | "404";
 * `mode.status` is "ok" | "hang". Hung requests reject only when aborted,
 * like a real fetch whose server never answers.
 */
function fakeServer() {
  const mode = { analytics: "ok", status: "ok" };
  const calls = [];

  const hang = (init) => new Promise((_, reject) => {
    init.signal?.addEventListener("abort", () => {
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    });
  });

  globalThis.fetch = async (url, init = {}) => {
    const ifNoneMatch = init.headers?.["If-None-Match"] ?? null;
    calls.push({ url, ifNoneMatch });

    if (url.includes("status")) {
      if (mode.status === "hang") return hang(init);
      return new Response(JSON.stringify(STATUS), { status: 200 });
    }

    if (mode.analytics === "hang") return hang(init);
    if (mode.analytics === "404") return new Response("{}", { status: 404 });
    if (ifNoneMatch === '"v1"') return new Response(null, { status: 304 });

    return new Response(CSV, { status: 200, headers: { ETag: '"v1"' } });
  };

  return { mode, calls, analyticsCalls: () => calls.filter((call) => call.url.includes("analytics")) };
}


function recorder() {
  const events = [];

  return {
    events,
    // Everything except status, which may arrive before or after the CSV.
    csvEvents: () => events.filter(([name]) => name !== "status"),
    callbacks: {
      onStatus: (status) => events.push(["status", status]),
      onRows: (rows) => events.push(["rows", rows.length]),
      onSuccess: () => events.push(["success"]),
      onNoData: () => events.push(["no-data"]),
      onError: (error) => events.push(["error", error.code]),
    },
  };
}


test("200 then 304: rows are delivered once, the etag is sent back", async () => {
  const server = fakeServer();
  const { events, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 500 });

  await poller.poll();
  await poller.poll();

  assert.deepEqual(server.analyticsCalls().map((call) => call.ifNoneMatch), [null, '"v1"']);
  assert.equal(events.filter(([name]) => name === "rows").length, 1);
  assert.equal(events.filter(([name]) => name === "success").length, 2);
});


test("a poll that is already running is shared, not duplicated", async () => {
  const server = fakeServer();
  const poller = createAnalyticsPoller({ timeoutMs: 500 });

  const first = poller.poll();
  const second = poller.poll();

  assert.equal(first, second);
  await first;
  assert.equal(server.analyticsCalls().length, 1);
});


test("a hung request times out, reports an error, and the next poll asks again", async () => {
  const server = fakeServer();
  const { csvEvents, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 60 });

  await poller.poll();
  server.mode.analytics = "hang";

  // refresh() while the hung poll is in flight gets the same promise, and it
  // settles (with the timeout) instead of spinning forever.
  const hung = poller.poll();
  const refresh = poller.poll();
  assert.equal(hung, refresh);

  await assert.rejects(refresh, (error) => error.code === "timeout");
  assert.deepEqual(csvEvents().at(-1), ["error", "timeout"]);

  // Polling continues: the next cycle sends a new request.
  server.mode.analytics = "ok";
  await poller.poll();

  assert.equal(server.analyticsCalls().length, 3);
  assert.deepEqual(csvEvents().at(-1), ["success"]);
});


test("/api/status is reported even while the CSV request hangs", async () => {
  const server = fakeServer();
  const { events, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 200 });

  server.mode.analytics = "hang";
  const pending = poller.poll().catch(() => {});

  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(events, [["status", STATUS]]);

  await pending;
});


test("a status request that hangs reports null rather than the last status", async () => {
  const server = fakeServer();
  const { events, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 60 });

  await poller.poll();
  server.mode.status = "hang";
  await poller.poll();

  const statuses = events.filter(([name]) => name === "status").map(([, value]) => value);
  assert.deepEqual(statuses, [STATUS, null]);
});


test("a 404 drops the snapshot and forgets the etag", async () => {
  const server = fakeServer();
  const { csvEvents, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 500 });

  await poller.poll();
  server.mode.analytics = "404";
  await assert.rejects(poller.poll(), (error) => error.code === "no-data");

  assert.deepEqual(csvEvents().slice(-2), [["no-data"], ["error", "no-data"]]);

  // The same file coming back must be downloaded again, not answered 304.
  server.mode.analytics = "ok";
  await poller.poll();

  assert.equal(server.analyticsCalls().at(-1).ifNoneMatch, null);
  assert.deepEqual(csvEvents().slice(-2), [["rows", 1], ["success"]]);
});


test("abortAll cancels quietly and polling works again afterwards", async () => {
  const server = fakeServer();
  const { events, callbacks } = recorder();
  const poller = createAnalyticsPoller({ ...callbacks, timeoutMs: 5000 });

  server.mode.analytics = "hang";
  server.mode.status = "hang";
  const pending = poller.poll();
  poller.abortAll();

  await pending;
  assert.deepEqual(events, []);

  server.mode.analytics = "ok";
  server.mode.status = "ok";
  await poller.poll();
  assert.deepEqual(events.filter(([name]) => name !== "status").at(-1), ["success"]);
});


// --------------------------------------------------
// Cadence: how often the CSV is downloaded
// --------------------------------------------------

/**
 * A poller on a manual clock. Each new CSV snapshot "costs" `cost.ms` of
 * that clock (download + parse); 304s cost nothing.
 */
function clockedPoller({ cost = { ms: 0 }, changes = { always: true } } = {}) {
  const clock = { now: 0 };
  const requests = [];
  let version = 0;

  const poller = createAnalyticsPoller({
    clock: () => clock.now,
    fetchRows: async ({ etag }) => {
      requests.push("csv");

      if (!changes.always && etag === `v${version}`) return { notModified: true, etag };

      version += 1;
      clock.now += cost.ms;
      return { notModified: false, etag: `v${version}`, rows: [] };
    },
    loadStatus: async () => {
      requests.push("status");
      return STATUS;
    },
  });

  return { poller, clock, requests, take: () => requests.splice(0) };
}


test("rowsRefreshMs: every 10 s while a snapshot is cheap, less often as it grows", () => {
  assert.equal(rowsRefreshMs(0), REFRESH_SECONDS * 1000);
  assert.equal(rowsRefreshMs(400), REFRESH_SECONDS * 1000);   // a week of data
  assert.equal(rowsRefreshMs(2000), 20_000);                  // a month
  assert.equal(rowsRefreshMs(30_000), MAX_REFRESH_SECONDS * 1000);
  assert.equal(rowsRefreshMs(0, { background: true }), BACKGROUND_REFRESH_SECONDS * 1000);
});


test("tick: /api/status every time, the CSV only once it is due", async () => {
  const { poller, clock, take } = clockedPoller({ cost: { ms: 3000 } });

  await poller.poll();                    // costs 3 s -> CSV every 30 s
  assert.deepEqual(take().sort(), ["csv", "status"]);
  assert.equal(poller.refreshMs(), 30_000);

  clock.now = 13_000;
  await poller.tick();
  assert.deepEqual(take(), ["status"]);

  clock.now = 23_000;
  await poller.tick();
  assert.deepEqual(take(), ["status"]);

  // Timers fire a moment late; a cycle that started 30 s ago is due even if
  // this tick runs a few ms "early" relative to that start.
  clock.now = 29_995;
  await poller.tick();
  assert.deepEqual(take().sort(), ["csv", "status"]);
});


test("tick: a cheap snapshot keeps the 10 s cadence", async () => {
  const { poller, clock, take } = clockedPoller({ cost: { ms: 300 } });

  await poller.poll();
  take();

  for (let tick = 1; tick <= 3; tick += 1) {
    clock.now = tick * REFRESH_SECONDS * 1000;
    await poller.tick();
    assert.deepEqual(take().sort(), ["csv", "status"]);
  }
});


test("tick on a background page (Team, Focus): no /api/status, the CSV every 60 s", async () => {
  const { poller, clock, take } = clockedPoller();

  await poller.poll();
  take();

  clock.now = 10_000;
  await poller.tick({ background: true });
  assert.deepEqual(take(), []);

  clock.now = 50_000;
  await poller.tick({ background: true });
  assert.deepEqual(take(), []);

  clock.now = 60_000;
  await poller.tick({ background: true });
  assert.deepEqual(take().sort(), ["csv", "status"]);

  // Back on a single-user page the normal cadence applies at once.
  clock.now = 70_000;
  await poller.tick();
  assert.deepEqual(take().sort(), ["csv", "status"]);
});


test("refresh during a status-only cycle still downloads the CSV", async () => {
  let releaseStatus;
  const requests = [];
  const clock = { now: 0 };

  const poller = createAnalyticsPoller({
    clock: () => clock.now,
    fetchRows: async () => {
      requests.push("csv");
      clock.now += 5000;
      return { notModified: false, etag: "v", rows: [] };
    },
    loadStatus: () => {
      requests.push("status");
      return new Promise((resolve) => {
        releaseStatus = () => resolve(STATUS);
      });
    },
  });

  const first = poller.poll();
  releaseStatus();
  await first;

  clock.now = 12_000;
  const statusOnly = poller.tick();
  const refresh = poller.poll();
  assert.notEqual(refresh, statusOnly);

  releaseStatus();
  await statusOnly;
  await new Promise((resolve) => setTimeout(resolve, 0));
  releaseStatus();
  await refresh;

  assert.deepEqual(requests, ["status", "csv", "status", "status", "csv"]);
});


test("followRange: follows the data until the user picks a range", () => {
  const now = new Date(2026, 9, 15, 14, 0, 0).getTime();
  const recent = [{ ts: now - 3600e3 }];
  const old = [{ ts: now - 5 * 24 * 3600e3 }];

  const start = { value: "24h", picked: false };
  assert.equal(followRange(start, recent, now), start);
  assert.deepEqual(followRange(start, old, now), { value: "all", picked: false });
  assert.deepEqual(followRange({ value: "all", picked: false }, recent, now), { value: "24h", picked: false });

  const picked = { value: "7d", picked: true };
  assert.equal(followRange(picked, old, now), picked);
  assert.equal(followRange(picked, recent, now), picked);
});


// --------------------------------------------------
// startPolling: timer, visibility and cleanup
// --------------------------------------------------

function fakeDocument() {
  const listeners = new Set();

  return {
    hidden: false,
    listeners,
    addEventListener: (type, listener) => {
      if (type === "visibilitychange") listeners.add(listener);
    },
    removeEventListener: (type, listener) => {
      if (type === "visibilitychange") listeners.delete(listener);
    },
    setHidden(hidden) {
      this.hidden = hidden;
      listeners.forEach((listener) => listener());
    },
  };
}


function fakePoller() {
  const calls = [];

  return {
    calls,
    poll: () => {
      calls.push("poll");
      return Promise.resolve();
    },
    tick: ({ background }) => {
      calls.push(background ? "tick:background" : "tick");
      return Promise.reject(new Error("offline"));   // errors stay quiet
    },
    abortAll: () => calls.push("abortAll"),
  };
}


test("startPolling: polls now, ticks every 10 s while visible, pauses while hidden", () => {
  const doc = fakeDocument();
  const poller = fakePoller();
  const timers = [];
  let background = false;

  const stop = startPolling(poller, {
    doc,
    isBackground: () => background,
    setTimer: (callback, ms) => {
      timers.push({ callback, ms, cleared: false });
      return timers.length - 1;
    },
    clearTimer: (id) => {
      timers[id].cleared = true;
    },
  });

  assert.deepEqual(poller.calls, ["poll"]);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, REFRESH_SECONDS * 1000);

  timers[0].callback();
  background = true;
  timers[0].callback();
  assert.deepEqual(poller.calls.splice(1), ["tick", "tick:background"]);

  // Hidden: the timer keeps firing but nothing is requested.
  doc.setHidden(true);
  timers[0].callback();
  timers[0].callback();
  assert.deepEqual(poller.calls.splice(1), []);

  // Shown again: catch up immediately.
  background = false;
  doc.setHidden(false);
  assert.deepEqual(poller.calls.splice(1), ["tick"]);

  // Unmount: timer and listener gone, requests aborted.
  stop();
  assert.equal(timers[0].cleared, true);
  assert.equal(doc.listeners.size, 0);
  assert.deepEqual(poller.calls.splice(1), ["abortAll"]);
});


test("startPolling twice (React StrictMode) leaves one timer and one listener", () => {
  const doc = fakeDocument();
  const poller = fakePoller();
  const live = new Set();
  let nextId = 0;

  const options = {
    doc,
    setTimer: () => {
      nextId += 1;
      live.add(nextId);
      return nextId;
    },
    clearTimer: (id) => live.delete(id),
  };

  const stopFirst = startPolling(poller, options);
  stopFirst();
  const stopSecond = startPolling(poller, options);

  assert.equal(live.size, 1);
  assert.equal(doc.listeners.size, 1);
  assert.deepEqual(poller.calls, ["poll", "abortAll", "poll"]);

  stopSecond();
  assert.equal(live.size, 0);
  assert.equal(doc.listeners.size, 0);
});
