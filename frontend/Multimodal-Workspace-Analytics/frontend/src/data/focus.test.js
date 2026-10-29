// Unit tests for the Focus API client. Run with `npm test`.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  FocusApiError,
  describeFocusLive,
  loadFocusLive,
  loadFocusSessions,
  loadFocusSummary,
} from "./focus.js";


const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});


function stubFetch(handler) {
  const urls = [];

  globalThis.fetch = async (url, init = {}) => {
    urls.push(url);
    return handler(url, init);
  };

  return urls;
}


test("loads the contract shapes from /api/focus/*", async () => {
  const live = { running: false, stale: true, title: null, goal: null, remaining_s: null, current_app: null, allowed: null, status: "Focus Assistant not running", updated_at: null };
  const sessions = [{ id: "s1", title: "Write" }];

  const urls = stubFetch((url) => new Response(JSON.stringify(
    url.endsWith("/live") ? live : url.endsWith("/sessions") ? { sessions } : { sessions: 0 }
  ), { status: 200 }));

  assert.deepEqual(await loadFocusLive({ timeoutMs: 500 }), live);
  assert.deepEqual(await loadFocusSessions({ timeoutMs: 500 }), sessions);
  assert.deepEqual(await loadFocusSummary({ timeoutMs: 500 }), { sessions: 0 });
  assert.deepEqual(urls, ["/api/focus/live", "/api/focus/sessions", "/api/focus/summary"]);
});


test("404 means the server has no Focus endpoints; other errors keep the server's message", async () => {
  stubFetch(() => new Response("<html>Not Found</html>", { status: 404 }));
  await assert.rejects(loadFocusLive({ timeoutMs: 500 }), (error) => (
    error instanceof FocusApiError && error.status === 404 && /no Focus endpoints/.test(error.message)
  ));

  stubFetch(() => new Response(JSON.stringify({ error: "focus data unreadable" }), { status: 500 }));
  await assert.rejects(loadFocusSummary({ timeoutMs: 500 }), (error) => (
    error.status === 500 && error.message === "focus data unreadable"
  ));

  stubFetch(() => Promise.reject(new TypeError("fetch failed")));
  await assert.rejects(loadFocusLive({ timeoutMs: 500 }), (error) => error.status === 0 && error.code === null);
});


test("a Focus request that never answers times out instead of freezing the page", async () => {
  stubFetch((url, init) => new Promise((_, reject) => {
    init.signal?.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  }));

  await assert.rejects(loadFocusLive({ timeoutMs: 50 }), (error) => (
    error instanceof FocusApiError && error.code === "timeout" && error.status === 0
  ));
});


function running(overrides) {
  return { running: true, stale: false, title: "Write report", goal: "g", remaining_s: 900, updated_at: "x", ...overrides };
}


test("live card: on/off task from 'allowed'", () => {
  assert.deepEqual(
    describeFocusLive(running({ current_app: "Code", allowed: true, status: "Within focus zone" })),
    { label: "On task", tone: "on-task", note: "Using Code" }
  );
  assert.deepEqual(
    describeFocusLive(running({ current_app: "Safari", allowed: false, status: "Focus deviation" })),
    { label: "Off task", tone: "off-task", note: "Using Safari" }
  );
});


test("live card: uncounted states show the assistant's status, not 'Checking…'", () => {
  const idle = describeFocusLive(running({ current_app: "Code", allowed: null, status: "Paused (idle)" }));
  assert.equal(idle.label, "Paused (idle)");
  assert.match(idle.note, /not counted/);
  assert.match(idle.note, /Using Code/);

  const neutral = describeFocusLive(running({ current_app: "Dock", allowed: null, status: "Neutral" }));
  assert.equal(neutral.label, "Not counted");
  assert.match(neutral.note, /System window/);

  const own = describeFocusLive(running({ current_app: "Focus Assistant", allowed: null, status: "Focus Assistant" }));
  assert.equal(own.label, "Not counted");
  assert.doesNotMatch(own.note, /Using/);

  const unknown = describeFocusLive(running({ current_app: null, allowed: null, status: "Active window unknown" }));
  assert.equal(unknown.label, "Checking…");

  const done = describeFocusLive(running({ current_app: null, allowed: null, status: "Planned focus time reached" }));
  assert.equal(done.label, "Time's up");
});


test("viewingBrowser reports the browser only while the page is visible and focused", async () => {
  const { viewingBrowser } = await import("./focus.js");
  const chrome = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
  const env = (visibilityState, focused) => ({
    document: { visibilityState, hasFocus: () => focused },
    navigator: { userAgent: chrome },
  });

  assert.equal(viewingBrowser(env("visible", true)), "Google Chrome");
  assert.equal(viewingBrowser(env("hidden", true)), null);
  assert.equal(viewingBrowser(env("visible", false)), null);
  assert.equal(viewingBrowser({}), null);
});
