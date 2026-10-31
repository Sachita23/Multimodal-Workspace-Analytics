// --------------------------------------------------
// Focus API (api/focus.py, Blueprint "focus_api")
// --------------------------------------------------
//
// Read: /live, /summary, /sessions, /apps. Control (dashboard focus
// sessions run by the API's FocusEngine): POST /session, /session/stop,
// /session/extend, /session/allow. Every POST is JSON (the server answers
// 415 otherwise, so other websites can't start or stop sessions).

import { fetchWithTimeout } from "../utils/http.js";


const FOCUS_API = "/api/focus";

// /live while no session runs (and the tab is visible).
export const FOCUS_LIVE_SECONDS = 5;

// /live while a session runs: every second, even in a hidden tab, so a
// deviation is noticed (and notified) straight away.
export const FOCUS_RUNNING_POLL_SECONDS = 1;

// History changes only when a session starts or ends.
export const FOCUS_HISTORY_SECONDS = 30;

// Mirrors the API's validation (api/focus.py) and config.py's scoring.
export const FOCUS_DURATION_PRESETS = [15, 25, 45, 60, 90];
export const FOCUS_MAX_MINUTES = 480;
export const FOCUS_MAX_EXTEND_MINUTES = 240;
export const FOCUS_MAX_APPS = 30;
export const FOCUS_MAX_TEXT = 200;
export const FOCUS_SWITCH_PENALTY = 5;

// A deviation banner stays at least this long, so a quick switch and
// back is still seen.
export const FOCUS_ALERT_MIN_MS = 4000;


// status is the HTTP status, or 0 when there was no answer (network error,
// or no response within the deadline: then code is "timeout").
export class FocusApiError extends Error {
  constructor(message, status, code = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}


const NO_FOCUS_API =
  "This API server has no Focus endpoints yet. Restart it with " +
  "`python start_project.py` after updating the project.";

const NO_SESSION_CONTROL =
  "This API server can't run focus sessions from the dashboard yet. " +
  "Restart it with `python start_project.py` after updating the project.";


async function request(path, { signal, timeoutMs, method = "GET", body, notFound = NO_FOCUS_API } = {}) {
  let response;
  let text;

  const init = method === "GET" ? {} : {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  };

  try {
    ({ response, text } = await fetchWithTimeout(`${FOCUS_API}${path}`, {
      ...init,
      cache: "no-store",
      signal,
      timeoutMs,
      // Error bodies carry {"error": "..."}; a 404 means no such endpoint.
      readBody: (answer) => answer.status !== 404,
    }));
  } catch (error) {
    if (error?.name === "AbortError") throw error;

    if (error?.code === "timeout") {
      throw new FocusApiError(error.message, 0, "timeout");
    }

    throw new FocusApiError(
      "Can't reach the dashboard server. Start the project with " +
      "`python start_project.py` and reload this page.",
      0
    );
  }

  if (response.status === 404) {
    throw new FocusApiError(notFound, 404);
  }

  let parsed = null;

  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = null;
  }

  if (response.status === 415) {
    throw new FocusApiError(
      parsed?.error || "The server only accepts JSON requests for focus sessions.",
      415
    );
  }

  if (!response.ok || parsed === null) {
    throw new FocusApiError(
      parsed?.error || "The API server isn't responding. Start it with `python start_project.py`.",
      response.status,
      response.status === 409 ? "busy" : null
    );
  }

  return parsed;
}


/**
 * See the contract: {running, stale, session_id, source, title, goal,
 *  started_at, planned_s, remaining_s, allowed_apps, current_app,
 *  current_window, allowed, state, status, score, deviations,
 *  deviation_seconds, last_deviation, recent_deviations, updated_at, pid}
 * (older servers send only running, stale, title, goal, remaining_s,
 * current_app, allowed, status, updated_at).
 */
export function loadFocusLive(options) {
  const viewing = viewingBrowser();

  return request(viewing ? `/live?viewing=${encodeURIComponent(viewing)}` : "/live", options);
}


