// Unit tests for dashboard focus sessions: control requests, the live
// poller, deviation alerts, score bands and the start form.
// Run with `npm test`.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  FOCUS_ALERT_MIN_MS,
  FocusApiError,
  INITIAL_ALERT_STATE,
  addCustomApp,
  alertMessage,
  allowFocusApp,
  appChoices,
  createFocusLivePoller,
  dashboardBrowserApp,
  describeFocusLive,
  extendFocusSession,
  finalSessionSummary,
  findFinishedSession,
  focusAlertStep,
  focusPollSeconds,
  focusProgress,
  formatAppList,
  formatScore,
  loadFocusApps,
  notifyDeviation,
  requestNotificationPermission,
  scoreBand,
  startFocusSession,
  stopFocusSession,
  toggleApp,
  validateFocusForm,
} from "./focus.js";


const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});


function stubFetch(handler) {
  const calls = [];

  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, init });
    return handler(url, init);
  };

  return calls;
}


const json = (body, status = 200) => new Response(JSON.stringify(body), { status });


// --------------------------------------------------
// Requests
// --------------------------------------------------

test("control requests are JSON POSTs to the contract's paths", async () => {
  const calls = stubFetch((url) => json(url.endsWith("/stop") ? { stopped: true, session: null } : { running: true }, url.endsWith("/session") ? 201 : 200));

  await startFocusSession({ title: "Write", duration_minutes: 25, allowed_apps: ["Code"] }, { timeoutMs: 500 });
  assert.deepEqual(await stopFocusSession({ timeoutMs: 500 }), { stopped: true, session: null });
  await extendFocusSession(5, { timeoutMs: 500 });
  await allowFocusApp("Safari", { timeoutMs: 500 });

  assert.deepEqual(calls.map((call) => call.url), [
    "/api/focus/session",
    "/api/focus/session/stop",
    "/api/focus/session/extend",
    "/api/focus/session/allow",
  ]);

  for (const call of calls) {
    assert.equal(call.init.method, "POST");
    assert.equal(call.init.headers["Content-Type"], "application/json");
  }

  assert.deepEqual(JSON.parse(calls[0].init.body), { title: "Write", duration_minutes: 25, allowed_apps: ["Code"] });
  assert.deepEqual(JSON.parse(calls[1].init.body), {});
  assert.deepEqual(JSON.parse(calls[2].init.body), { minutes: 5 });
  assert.deepEqual(JSON.parse(calls[3].init.body), { app: "Safari" });
});


test("409, 400 and 415 keep the server's message; 404 says the server can't run sessions", async () => {
  stubFetch(() => json({ error: "A focus session is already running." }, 409));
  await assert.rejects(startFocusSession({}, { timeoutMs: 500 }), (error) => (
    error instanceof FocusApiError && error.status === 409 && error.code === "busy"
      && error.message === "A focus session is already running."
  ));

  stubFetch(() => json({ error: "duration_minutes must be 1-480" }, 400));
  await assert.rejects(startFocusSession({}, { timeoutMs: 500 }), (error) => (
    error.status === 400 && /1-480/.test(error.message)
  ));

  stubFetch(() => new Response("Unsupported Media Type", { status: 415 }));
  await assert.rejects(stopFocusSession({ timeoutMs: 500 }), (error) => (
    error.status === 415 && /JSON/.test(error.message)
  ));

  stubFetch(() => new Response("<html>Not Found</html>", { status: 404 }));
  await assert.rejects(extendFocusSession(5, { timeoutMs: 500 }), (error) => (
    error.status === 404 && /can't run focus sessions/.test(error.message)
  ));
  await assert.rejects(loadFocusApps({ timeoutMs: 500 }), (error) => error.status === 404);
});


test("the app list keeps only names", async () => {
  stubFetch(() => json({ running: ["Code", "", 3, "Safari"], recent: null }));
  assert.deepEqual(await loadFocusApps({ timeoutMs: 500 }), { running: ["Code", "Safari"], recent: [] });
});


// --------------------------------------------------
// Live poller
// --------------------------------------------------

function fakeDocument() {
  const listeners = new Set();

  return {
    hidden: false,
    addEventListener: (type, listener) => listeners.add(listener),
    removeEventListener: (type, listener) => listeners.delete(listener),
    listenerCount: () => listeners.size,
    setHidden(hidden) {
      this.hidden = hidden;
      listeners.forEach((listener) => listener());
    },
  };
}


