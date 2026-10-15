// --------------------------------------------------
// Focus Assistant API (api/focus.py, Blueprint "focus_api")
// --------------------------------------------------

import { fetchWithTimeout } from "../utils/http.js";


const FOCUS_API = "/api/focus";

// The Focus Assistant writes its heartbeat every second; poll it often.
export const FOCUS_LIVE_SECONDS = 5;

// History changes only when a session starts or ends.
export const FOCUS_HISTORY_SECONDS = 30;


// status is the HTTP status, or 0 when there was no answer (network error,
// or no response within the deadline: then code is "timeout").
export class FocusApiError extends Error {
  constructor(message, status, code = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}


async function request(path, { signal, timeoutMs } = {}) {
  let response;
  let text;

  try {
    ({ response, text } = await fetchWithTimeout(`${FOCUS_API}${path}`, {
      cache: "no-store",
      signal,
      timeoutMs,
      // Error bodies carry {"error": "..."}; a 404 means no Focus endpoints.
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
    throw new FocusApiError(
      "This API server has no Focus endpoints yet. Restart it with " +
      "`python start_project.py` after updating the project.",
      404
    );
  }

  let body = null;

  try {
    body = JSON.parse(text);
  } catch {
    body = null;
  }

  if (!response.ok || body === null) {
    throw new FocusApiError(
      body?.error || "The API server isn't responding. Start it with `python start_project.py`.",
      response.status
    );
  }

  return body;
}


/**
 * {running, stale, title, goal, remaining_s, current_app, allowed, status, updated_at}
 */
export function loadFocusLive(options) {
  return request("/live", options);
}


/**
 * {sessions, completed, adherence_pct, focus_seconds_today, focus_seconds_total,
 *  deviation_count, deviation_sources: [{app, seconds}]}
 */
export function loadFocusSummary(options) {
  return request("/summary", options);
}


/**
 * Newest first: [{id, title, goal, start, end, status, planned_min, actual_s,
 *  adherence_pct, deviations, switches, longest_focus_s, top_deviation_app}]
 */
export async function loadFocusSessions(options) {
  const body = await request("/sessions", options);

  if (Array.isArray(body)) return body;

  return Array.isArray(body?.sessions) ? body.sessions : [];
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