/**
 * The browser app showing this page while the page is the visible,
 * focused tab, else null. The server then doesn't count that browser as
 * a deviation, so checking the timer or pressing Stop costs nothing; in
 * any other tab the page is hidden and the browser counts again.
 */
export function viewingBrowser(env = globalThis) {
  const doc = env?.document;
  const nav = env?.navigator;

  if (!doc || doc.visibilityState !== "visible") return null;
  if (typeof doc.hasFocus === "function" && !doc.hasFocus()) return null;

  return dashboardBrowserApp(nav?.userAgent, [], { brave: Boolean(nav?.brave) });
}


/**
 * {sessions, completed, adherence_pct, focus_seconds_today, focus_seconds_total,
 *  deviation_count, deviation_sources: [{app, seconds}], focus_score}
 */
export function loadFocusSummary(options) {
  return request("/summary", options);
}


/**
 * Newest first: [{id, title, goal, start, end, status, planned_min, actual_s,
 *  adherence_pct, deviations, switches, longest_focus_s, top_deviation_app, score}]
 */
export async function loadFocusSessions(options) {
  const body = await request("/sessions", options);

  if (Array.isArray(body)) return body;

  return Array.isArray(body?.sessions) ? body.sessions : [];
}


function nameList(value) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string" && item.trim())
    : [];
}


/** {running: [app], recent: [app]}: apps open now, and recently used ones. */
export async function loadFocusApps(options = {}) {
  const body = await request("/apps", { ...options, notFound: NO_SESSION_CONTROL });

  return { running: nameList(body?.running), recent: nameList(body?.recent) };
}


/** 201 with the live payload; 409 (code "busy") when a session already runs. */
export function startFocusSession(payload, options = {}) {
  return request("/session", { ...options, method: "POST", body: payload, notFound: NO_SESSION_CONTROL });
}


/** {stopped: true, session: session_json | null}; 409 when nothing runs. */
export function stopFocusSession(options = {}) {
  return request("/session/stop", { ...options, method: "POST", body: {}, notFound: NO_SESSION_CONTROL });
}


/** The live payload; 409 when nothing runs. */
export function extendFocusSession(minutes, options = {}) {
  return request("/session/extend", { ...options, method: "POST", body: { minutes }, notFound: NO_SESSION_CONTROL });
}


/** Adds an app to the running session's allowed apps; the live payload. */
export function allowFocusApp(app, options = {}) {
  return request("/session/allow", { ...options, method: "POST", body: { app }, notFound: NO_SESSION_CONTROL });
}


// Focus timestamps may be ISO or "YYYY-MM-DD HH:MM:SS" (local time).
export function parseFocusTime(value) {
  if (!value) return null;

  const text = String(value);
  const date = new Date(/^\d{4}-\d{2}-\d{2} \d/.test(text) ? text.replace(" ", "T") : text);

  return Number.isNaN(date.getTime()) ? null : date;
}


// Heartbeat states in which the Focus Assistant counts neither focus nor a
// deviation (focus_gui.py sends allowed=null for them).
const UNCOUNTED_STATES = {
  "Paused (idle)": { label: "Paused (idle)", note: "Time away is not counted" },
  "Neutral": { label: "Not counted", note: "System window" },
  "Focus Assistant": { label: "Not counted", note: "In the Focus Assistant" },
  "Active window unknown": { label: "Checking…", note: "Active window unknown" },
  "Planned focus time reached": { label: "Time's up", note: "Planned focus time reached" },
};


// The engine's `state` for the moments that count as neither.
const UNCOUNTED_ENGINE_STATES = {
  idle: { label: "Paused", note: "Idle, time away is not counted" },
  neutral: { label: "System", note: "System window, not counted" },
  self: { label: "System", note: "Focus Assistant, not counted" },
  unknown: { label: "Checking…", note: "Active window unknown" },
};


/**
 * What the live card shows for a running session:
 * {label, tone ("on-task" | "off-task" | "neutral"), note}.
 */