function fakeTimers() {
  const pending = new Map();
  let next = 1;

  return {
    pending,
    setTimer: (callback, ms) => {
      const id = next++;
      pending.set(id, { callback, ms });
      return id;
    },
    clearTimer: (id) => pending.delete(id),
    // Fires the only pending timer and returns its delay.
    fire() {
      assert.equal(pending.size, 1, "exactly one timer is pending");
      const [[id, { callback, ms }]] = pending;
      pending.delete(id);
      callback();
      return ms;
    },
  };
}


const flush = () => new Promise((resolve) => setImmediate(resolve));

const RUNNING = { running: true, stale: false, session_id: "s1" };
const IDLE = { running: false, stale: false };


test("polls every second while a session runs, every 5 s otherwise", async () => {
  assert.equal(focusPollSeconds(RUNNING), 1);
  assert.equal(focusPollSeconds(IDLE), 5);
  assert.equal(focusPollSeconds({ running: true, stale: true }), 5);
  assert.equal(focusPollSeconds(null), 5);

  const answers = [IDLE, RUNNING, RUNNING];
  const values = [];
  const timers = fakeTimers();
  const doc = fakeDocument();

  const poller = createFocusLivePoller(async () => answers.shift(), {
    onValue: (value) => values.push(value),
    doc,
    ...timers,
  });

  await flush();
  assert.deepEqual(values, [IDLE]);
  assert.equal(timers.fire(), 5000);

  await flush();
  assert.deepEqual(values, [IDLE, RUNNING]);
  assert.equal([...timers.pending.values()][0].ms, 1000);

  poller.stop();
  assert.equal(timers.pending.size, 0);
  assert.equal(doc.listenerCount(), 0);
});


test("a hidden tab keeps polling during a session but not without one", async () => {
  const timers = fakeTimers();
  const doc = fakeDocument();
  let answer = RUNNING;
  let calls = 0;

  const poller = createFocusLivePoller(async () => {
    calls += 1;
    return answer;
  }, { doc, ...timers });

  await flush();
  doc.hidden = true;

  timers.fire();
  await flush();
  assert.equal(calls, 2, "still polling while hidden: the session runs");

  answer = IDLE;
  timers.fire();
  await flush();
  assert.equal(calls, 3);

  timers.fire();
  await flush();
  assert.equal(calls, 3, "idle and hidden: no request");
  assert.equal(timers.pending.size, 0);

  doc.setHidden(false);
  await flush();
  assert.equal(calls, 4, "visible again: polls at once");

  poller.stop();
});


test("push() reports a fresher value and an older answer in flight can't overwrite it", async () => {
  const timers = fakeTimers();
  const values = [];
  const pending = [];

  const poller = createFocusLivePoller(() => new Promise((resolve) => pending.push(resolve)), {
    onValue: (value) => values.push(value),
    doc: fakeDocument(),
    ...timers,
  });

  poller.push(RUNNING);
  pending[0](IDLE);
  await flush();

  assert.deepEqual(values, [RUNNING]);
  assert.equal([...timers.pending.values()][0].ms, 1000);

  // refresh() while a call runs: runs once more right after it.
  timers.fire();
  poller.refresh();
  assert.equal(pending.length, 2);
  pending[1](RUNNING);
  await flush();
  assert.equal(pending.length, 3);

  poller.stop();
  pending[2](IDLE);
  await flush();
  assert.deepEqual(values, [RUNNING, RUNNING]);
});


test("errors are reported and polling goes on", async () => {
  const timers = fakeTimers();
  const errors = [];

  const poller = createFocusLivePoller(async () => {
    throw new FocusApiError("down", 0);
  }, { onError: (error) => errors.push(error.message), doc: fakeDocument(), ...timers });

  await flush();
  assert.deepEqual(errors, ["down"]);
  assert.equal(timers.pending.size, 1);
  poller.stop();
});


// --------------------------------------------------
// Deviation alerts
// --------------------------------------------------

function live(overrides = {}) {
  return {
    running: true,
    stale: false,
    session_id: "s1",
    allowed_apps: ["Code", "Terminal"],
    current_app: "Code",
    allowed: true,
    state: "allowed",
    score: 100,
    deviations: 0,
    last_deviation: null,
    ...overrides,
  };
}

