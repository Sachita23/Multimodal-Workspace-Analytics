// Unit tests for the Team pages' data module (requests, polling, helpers).
// Run with `node --test src/data/team.test.js` (or `npm test` once it is
// listed in package.json).

import assert from "node:assert/strict";
import { afterEach, beforeEach, mock, test } from "node:test";

import {
  ADMIN_KEY_STORAGE,
  createPoller,
  describeTeamRange,
  formatExpiry,
  formatRelative,
  getAdminKey,
  isInviteExpired,
  isPendingInvite,
  keyWasAccepted,
  loadTeamLive,
  loadTeamOverview,
  mergeRows,
  normalizeTeamRange,
  revokePendingInvite,
  setAdminKey,
  subscribeAdminKey,
  teamRangeSearch,
  trimRows,
} from "./team.js";


// --------------------------------------------------
// Browser stand-ins
// --------------------------------------------------

function fakeStorage() {
  const values = new Map();

  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    clear: () => values.clear(),
  };
}

function fakeDocument() {
  const doc = new EventTarget();
  doc.hidden = false;
  return doc;
}

const realFetch = globalThis.fetch;
const KEY = "admin-key-0123456789abcdef";

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", {
    value: fakeStorage(),
    configurable: true,
    writable: true,
  });
  globalThis.document = fakeDocument();
  setAdminKey(KEY);
});

afterEach(() => {
  globalThis.fetch = realFetch;
  delete globalThis.document;
  mock.timers.reset();
});


function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/**
 * fetch stand-in: `routes` maps "METHOD /path" to a function returning a
 * Response (or throwing, like a network failure). Every call is recorded.
 */
function fakeApi(routes) {
  const calls = [];

  globalThis.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    const path = String(url).replace(/^\/api\/team/, "").replace(/\?.*$/, "");
    const call = { method, path, url: String(url), headers: init.headers || {} };
    calls.push(call);

    const handler = routes[`${method} ${path}`];

    if (!handler) {
      return json({ error: "No such route in the test." }, 500);
    }

    return handler(call);
  };

  return calls;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));


// --------------------------------------------------
// Formatting and ranges
// --------------------------------------------------

test("formatRelative counts seconds under a minute and handles empty or bad values", () => {
  const now = Date.now();

  assert.equal(formatRelative(null), "Never");
  assert.equal(formatRelative(""), "Never");
  assert.equal(formatRelative("not a date"), "--");
  assert.equal(formatRelative(now), "Just now");
  assert.equal(formatRelative(now + 60_000), "Just now"); // clock skew: never "in the future"
  assert.equal(formatRelative(now - 5_000), "5 s ago");
  assert.equal(formatRelative(now - 59_000), "59 s ago");
  assert.equal(formatRelative(now - 90_000), "1 min ago");
  assert.equal(formatRelative(new Date(now - 2 * 3600_000).toISOString()), "2 h ago");
  assert.equal(formatRelative(new Date(now - 3 * 86400_000)), "3 d ago");
});


test("formatExpiry says how long is left or how long ago it expired", () => {
  const now = Date.now();

  assert.equal(formatExpiry(null), "--");
  assert.equal(formatExpiry("nope"), "--");
  assert.equal(formatExpiry(new Date(now + 6.5 * 86400_000).toISOString()), "Expires in 6 d");
  assert.equal(formatExpiry(new Date(now + 30_500).toISOString()), "Expires in 30 s");
  assert.equal(formatExpiry(new Date(now - 1.5 * 86400_000).toISOString()), "Expired 1 d ago");
});


test("isInviteExpired trusts the server and catches invites that ran out since", () => {
  const past = new Date(Date.now() - 1000).toISOString();
  const future = new Date(Date.now() + 3600_000).toISOString();

  assert.equal(isInviteExpired({ status: "expired" }), true);
  assert.equal(isInviteExpired({ status: "invited", invite_expires: past }), true);
  assert.equal(isInviteExpired({ status: "invited", invite_expires: future }), false);
  assert.equal(isInviteExpired({ status: "invited", invite_expires: null }), false);
  assert.equal(isInviteExpired({ status: "joined", invite_expires: past }), false);

  assert.equal(isPendingInvite("invited"), true);
  assert.equal(isPendingInvite("expired"), true);
  assert.equal(isPendingInvite("online"), false);
  assert.equal(isPendingInvite(null), false);
});