export function describeFocusLive(live) {
  const app = live?.current_app && live.current_app !== "Focus Assistant"
    ? live.current_app
    : null;
  const using = app ? `Using ${app}` : "";

  if (live?.allowed === true) {
    return { label: "On task", tone: "on-task", note: using || live.status || "" };
  }

  if (live?.allowed === false) {
    return { label: "Off task", tone: "off-task", note: using || live.status || "" };
  }

  const engine = UNCOUNTED_ENGINE_STATES[live?.state];

  if (engine) {
    return {
      label: engine.label,
      tone: "neutral",
      note: [engine.note, live.state === "self" ? "" : using].filter(Boolean).join(" · "),
    };
  }

  const known = UNCOUNTED_STATES[live?.status];

  if (known) {
    return {
      label: known.label,
      tone: "neutral",
      note: [known.note, using].filter(Boolean).join(" · "),
    };
  }

  return { label: live?.status || "Checking…", tone: "neutral", note: using };
}


/**
 * What the Focus page shows, from its three polled requests (each
 * {value, error, loading}): {state, error} with state
 *   "not-installed"  any request got a 404 (the server has no Focus API)
 *   "loading"        a first answer is still missing
 *   "error"          neither the summary nor the history ever loaded
 *   "ready"          otherwise (the live card reports its own errors)
 */
export function focusPageState({ live, summary, sessions }) {
  const requests = [summary, live, sessions];
  const missing = requests.find((item) => item.error?.status === 404);

  if (missing) {
    return { state: "not-installed", error: missing.error };
  }

  if (requests.some((item) => item.loading)) {
    return { state: "loading", error: null };
  }

  if (!summary.value && !sessions.value && (summary.error || sessions.error)) {
    return { state: "error", error: summary.error || sessions.error };
  }

  return { state: "ready", error: null };
}


// --------------------------------------------------
// Live polling: every second while a session runs (even in a hidden tab,
// so deviations are noticed), every 5 s and only while visible otherwise
// --------------------------------------------------

export function isLiveRunning(live) {
  return Boolean(live && live.running && !live.stale);
}


export function focusPollSeconds(live) {
  return isLiveRunning(live) ? FOCUS_RUNNING_POLL_SECONDS : FOCUS_LIVE_SECONDS;
}


/**
 * Calls load({signal}) now and then again focusPollSeconds(last value)
 * after each answer. With no session running, a hidden tab stops polling
 * until it is visible again; with one running, polling goes on.
 *
 * Returns {stop, refresh, push}:
 *   refresh()   load again now (after the current call, if one is running)
 *   push(value) a fresher value from elsewhere (e.g. the answer to a POST):
 *               reported at once, and an older call still in flight is
 *               ignored so it can't overwrite it
 *   stop()      abort and clear everything
 */
export function createFocusLivePoller(load, {
  onValue = () => {},
  onError = () => {},
  doc = globalThis.document,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  intervalFor = focusPollSeconds,
} = {}) {
  const controller = new AbortController();
  const { signal } = controller;
  let last = null;
  let timer = null;
  let inFlight = false;
  let queued = false;
  let generation = 0;

  function cancelTimer() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
  }

  function schedule() {
    cancelTimer();

    if (!signal.aborted) {
      timer = setTimer(tick, intervalFor(last) * 1000);
    }
  }

  function tick() {
    timer = null;

    // Idle and hidden: wait for visibilitychange.
    if (doc?.hidden && !isLiveRunning(last)) return;

    run();
  }

  async function run() {
    if (signal.aborted) return;

    if (inFlight) {
      queued = true;
      return;
    }

    cancelTimer();
    inFlight = true;

    const mine = generation;

    try {
      const value = await load({ signal });

      if (!signal.aborted && mine === generation) {
        last = value;
        onValue(value);
      }
    } catch (error) {
      if (!signal.aborted && mine === generation) onError(error);
    } finally {
      inFlight = false;

      if (!signal.aborted) {
        if (queued) {
          queued = false;
          run();
        } else {
          schedule();
        }
      }
    }
  }

  function onVisibility() {
    if (!doc.hidden && !inFlight) run();
  }

  doc?.addEventListener?.("visibilitychange", onVisibility);
  run();

  return {
    refresh: run,
    push(value) {
      if (signal.aborted) return;

      generation += 1;
      last = value;
      onValue(value);

      if (!inFlight) schedule();
    },
    stop() {
      controller.abort();
      cancelTimer();
      doc?.removeEventListener?.("visibilitychange", onVisibility);
    },
  };
}