const safari = { app: "Safari", window_title: "YouTube", at: "2026-10-29T10:00:05", penalty: 5 };


test("a NEW deviation alerts once, with the contract's message", () => {
  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);
  assert.equal(step.fresh, null);
  assert.equal(step.state.alert, null);

  step = focusAlertStep(step.state, live({ allowed: false, current_app: "Safari", last_deviation: safari }), 1000);
  assert.ok(step.fresh);
  assert.equal(step.fresh.app, "Safari");
  assert.equal(alertMessage(step.fresh), "You switched to Safari. −5 points. Return to Code or Terminal.");

  // Same deviation on the next polls: banner stays, no new notification.
  step = focusAlertStep(step.state, live({ allowed: false, current_app: "Safari", last_deviation: safari }), 2000);
  assert.equal(step.fresh, null);
  assert.ok(step.state.alert);

  step = focusAlertStep(step.state, live({ allowed: false, current_app: "Safari", last_deviation: safari }), 60000);
  assert.ok(step.state.alert, "stays while allowed === false");

  // Back on task: the banner goes.
  step = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 61000);
  assert.equal(step.state.alert, null);

  // Another switch later is a new deviation.
  const mail = { app: "Mail", window_title: "", at: "2026-10-29T10:05:00", penalty: 5 };
  step = focusAlertStep(step.state, live({ allowed: false, current_app: "Mail", last_deviation: mail }), 62000);
  assert.equal(step.fresh?.app, "Mail");
});


test("a quick switch and back still shows the banner briefly", () => {
  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);

  step = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 1000);
  assert.ok(step.fresh, "notified even though already back");
  assert.ok(step.state.alert);

  step = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 1000 + FOCUS_ALERT_MIN_MS - 1);
  assert.ok(step.state.alert);

  step = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 1000 + FOCUS_ALERT_MIN_MS);
  assert.equal(step.state.alert, null);
});


test("a page opened mid-deviation shows the banner without notifying again", () => {
  const step = focusAlertStep(INITIAL_ALERT_STATE, live({ allowed: false, last_deviation: safari }), 0);

  assert.equal(step.fresh, null);
  assert.equal(step.state.alert?.app, "Safari");
});


test("a session that ends clears the banner; a heartbeat blip doesn't re-notify", () => {
  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);
  step = focusAlertStep(step.state, live({ allowed: false, last_deviation: safari }), 1000);

  step = focusAlertStep(step.state, { running: true, stale: true }, 2000);
  assert.equal(step.state.alert, null);

  step = focusAlertStep(step.state, live({ allowed: false, last_deviation: safari }), 3000);
  assert.equal(step.fresh, null, "same session, same deviation");
  assert.equal(step.state.alert?.app, "Safari", "still off task: the banner is back");

  // A new session with its own first deviation does notify.
  step = focusAlertStep(step.state, IDLE, 4000);
  step = focusAlertStep(step.state, live({ session_id: "s2", allowed: false, last_deviation: { ...safari, at: "2026-10-29T11:00:00" } }), 5000);
  assert.ok(step.fresh);
});


test("Tk heartbeats without last_deviation alert when allowed turns false (no points)", () => {
  const legacy = (allowed) => ({ running: true, stale: false, current_app: allowed ? "Code" : "Safari", allowed, status: "x" });

  let step = focusAlertStep(INITIAL_ALERT_STATE, legacy(true), 0);
  step = focusAlertStep(step.state, legacy(false), 1000);
  assert.equal(step.fresh?.app, "Safari");
  assert.equal(step.fresh.penalty, null);
  assert.equal(alertMessage(step.fresh), "You switched to Safari. Return to your focus apps.");

  step = focusAlertStep(step.state, legacy(false), 2000);
  assert.equal(step.fresh, null);
});


test("allowing the app updates the banner's list", () => {
  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);
  step = focusAlertStep(step.state, live({ allowed: false, last_deviation: safari }), 1000);
  step = focusAlertStep(step.state, live({ allowed: false, allowed_apps: ["Code", "Terminal", "Safari"], last_deviation: safari }), 1500);

  assert.deepEqual(step.state.alert.allowedApps, ["Code", "Terminal", "Safari"]);
});