test("team ranges normalise unknown values and only add ?days= when not the default", () => {
  assert.equal(normalizeTeamRange("7"), "7");
  assert.equal(normalizeTeamRange("all"), "all");
  assert.equal(normalizeTeamRange("5"), "30");
  assert.equal(normalizeTeamRange(null), "30");

  assert.equal(teamRangeSearch("30"), "");
  assert.equal(teamRangeSearch("7"), "?days=7");
  assert.equal(teamRangeSearch("all"), "?days=all");

  assert.equal(describeTeamRange("1"), "in the last 24 hours");
  assert.equal(describeTeamRange("all"), "overall");
  assert.equal(describeTeamRange("7"), "in the last 7 days");
});


// --------------------------------------------------
// Activity log rows
// --------------------------------------------------

const row = (second) => ({ timestamp: `2026-10-15 10:00:${String(second).padStart(2, "0")}` });

test("mergeRows keeps newest-first order and the first copy of each timestamp", () => {
  const live = [row(9), row(8)];
  const shown = [row(8), row(7), row(6)];

  assert.deepEqual(
    mergeRows(live, shown).map((r) => r.timestamp.slice(-2)),
    ["09", "08", "07", "06"]
  );
  assert.equal(mergeRows(live, shown)[1], live[1]);
  assert.deepEqual(mergeRows([], []), []);
});


test("trimRows drops only the oldest rows so the list stays gap-free", () => {
  const rows = [row(9), row(8), row(7), row(6)];

  assert.equal(trimRows(rows, 10), rows);
  assert.deepEqual(trimRows(rows, 2), [row(9), row(8)]);
});


// --------------------------------------------------
// Requests and the admin key
// --------------------------------------------------

test("no request is sent without a key, and that is not reported as a rejected key", async () => {
  const calls = fakeApi({ "GET /overview": () => json({}) });
  setAdminKey("");

  await assert.rejects(loadTeamOverview(), (err) => {
    assert.equal(err.status, 401);
    assert.equal(err.missingKey, true);
    return true;
  });

  assert.equal(calls.length, 0);
});


test("a rejected key is forgotten, unless another key was saved meanwhile", async () => {
  fakeApi({ "GET /overview": () => json({ error: "Admin key required." }, 401) });

  await assert.rejects(loadTeamOverview(), (err) => {
    assert.equal(err.status, 401);
    assert.equal(err.missingKey, false);
    return true;
  });
  assert.equal(getAdminKey(), "");

  setAdminKey(KEY);
  fakeApi({
    "GET /overview": () => {
      setAdminKey("a-newer-key-0123456789");
      return json({ error: "Admin key required." }, 401);
    },
  });

  await assert.rejects(loadTeamOverview());
  assert.equal(getAdminKey(), "a-newer-key-0123456789");
});


test("keyWasAccepted: only the API's own non-401 4xx errors prove the key works", async () => {
  async function failure(response) {
    fakeApi({ "GET /overview": () => response });

    try {
      await loadTeamOverview();
    } catch (err) {
      return err;
    }

    throw new Error("expected a failure");
  }

  assert.equal(keyWasAccepted(await failure(json({ error: "No such member." }, 404))), true);
  assert.equal(keyWasAccepted(await failure(json({ error: "days must be 1, 7, 30 or all." }, 400))), true);
  assert.equal(keyWasAccepted(await failure(new Response("<h1>Not Found</h1>", { status: 404 }))), false);
  assert.equal(keyWasAccepted(await failure(new Response("", { status: 502 }))), false);
  setAdminKey(KEY);
  assert.equal(keyWasAccepted(await failure(json({ error: "Admin key required." }, 401))), false);

  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  setAdminKey(KEY);
  const network = await loadTeamOverview().catch((err) => err);
  assert.equal(network.status, 0);
  assert.equal(keyWasAccepted(network), false);
  assert.equal(keyWasAccepted(null), false);
});


test("loadTeamLive sends the ETag and keeps it on a 304", async () => {
  const calls = fakeApi({
    "GET /live": (call) => (
      call.headers["If-None-Match"] === '"v1"'
        ? new Response(null, { status: 304 })
        : json({ members: [{ id: 1, status: "online" }] }, 200, { ETag: '"v1"' })
    ),
  });

  const first = await loadTeamLive();
  assert.equal(first.notModified, false);
  assert.equal(first.etag, '"v1"');
  assert.equal(first.data.members[0].status, "online");
  assert.equal(calls[0].headers["If-None-Match"], undefined);

  const second = await loadTeamLive({ etag: first.etag });
  assert.deepEqual(second, { notModified: true, etag: '"v1"', data: null });
  assert.equal(calls[1].headers["If-None-Match"], '"v1"');
});