// --------------------------------------------------
// Scores
// --------------------------------------------------

/**
 * Colour band and text label of a focus score (0-100):
 * >= 80 good, 50-79 warning, < 50 critical; "none" without a score.
 */
export function scoreBand(score) {
  const value = score === null || score === undefined || score === "" ? NaN : Number(score);

  if (!Number.isFinite(value)) return { band: "none", label: "No score yet" };
  if (value >= 80) return { band: "good", label: "Good" };
  if (value >= 50) return { band: "warning", label: "Slipping" };

  return { band: "critical", label: "Low" };
}


// "87", "87.5" (overall scores have one decimal), "--".
export function formatScore(score) {
  const value = score === null || score === undefined || score === "" ? NaN : Number(score);

  if (!Number.isFinite(value)) return "--";

  const rounded = Math.round(value * 10) / 10;

  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}


/**
 * Countdown for a running session, `elapsedS` seconds after the payload
 * was received: {remaining, planned, fraction} (fraction of the planned
 * time already used, 0-1, or null without a plan).
 */
export function focusProgress(live, elapsedS = 0) {
  const remainingRaw = live?.remaining_s;
  const remaining = remainingRaw === null || remainingRaw === undefined || !Number.isFinite(Number(remainingRaw))
    ? null
    : Math.max(0, Number(remainingRaw) - Math.max(0, elapsedS));
  const planned = Number(live?.planned_s) > 0 ? Number(live.planned_s) : null;

  const fraction = planned !== null && remaining !== null
    ? Math.min(1, Math.max(0, 1 - remaining / planned))
    : null;

  return { remaining, planned, fraction };
}


// --------------------------------------------------
// Start form
// --------------------------------------------------

// Trimmed, inner whitespace collapsed.
export function cleanAppName(name) {
  return String(name ?? "").trim().replace(/\s+/g, " ");
}


function sameApp(a, b) {
  return cleanAppName(a).toLowerCase() === cleanAppName(b).toLowerCase();
}


export function hasApp(list, name) {
  return list.some((item) => sameApp(item, name));
}