test("app lists read naturally", () => {
  assert.equal(formatAppList([]), "");
  assert.equal(formatAppList(["Code"]), "Code");
  assert.equal(formatAppList(["Code", "Notes", "Terminal"]), "Code, Notes or Terminal");
  assert.equal(formatAppList(["A", "B", "C", "D", "E"]), "A, B, C or 2 more");
});


// --------------------------------------------------
// Notifications
// --------------------------------------------------

function fakeNotificationEnv(permission, answer = permission) {
  const shown = [];

  class FakeNotification {
    constructor(title, options) {
      shown.push({ title, ...options, instance: this });
    }
  }

  FakeNotification.permission = permission;
  FakeNotification.requestPermission = async () => {
    FakeNotification.permission = answer;
    return answer;
  };

  return { env: { Notification: FakeNotification }, shown };
}


test("browser notifications only when granted", async () => {
  const alert = { app: "Safari", penalty: 5, allowedApps: ["Code"] };

  const denied = fakeNotificationEnv("denied");
  assert.equal(notifyDeviation(alert, denied.env), false);
  assert.equal(denied.shown.length, 0);

  const asked = fakeNotificationEnv("default", "granted");
  assert.equal(await requestNotificationPermission(asked.env), "granted");
  assert.equal(notifyDeviation(alert, asked.env), true);
  assert.match(asked.shown[0].body, /You switched to Safari/);

  assert.equal(await requestNotificationPermission({}), "unsupported");
  assert.equal(notifyDeviation(alert, {}), false);
});


// --------------------------------------------------
// Scores and progress
// --------------------------------------------------

test("score bands: >= 80 good, 50-79 warning, < 50 critical", () => {
  assert.equal(scoreBand(100).band, "good");
  assert.equal(scoreBand(80).band, "good");
  assert.equal(scoreBand(79.9).band, "warning");
  assert.equal(scoreBand(50).band, "warning");
  assert.equal(scoreBand(49).band, "critical");
  assert.equal(scoreBand(0).band, "critical");
  assert.equal(scoreBand(null).band, "none");
  assert.equal(scoreBand(undefined).band, "none");
  assert.equal(scoreBand("abc").band, "none");

  for (const score of [95, 60, 10, null]) {
    assert.ok(scoreBand(score).label, "every band has a text label");
  }
});


test("scores format as whole numbers or one decimal", () => {
  assert.equal(formatScore(87), "87");
  assert.equal(formatScore(87.46), "87.5");
  assert.equal(formatScore(0), "0");
  assert.equal(formatScore(null), "--");
});


test("progress counts down from the payload and never goes below zero", () => {
  assert.deepEqual(focusProgress({ remaining_s: 600, planned_s: 1500 }, 0), { remaining: 600, planned: 1500, fraction: 0.6 });
  assert.equal(focusProgress({ remaining_s: 600, planned_s: 1500 }, 2.5).remaining, 597.5);
  assert.equal(focusProgress({ remaining_s: 1, planned_s: 1500 }, 10).remaining, 0);
  assert.deepEqual(focusProgress({ remaining_s: null }), { remaining: null, planned: null, fraction: null });
});


// --------------------------------------------------
// Start form
// --------------------------------------------------

test("app chips: open apps first, then recent, then typed ones, each once", () => {
  const choices = appChoices({ running: ["Code", "Safari"], recent: ["code", "Notes"] }, ["Figma", "notes"]);

  assert.deepEqual(choices, [
    { name: "Code", running: true, recent: true, dashboard: false },
    { name: "Safari", running: true, recent: false, dashboard: false },
    { name: "Notes", running: false, recent: true, dashboard: false },
    { name: "Figma", running: false, recent: false, dashboard: false },
  ]);

  assert.deepEqual(toggleApp(["Code"], "code"), []);
  assert.deepEqual(toggleApp(["Code"], "Notes"), ["Code", "Notes"]);
});


test("adding an app by hand", () => {
  assert.deepEqual(addCustomApp(["Code"], "  Visual   Studio  "), { selected: ["Code", "Visual Studio"], error: null });
  assert.deepEqual(addCustomApp(["Code"], "code"), { selected: ["Code"], error: null });
  assert.match(addCustomApp(["Code"], "  ").error, /Type an app name/);

  const full = Array.from({ length: 30 }, (_, i) => `App ${i}`);
  assert.match(addCustomApp(full, "One more").error, /at most 30/);
});