// --------------------------------------------------
// Revoking an invite never deletes a member who joined
// --------------------------------------------------

test("revoke: a member who joined since the page loaded is not deleted", async () => {
  const calls = fakeApi({
    "POST /members/7/invite": () => json({ error: "This member has already joined." }, 409),
    "DELETE /members/7": () => json({ deleted: true }),
  });

  await assert.rejects(revokePendingInvite(7), (err) => {
    assert.equal(err.status, 409);
    assert.equal(err.alreadyJoined, true);
    assert.match(err.message, /already joined/);
    return true;
  });

  assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["POST /members/7/invite"]);
});


test("revoke: the code is rotated before the delete, so nobody can join in between", async () => {
  const calls = fakeApi({
    "POST /members/7/invite": () => json({ member_id: 7, invite_code: "NEW" }, 201),
    "DELETE /members/7": () => json({ deleted: true }),
  });

  assert.deepEqual(await revokePendingInvite(7), { deleted: true });
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`),
    ["POST /members/7/invite", "DELETE /members/7"]
  );
});


test("revoke: other failures of the check stop before the delete", async () => {
  for (const [status, body] of [[404, { error: "No such member." }], [401, { error: "Admin key required." }]]) {
    setAdminKey(KEY);
    const calls = fakeApi({
      "POST /members/7/invite": () => json(body, status),
      "DELETE /members/7": () => json({ deleted: true }),
    });

    await assert.rejects(revokePendingInvite(7), (err) => {
      assert.equal(err.status, status);
      assert.equal(err.alreadyJoined, undefined);
      return true;
    });
    assert.equal(calls.length, 1, `status ${status} must not be followed by DELETE`);
  }
});


test("revoke: a failed delete after the rotation says the old code is dead", async () => {
  fakeApi({
    "POST /members/7/invite": () => json({ member_id: 7, invite_code: "NEW" }, 201),
    "DELETE /members/7": () => { throw new TypeError("Failed to fetch"); },
  });

  await assert.rejects(revokePendingInvite(7), (err) => {
    assert.equal(err.inviteRotated, true);
    assert.equal(err.status, 0);
    return true;
  });
});


// --------------------------------------------------
// Admin key changes in other tabs
// --------------------------------------------------

test("subscribeAdminKey reports changes to the admin key only", () => {
  const target = new EventTarget();
  const seen = [];
  const storageEvent = (key) => Object.assign(new Event("storage"), { key });

  const unsubscribe = subscribeAdminKey((key) => seen.push(key), target);

  setAdminKey("");
  target.dispatchEvent(storageEvent(ADMIN_KEY_STORAGE));
  target.dispatchEvent(storageEvent("workspace-preferences"));
  setAdminKey(KEY);
  target.dispatchEvent(storageEvent(null)); // storage.clear() elsewhere

  assert.deepEqual(seen, ["", KEY]);

  unsubscribe();
  target.dispatchEvent(storageEvent(ADMIN_KEY_STORAGE));
  assert.equal(seen.length, 2);

  // No window (e.g. tests, SSR): a harmless no-op.
  assert.equal(typeof subscribeAdminKey(() => {}, null), "function");
});


// --------------------------------------------------
// Polling
// --------------------------------------------------

/** A task whose runs finish only when the test says so. */
function scriptedTask() {
  const runs = [];

  const task = ({ signal, isCurrent }) => new Promise((resolve, reject) => {
    const entry = { signal, isCurrent, resolve };
    runs.push(entry);
    signal.addEventListener("abort", () => {
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    });
  });

  return { task, runs };
}

function useFakeClock() {
  mock.timers.enable({ apis: ["setInterval", "Date"], now: 1_000_000 });
}


test("poller: runs at once, then every interval, skipping ticks while one is in flight", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "k");
  const stop = poller.start({ intervalMs: 5000, key: "k" });

  assert.equal(runs.length, 1);

  mock.timers.tick(5000);
  mock.timers.tick(5000);
  assert.equal(runs.length, 1, "no second request while the first is in flight");

  runs[0].resolve();
  await flush();
  mock.timers.tick(5000);
  assert.equal(runs.length, 2);

  stop();
});


test("poller: run(true) aborts the request in flight and marks it superseded", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "k");

  const first = poller.run();
  assert.equal(poller.run(), first, "run() joins the run in flight");

  poller.run(true);
  assert.equal(runs.length, 2);
  assert.equal(runs[0].signal.aborted, true);
  assert.equal(runs[0].isCurrent(), false);
  assert.equal(runs[1].isCurrent(), true);

  runs[1].resolve();
  await flush();
  await first; // the aborted run settles quietly
});


test("poller: stop() aborts and invalidates the run in flight", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "k");
  const stop = poller.start({ intervalMs: 5000, key: "k" });

  stop();
  assert.equal(runs[0].signal.aborted, true);
  assert.equal(runs[0].isCurrent(), false);

  mock.timers.tick(20_000);
  assert.equal(runs.length, 1, "no timer after stop()");
  await flush();
});


test("poller: paused sets no timer but still loads a key it has not loaded", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "7");

  let stop = poller.start({ intervalMs: 5000, paused: true, key: "7" });
  assert.equal(runs.length, 1, "first load happens even when paused");
  runs[0].resolve();
  await flush();

  mock.timers.tick(30_000);
  assert.equal(runs.length, 1, "paused: no polling");

  // Re-render with the same key while paused: nothing new to load.
  stop();
  stop = poller.start({ intervalMs: 5000, paused: true, key: "7" });
  assert.equal(runs.length, 1);

  // Range change while paused loads once.
  stop();
  poller.update(task, "30");
  stop = poller.start({ intervalMs: 5000, paused: true, key: "30" });
  assert.equal(runs.length, 2);

  stop();
  await flush();
});


test("poller: a forced run works while disabled, and a paused start does not repeat it", async () => {
  // Team.jsx relies on this: after Lock and Unlock while paused, unlock()
  // force-runs the live view itself, because re-enabling a paused poller
  // whose key already loaded does not fetch again.
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "live");

  let stop = poller.start({ intervalMs: 5000, paused: true, key: "live" });
  runs[0].resolve();
  await flush();
  stop(); // Lock: polling disabled

  stop = poller.start({ intervalMs: 5000, paused: true, key: "live" });
  assert.equal(runs.length, 1, "re-enabling while paused does not reload by itself");
  stop();

  poller.run(true); // what unlock() does
  assert.equal(runs.length, 2);
  runs[1].resolve();
  await flush();

  stop = poller.start({ intervalMs: 5000, paused: true, key: "live" });
  assert.equal(runs.length, 2, "no duplicate request after the forced run");
  stop();
});


test("poller: skips the immediate run when the same key finished under 1 s ago", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "k");

  poller.run(true); // e.g. the request that verified a new key
  runs[0].resolve();
  await flush();

  let stop = poller.start({ intervalMs: 5000, key: "k" });
  assert.equal(runs.length, 1, "just loaded: no immediate repeat");
  mock.timers.tick(5000);
  assert.equal(runs.length, 2, "the timer still runs");
  runs[1].resolve();
  await flush();
  stop();

  mock.timers.tick(1500);
  stop = poller.start({ intervalMs: 5000, key: "k" });
  assert.equal(runs.length, 3, "older than 1 s: runs at once");
  stop();
  await flush();
});


test("poller: pauses while the tab is hidden and runs as soon as it is visible", async () => {
  useFakeClock();
  const { task, runs } = scriptedTask();
  const poller = createPoller(task, "k");
  const stop = poller.start({ intervalMs: 5000, key: "k" });

  runs[0].resolve();
  await flush();

  document.hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  mock.timers.tick(60_000);
  assert.equal(runs.length, 1, "nothing while hidden");

  document.hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  assert.equal(runs.length, 2, "runs straight away when visible");

  stop();
  await flush();
});


test("poller: update() swaps the task and key used by the next run", async () => {
  useFakeClock();
  const seen = [];
  const poller = createPoller(async () => seen.push("old"), "a");

  poller.update(async () => seen.push("new"), "b");
  await poller.run();

  assert.deepEqual(seen, ["new"]);
  assert.equal(poller.state.lastDoneKey, "b");
});