// The app names the collector (collector.clean_application_name) reports
// for each browser, most likely first.
const BROWSER_APPS = [
  { test: (ua) => /\bEdg(e|A|iOS)?\//.test(ua), names: ["Microsoft Edge"] },
  { test: (ua) => /\bOPR\//.test(ua), names: ["Opera"] },
  { test: (ua) => /\bVivaldi\//.test(ua), names: ["Vivaldi"] },
  { test: (ua) => /\b(Firefox|FxiOS)\//.test(ua), names: ["Mozilla Firefox", "Firefox"] },
  { test: (ua) => /\b(Chrome|Chromium|CriOS)\//.test(ua), names: ["Google Chrome", "Chromium", "Brave Browser", "Arc"] },
  { test: (ua) => /\bSafari\//.test(ua), names: ["Safari"] },
];


/**
 * The app name of the browser showing this dashboard, or null. Coming back
 * to the dashboard (to stop or extend a session) is a switch to this app,
 * so the start form offers it. A name among the open apps wins (Brave and
 * Arc look like Chrome to the page).
 */
export function dashboardBrowserApp(userAgent, runningApps = [], { brave = false } = {}) {
  const ua = String(userAgent ?? "");
  const entry = BROWSER_APPS.find((item) => item.test(ua));

  if (!entry) return null;

  const names = brave ? ["Brave Browser", ...entry.names] : entry.names;
  const open = names.find((name) => hasApp(runningApps || [], name));

  return open || names[0];
}


/**
 * The app chips to offer: open apps first, then recently used ones, then
 * any added by hand; each name once (case-insensitive). `browser` (the
 * dashboard's own browser) is offered too, marked dashboard: true.
 * [{name, running, recent, dashboard}]
 */
export function appChoices(apps, selected = [], browser = null) {
  const choices = [];

  function add(name, flags) {
    const clean = cleanAppName(name);

    if (!clean) return;

    const existing = choices.find((choice) => sameApp(choice.name, clean));

    if (existing) {
      existing.running ||= flags.running;
      existing.recent ||= flags.recent;
      existing.dashboard ||= flags.dashboard;
    } else {
      choices.push({ name: clean, running: flags.running, recent: flags.recent, dashboard: flags.dashboard });
    }
  }

  (apps?.running || []).forEach((name) => add(name, { running: true, recent: false, dashboard: false }));
  (apps?.recent || []).forEach((name) => add(name, { running: false, recent: true, dashboard: false }));
  if (browser) add(browser, { running: true, recent: false, dashboard: true });
  selected.forEach((name) => add(name, { running: false, recent: false, dashboard: false }));

  return choices;
}


/** Selects or unselects an app chip. */
export function toggleApp(selected, name) {
  return hasApp(selected, name)
    ? selected.filter((item) => !sameApp(item, name))
    : [...selected, cleanAppName(name)];
}


/** {selected, error}: adds a typed app name (selected) unless invalid. */
export function addCustomApp(selected, name) {
  const clean = cleanAppName(name);

  if (!clean) return { selected, error: "Type an app name first." };
  if (clean.length > FOCUS_MAX_TEXT) return { selected, error: "That app name is too long." };
  if (hasApp(selected, clean)) return { selected, error: null };
  if (selected.length >= FOCUS_MAX_APPS) {
    return { selected, error: `Choose at most ${FOCUS_MAX_APPS} apps.` };
  }

  return { selected: [...selected, clean], error: null };
}


/**
 * {errors: {title?, goal?, minutes?, apps?}, payload | null}; payload is
 * the POST /api/focus/session body.
 */
export function validateFocusForm({ title, goal, minutes, apps }) {
  const errors = {};
  const cleanTitle = String(title ?? "").trim();
  const cleanGoal = String(goal ?? "").trim();
  const text = String(minutes ?? "").trim();
  const duration = /^\d+$/.test(text) ? Number(text) : NaN;
  const allowed = [];

  (apps || []).forEach((name) => {
    if (cleanAppName(name) && !hasApp(allowed, name)) allowed.push(cleanAppName(name));
  });

  if (!cleanTitle) errors.title = "Give the session a title.";
  else if (cleanTitle.length > FOCUS_MAX_TEXT) errors.title = `Keep the title under ${FOCUS_MAX_TEXT} characters.`;

  if (cleanGoal.length > FOCUS_MAX_TEXT) errors.goal = `Keep the goal under ${FOCUS_MAX_TEXT} characters.`;

  if (!Number.isInteger(duration) || duration < 1 || duration > FOCUS_MAX_MINUTES) {
    errors.minutes = `Choose a whole number of minutes from 1 to ${FOCUS_MAX_MINUTES}.`;
  }

  if (allowed.length === 0) errors.apps = "Choose at least one app you will work in.";
  else if (allowed.length > FOCUS_MAX_APPS) errors.apps = `Choose at most ${FOCUS_MAX_APPS} apps.`;

  if (Object.keys(errors).length > 0) return { errors, payload: null };

  const payload = { title: cleanTitle, duration_minutes: duration, allowed_apps: allowed };

  if (cleanGoal) payload.goal = cleanGoal;

  return { errors, payload };
}


// --------------------------------------------------
// Deviation alerts
// --------------------------------------------------

// "Code", "Code or Notes", "Code, Notes or Terminal", "A, B, C or 2 more".
export function formatAppList(apps) {
  const names = (apps || []).map(cleanAppName).filter(Boolean);

  if (names.length === 0) return "";
  if (names.length === 1) return names[0];
  if (names.length > 4) return `${names.slice(0, 3).join(", ")} or ${names.length - 3} more`;

  return `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
}


/** The banner (and notification) text for an alert from focusAlertStep. */
export function alertMessage(alert) {
  if (!alert) return "";

  const points = alert.penalty ? ` \u2212${alert.penalty} points.` : "";
  const apps = formatAppList(alert.allowedApps);
  const back = apps ? ` Return to ${apps}.` : " Return to your focus apps.";
  const missed = Number(alert.missed) > 0
    ? ` (${alert.missed} more switch${alert.missed === 1 ? "" : "es"} away since the last update.)`
    : "";

  return `You switched to ${alert.app}.${points}${back}${missed}`;
}


export const INITIAL_ALERT_STATE = {
  initialized: false,
  sessionId: null,
  seenKey: null,
  seenAt: null,
  wasAllowed: null,
  alert: null,
};


function deviationKey(live) {
  const last = live?.last_deviation;

  return last && last.at ? `${last.at}|${last.app ?? ""}` : null;
}


// The newest deviation time in a payload (ms), or null.
function newestDeviationAt(live) {
  const times = [live?.last_deviation?.at, ...(Array.isArray(live?.recent_deviations)
    ? live.recent_deviations.map((item) => item?.at)
    : [])]
    .map((value) => parseFocusTime(value)?.getTime())
    .filter((value) => Number.isFinite(value));

  return times.length ? Math.max(...times) : null;
}


function latestOf(a, b) {
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}


// Deviation episodes in recent_deviations newer than `seenAt`, besides the
// current one: switches that happened between two answers (a slow or
// throttled poll), so they are not silently merged into one alert.
function missedDeviations(live, seenAt) {
  if (!Array.isArray(live?.recent_deviations)) return 0;

  const newer = live.recent_deviations.filter((item) => {
    const at = parseFocusTime(item?.at)?.getTime();
    return Number.isFinite(at) && (seenAt === null || at > seenAt);
  });

  return Math.max(0, newer.length - 1);
}


function alertFrom(live, now) {
  const last = live.last_deviation;
  const penalty = last
    ? (Number.isFinite(Number(last.penalty)) ? Number(last.penalty) : FOCUS_SWITCH_PENALTY)
    : null;

  return {
    key: deviationKey(live),
    app: cleanAppName(last?.app) || cleanAppName(live.current_app) || "another app",
    windowTitle: last?.window_title ?? live.current_window ?? "",
    at: last?.at ?? live.updated_at ?? null,
    penalty,
    allowedApps: Array.isArray(live.allowed_apps) ? live.allowed_apps : [],
    shownAt: now,
  };
}


/**
 * One step of the deviation alert, for each /live answer:
 * {state, fresh}. `fresh` is the alert to notify about (a NEW deviation:
 * last_deviation.at changed), else null. state.alert is the banner to
 * show: it stays while allowed === false, and at least
 * FOCUS_ALERT_MIN_MS, and goes when the session ends.
 *
 * The first answer is a baseline: a deviation already under way
 * (allowed === false) is shown but not notified again (a reload must not
 * re-notify), and an old last_deviation while back on task shows nothing.
 * Heartbeats with no last_deviation field (the Tk Focus Assistant) count
 * a change of allowed to false as the new deviation, without a penalty;
 * a paused or neutral moment (allowed null) doesn't end that episode.
 */
export function focusAlertStep(previous, live, now = Date.now()) {
  const prev = previous || INITIAL_ALERT_STATE;

  if (!isLiveRunning(live)) {
    return {
      state: { ...prev, initialized: true, wasAllowed: null, alert: null },
      fresh: null,
    };
  }

  const sessionId = live.session_id ?? null;
  const newSession = prev.initialized && sessionId !== prev.sessionId;
  const baseline = !prev.initialized;
  const legacy = live.last_deviation === undefined;
  const key = deviationKey(live);
  const seenKey = newSession ? null : prev.seenKey;
  const seenAt = newSession ? null : (prev.seenAt ?? null);
  const wasAllowed = newSession ? null : prev.wasAllowed;

  let isNew;

  if (legacy) {
    isNew = live.allowed === false && wasAllowed !== false;
  } else {
    isNew = key !== null && key !== seenKey;
  }

  let alert = newSession ? null : prev.alert;
  let fresh = null;

  // On the first answer only an ongoing deviation is shown: an old
  // last_deviation while on task is history, not an alert.
  if (isNew && baseline && live.allowed !== false) isNew = false;

  if (isNew) {
    alert = alertFrom(live, now);

    if (!baseline) {
      const missed = legacy ? 0 : missedDeviations(live, seenAt);
      if (missed > 0) alert = { ...alert, missed };
      fresh = alert;
    }
  } else if (alert && live.allowed !== false && now - alert.shownAt >= FOCUS_ALERT_MIN_MS) {
    alert = null;
  } else if (!alert && live.allowed === false) {
    // Still off task in a known deviation (after a heartbeat blip, or a
    // pause inside one episode): show it again, without notifying.
    alert = alertFrom(live, now);
  }

  // The allowed apps can grow ("Allow this app") while it is shown.
  if (alert && Array.isArray(live.allowed_apps)) {
    alert = { ...alert, allowedApps: live.allowed_apps };
  }

  return {
    state: {
      initialized: true,
      sessionId,
      seenKey: key ?? seenKey,
      seenAt: latestOf(seenAt, newestDeviationAt(live)),
      // Only on task / off task change the episode; null (paused, system
      // window) keeps the last one.
      wasAllowed: typeof live.allowed === "boolean" ? live.allowed : wasAllowed,
      alert,
    },
    fresh,
  };
}


// --------------------------------------------------
// Browser notifications (used only once the user granted them)
// --------------------------------------------------

export function notificationPermission(env = globalThis) {
  const api = env?.Notification;

  return api && typeof api.permission === "string" ? api.permission : "unsupported";
}


// Asks once (on Start, inside the click). Never throws.
export async function requestNotificationPermission(env = globalThis) {
  const api = env?.Notification;

  if (!api || api.permission !== "default" || typeof api.requestPermission !== "function") {
    return notificationPermission(env);
  }

  try {
    return (await api.requestPermission()) || notificationPermission(env);
  } catch {
    return notificationPermission(env);
  }
}


let notificationSeq = 0;


/** Shows a browser notification for an alert if permitted; true if shown. */
export function notifyDeviation(alert, env = globalThis) {
  if (!alert || notificationPermission(env) !== "granted") return false;

  try {
    // A tag of its own per deviation: a shared tag would silently replace
    // the first notification (no banner, no sound) for every later one.
    const notification = new env.Notification("Focus session: off task", {
      body: alertMessage(alert),
      // The episode's key when known (two open tabs then share one).
      tag: `focus-deviation-${alert.key ?? `local-${(notificationSeq += 1)}`}`,
    });

    if (notification && typeof env.focus === "function") {
      notification.onclick = () => env.focus();
    }

    return true;
  } catch {
    return false;
  }
}


// --------------------------------------------------
// End of a session
// --------------------------------------------------

/** The finished session in the history (newest first) for a live payload. */
export function findFinishedSession(sessions, live) {
  if (!live || !Array.isArray(sessions)) return null;

  if (live.session_id) {
    const byId = sessions.find((session) => String(session.id) === String(live.session_id));
    if (byId) return byId;
  }

  const started = parseFocusTime(live.started_at)?.getTime();

  if (started === undefined) return null;

  return sessions.find((session) => (
    parseFocusTime(session.start)?.getTime() === started
  )) ?? null;
}


/**
 * The final score card: the recorded session when there is one (it is
 * authoritative), else the last live payload of the session.
 */
export function finalSessionSummary(lastLive, session = null) {
  if (!lastLive && !session) return null;

  const pick = (recorded, live) => (recorded === null || recorded === undefined ? live : recorded);

  return {
    title: session?.title || lastLive?.title || "Focus session",
    status: session?.status || "Ended",
    score: pick(session?.score, lastLive?.score ?? null),
    deviations: pick(session?.deviations, lastLive?.deviations ?? 0),
    deviationSeconds: lastLive?.deviation_seconds ?? null,
    plannedMinutes: pick(session?.planned_min, lastLive?.planned_s ? Math.round(lastLive.planned_s / 60) : null),
    actualSeconds: session?.actual_s ?? null,
    adherencePct: session?.adherence_pct ?? null,
    recorded: Boolean(session),
    recentDeviations: Array.isArray(lastLive?.recent_deviations) ? lastLive.recent_deviations : [],
  };
}