test("form validation builds the POST body", () => {
  assert.deepEqual(
    validateFocusForm({ title: " Write report ", goal: "", minutes: "25", apps: ["Code", "code", "Notes"] }),
    { errors: {}, payload: { title: "Write report", duration_minutes: 25, allowed_apps: ["Code", "Notes"] } }
  );

  assert.equal(
    validateFocusForm({ title: "T", goal: " Draft intro ", minutes: 90, apps: ["Code"] }).payload.goal,
    "Draft intro"
  );

  const bad = validateFocusForm({ title: "", minutes: "0", apps: [] });
  assert.equal(bad.payload, null);
  assert.deepEqual(Object.keys(bad.errors).sort(), ["apps", "minutes", "title"]);

  assert.ok(validateFocusForm({ title: "T", minutes: "481", apps: ["Code"] }).errors.minutes);
  assert.ok(validateFocusForm({ title: "T", minutes: "2.5", apps: ["Code"] }).errors.minutes);
  assert.ok(validateFocusForm({ title: "T", minutes: "480", apps: ["Code"] }).payload);
});


// --------------------------------------------------
// Live card and the end of a session
// --------------------------------------------------

test("engine states read as Paused / System", () => {
  assert.equal(describeFocusLive(live({ allowed: null, state: "idle" })).label, "Paused");
  assert.equal(describeFocusLive(live({ allowed: null, state: "neutral", current_app: "Dock" })).label, "System");
  assert.equal(describeFocusLive(live({ allowed: false, state: "deviation", current_app: "Safari" })).label, "Off task");
});


test("the final summary prefers the recorded session", () => {
  const lastLive = live({ session_id: "s9", started_at: "2026-10-29T10:00:00", title: "Write", score: 80, deviations: 2, deviation_seconds: 70, planned_s: 1500 });
  const sessions = [
    { id: "other", start: "2026-10-29T09:00:00" },
    { id: "s9", start: "2026-10-29T10:00:00", title: "Write", status: "Completed", score: 78, deviations: 2, planned_min: 25, actual_s: 1500, adherence_pct: 95.3 },
  ];

  const recorded = findFinishedSession(sessions, lastLive);
  assert.equal(recorded.id, "s9");

  // Matched by start time when ids differ.
  assert.equal(findFinishedSession([{ id: "x", start: "2026-10-29 10:00:00" }], lastLive).id, "x");
  assert.equal(findFinishedSession(sessions, null), null);

  const summary = finalSessionSummary(lastLive, recorded);
  assert.equal(summary.score, 78);
  assert.equal(summary.status, "Completed");
  assert.equal(summary.deviationSeconds, 70);
  assert.equal(summary.recorded, true);

  const fromLive = finalSessionSummary(lastLive, null);
  assert.equal(fromLive.score, 80);
  assert.equal(fromLive.plannedMinutes, 25);
  assert.equal(fromLive.recorded, false);

  assert.equal(finalSessionSummary(null, null), null);
});


// --------------------------------------------------
// Review fixes
// --------------------------------------------------

test("a reload while back on task shows no stale banner for an old deviation", () => {
  const step = focusAlertStep(INITIAL_ALERT_STATE, live({ allowed: true, last_deviation: safari }), 0);

  assert.equal(step.fresh, null);
  assert.equal(step.state.alert, null);

  // The old deviation is remembered: it doesn't alert on the next poll.
  const next = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 1000);
  assert.equal(next.fresh, null);
  assert.equal(next.state.alert, null);

  // A later switch away does.
  const mail = { app: "Mail", window_title: "", at: "2026-10-29T10:20:00", penalty: 5 };
  const later = focusAlertStep(next.state, live({ allowed: false, last_deviation: mail }), 2000);
  assert.equal(later.fresh?.app, "Mail");
});


