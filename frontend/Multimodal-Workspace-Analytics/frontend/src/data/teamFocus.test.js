// Unit tests for members' focus sessions on the Team pages.
// Run with `npm test`.

import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";

import {
  ADMIN_KEY_STORAGE,
  MEMBER_FOCUS_LIVE_SECONDS,
  describeMemberFocus,
  focusAllowedApps,
  loadMemberFocus,
  memberFocusIntervalMs,
  pickMemberFocus,
  runningMemberSession,
} from "./team.js";


const realFetch = globalThis.fetch;

beforeEach(() => {
  const values = new Map([[ADMIN_KEY_STORAGE, "admin-key-0123456789abcdef"]]);

  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (key) => (values.has(key) ? values.get(key) : null),
      setItem: (key, value) => values.set(key, String(value)),
      removeItem: (key) => values.delete(key),
    },
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  globalThis.fetch = realFetch;
});


test("loads /api/team/members/<id>/focus with the admin key", async () => {
  const calls = [];
  const body = { sessions: [], summary: { sessions: 0, focus_score: null, deviation_count: 0 } };

  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status: 200 });
  };

  assert.deepEqual(await loadMemberFocus(7), body);
  assert.equal(calls[0].url, "/api/team/members/7/focus");
  assert.equal(calls[0].init.headers["X-Admin-Key"], "admin-key-0123456789abcdef");
});


test("a 404 without a JSON error means the server has no focus endpoint", async () => {
  globalThis.fetch = async () => new Response("<html>Not Found</html>", { status: 404 });
  await assert.rejects(loadMemberFocus(7), (error) => error.status === 404 && error.fromApi === false);

  globalThis.fetch = async () => new Response(JSON.stringify({ error: "member not found" }), { status: 404 });
  await assert.rejects(loadMemberFocus(7), (error) => error.status === 404 && error.fromApi === true);
});


test("the live view's focus block wins over the overview's", () => {
  const overview = { score: 70, sessions: 3, live: null };
  const live = { score: 70, sessions: 3, live: { running: true, remaining_s: 60 } };

  assert.equal(pickMemberFocus(overview, live), live);
  assert.equal(pickMemberFocus(overview, undefined), overview);
  assert.equal(pickMemberFocus(undefined, undefined), null);
});


test("Focus column: live session, else overall score, else --", () => {
  const live = describeMemberFocus({
    score: 82.5,
    sessions: 4,
    live: { running: true, title: "Report", remaining_s: 754, current_app: "Safari", allowed: false, score: 85, updated_at: "x" },
  }, 4);

  assert.equal(live.kind, "live");
  assert.equal(live.text, "In focus · 12:30 left · score 85");
  assert.equal(live.onTask, false);
  assert.equal(live.currentApp, "Safari");

  assert.equal(describeMemberFocus({ live: { remaining_s: 3, score: null, allowed: null } }, 10).text, "In focus · 0:00 left");

  assert.deepEqual(describeMemberFocus({ score: 82.5, sessions: 4, live: null }), { kind: "score", score: 82.5, sessions: 4 });
  assert.deepEqual(describeMemberFocus({ score: null, sessions: 0, live: null }), { kind: "none" });
  assert.deepEqual(describeMemberFocus(undefined), { kind: "none" });
});


test("a running row is live only while it was received in the last 30 s", () => {
  const now = new Date("2026-10-29T10:00:30").getTime();
  const row = (overrides) => ({ session_id: "a", status: "running", received_at: "2026-10-29T10:00:10", ...overrides });

  assert.equal(runningMemberSession({ sessions: [row()] }, now)?.session_id, "a");
  assert.equal(runningMemberSession({ sessions: [row({ received_at: "2026-10-29T09:59:50" })] }, now), null);
  assert.equal(runningMemberSession({ sessions: [row({ status: "Completed" })] }, now), null);
  // Older servers: updated_at stands in for received_at.
  assert.ok(runningMemberSession({ sessions: [row({ received_at: undefined, updated_at: "2026-10-29 10:00:20" })] }, now));
  assert.equal(runningMemberSession(null, now), null);
  assert.equal(MEMBER_FOCUS_LIVE_SECONDS, 30);
});


test("member focus polling: 5 s while a session runs, else 30 s", () => {
  assert.equal(memberFocusIntervalMs(true), 5000);
  assert.equal(memberFocusIntervalMs(false), 30000);
});


test("allowed apps from a list or a JSON string", () => {
  assert.deepEqual(focusAllowedApps(["Code", 3, "Notes"]), ["Code", "Notes"]);
  assert.deepEqual(focusAllowedApps('["Code","Terminal"]'), ["Code", "Terminal"]);
  assert.deepEqual(focusAllowedApps("not json"), []);
  assert.deepEqual(focusAllowedApps(null), []);
});