test("each deviation's browser notification has its own tag, so none is replaced silently", async () => {
  const { env, shown } = fakeNotificationEnv("granted");
  let focused = 0;
  env.focus = () => { focused += 1; };

  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);
  step = focusAlertStep(step.state, live({ allowed: false, last_deviation: safari }), 1000);
  notifyDeviation(step.fresh, env);

  const slack = { app: "Slack", window_title: "", at: "2026-10-29T10:03:00", penalty: 5 };
  step = focusAlertStep(step.state, live({ allowed: true, last_deviation: safari }), 9000);
  step = focusAlertStep(step.state, live({ allowed: false, last_deviation: slack }), 10000);
  notifyDeviation(step.fresh, env);

  assert.equal(shown.length, 2);
  assert.notEqual(shown[0].tag, shown[1].tag);
  assert.match(shown[0].tag, /^focus-deviation-/);

  shown[1].instance.onclick();
  assert.equal(focused, 1, "clicking the notification brings the dashboard back");
});


test("switches between two answers are counted, not merged into one alert", () => {
  let step = focusAlertStep(INITIAL_ALERT_STATE, live(), 0);

  const mail = { app: "Mail", window_title: "", at: "2026-10-29T10:00:05", penalty: 5 };
  step = focusAlertStep(step.state, live({
    allowed: false,
    last_deviation: mail,
    recent_deviations: [
      { app: "Mail", at: "2026-10-29T10:00:05", seconds: 1 },
      { app: "Safari", at: "2026-10-29T10:00:02", seconds: 2 },
    ],
  }), 5000);

  assert.equal(step.fresh?.app, "Mail");
  assert.equal(step.fresh.missed, 1);
  assert.match(alertMessage(step.fresh), /1 more switch away since the last update/);

  // The next switch, seen on time, doesn't count the old ones again.
  const notes = { app: "Notes", window_title: "", at: "2026-10-29T10:01:00", penalty: 5 };
  step = focusAlertStep(step.state, live({
    allowed: false,
    last_deviation: notes,
    recent_deviations: [
      { app: "Notes", at: "2026-10-29T10:01:00", seconds: 0 },
      { app: "Mail", at: "2026-10-29T10:00:05", seconds: 50 },
      { app: "Safari", at: "2026-10-29T10:00:02", seconds: 2 },
    ],
  }), 61000);
  assert.equal(step.fresh?.app, "Notes");
  assert.equal(step.fresh.missed, undefined);
  assert.doesNotMatch(alertMessage(step.fresh), /more switch/);
});


test("Tk heartbeats: a paused or neutral moment inside one excursion doesn't re-notify", () => {
  const legacy = (allowed) => ({ running: true, stale: false, current_app: allowed === true ? "Code" : "Safari", allowed, status: "x" });

  let step = focusAlertStep(INITIAL_ALERT_STATE, legacy(true), 0);
  step = focusAlertStep(step.state, legacy(false), 1000);
  assert.ok(step.fresh);

  step = focusAlertStep(step.state, legacy(null), 2000);
  step = focusAlertStep(step.state, legacy(false), 3000);
  assert.equal(step.fresh, null, "same excursion");
  assert.ok(step.state.alert, "banner still shown while off task");

  // Back on task, then away again: a new deviation.
  step = focusAlertStep(step.state, legacy(true), 10000);
  step = focusAlertStep(step.state, legacy(false), 11000);
  assert.ok(step.fresh);
});


test("the dashboard's own browser is offered as an app chip", () => {
  const chrome = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
  const safariUa = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15";
  const firefox = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0";
  const edge = `${chrome} Edg/130.0.0.0`;

  assert.equal(dashboardBrowserApp(chrome), "Google Chrome");
  assert.equal(dashboardBrowserApp(safariUa), "Safari");
  assert.equal(dashboardBrowserApp(firefox), "Mozilla Firefox");
  assert.equal(dashboardBrowserApp(firefox, ["Firefox", "Code"]), "Firefox");
  assert.equal(dashboardBrowserApp(edge), "Microsoft Edge");
  assert.equal(dashboardBrowserApp(chrome, ["Arc", "Code"]), "Arc");
  assert.equal(dashboardBrowserApp(chrome, [], { brave: true }), "Brave Browser");
  assert.equal(dashboardBrowserApp(""), null);

  const choices = appChoices({ running: ["Code", "Google Chrome"], recent: [] }, [], "Google Chrome");
  assert.deepEqual(choices.map((c) => [c.name, c.dashboard]), [["Code", false], ["Google Chrome", true]]);

  const notOpen = appChoices({ running: ["Code"], recent: [] }, [], "Safari");
  assert.equal(notOpen.at(-1).name, "Safari");
  assert.equal(notOpen.at(-1).dashboard, true);
});
