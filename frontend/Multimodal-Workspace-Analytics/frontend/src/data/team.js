import { useEffect, useState, useSyncExternalStore } from "react";

import { formatCountdown } from "../utils/format.js";


const TEAM_API = "/api/team";
export const ADMIN_KEY_STORAGE = "workspace-team-admin-key";


// `fromApi` is true when the error came with the API's own JSON error
// body, i.e. a team endpoint ran (and so the admin key was checked).
// `missingKey` marks a request that was never sent because no key is
// stored; pages show the key form for it without "not accepted".
export class TeamApiError extends Error {
  constructor(message, status, { fromApi = false, missingKey = false } = {}) {
    super(message);
    this.status = status;
    this.fromApi = fromApi;
    this.missingKey = missingKey;
  }
}


export function isAbortError(err) {
  return err?.name === "AbortError";
}


// Every team endpoint checks the admin key before anything else, so an
// API error other than 401 means the stored key was accepted (e.g. a 404
// for a member deleted since). A 404 page from an unknown route or a
// 5xx from the dev proxy carries no JSON error and proves nothing.
export function keyWasAccepted(err) {
  return Boolean(err?.fromApi)
    && err.status >= 400
    && err.status < 500
    && err.status !== 401;
}


// The admin key is remembered in this browser only, as a convenience.
// Reads and writes are guarded because storage can be unavailable.

export function getAdminKey() {
  try {
    return localStorage.getItem(ADMIN_KEY_STORAGE) || "";
  } catch {
    return "";
  }
}


export function setAdminKey(key) {
  try {
    if (key) {
      localStorage.setItem(ADMIN_KEY_STORAGE, key);
    } else {
      localStorage.removeItem(ADMIN_KEY_STORAGE);
    }
  } catch {
    // Key just won't persist across reloads.
  }
}


// Calls listener(currentKey) when another tab saves or forgets the admin
// key (the "storage" event only fires in the other tabs). Pages use it to
// hide team data as soon as the key is locked elsewhere.
export function subscribeAdminKey(listener, target = typeof window === "undefined" ? null : window) {
  if (!target?.addEventListener) {
    return () => {};
  }

  function onStorage(event) {
    // key === null: the whole storage area was cleared.
    if (event.key === ADMIN_KEY_STORAGE || event.key === null) {
      listener(getAdminKey());
    }
  }

  target.addEventListener("storage", onStorage);
  return () => target.removeEventListener("storage", onStorage);
}


// --------------------------------------------------
// Requests
// --------------------------------------------------

async function send(path, { method = "GET", body, signal, headers } = {}) {
  const sentKey = getAdminKey();

  // Every team endpoint the dashboard calls needs the admin key. Without
  // one (e.g. locked in another tab) don't send a request that can only
  // be refused, and don't report it as a rejected key either.
  if (!sentKey) {
    throw new TeamApiError(
      "Enter the team admin key to see team data.",
      401,
      { missingKey: true }
    );
  }

  let response;

  try {
    response = await fetch(`${TEAM_API}${path}`, {
      method,
      body,
      signal,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Key": sentKey,
        ...headers,
      },
    });
  } catch (err) {
    if (isAbortError(err)) {
      throw err;
    }

    throw new TeamApiError(
      "Can't reach the dashboard server. Start the project with " +
      "`python start_project.py` and reload this page.",
      0
    );
  }

  // Unchanged since the ETag we sent.
  if (response.status === 304) {
    return { response, body: null };
  }

  let payload = null;

  try {
    payload = await response.json();
  } catch (err) {
    if (isAbortError(err)) {
      throw err;
    }

    payload = null;
  }

  // A rejected key is forgotten so the key form starts empty and polling
  // stops. Only clear it if nobody saved a different key meanwhile.
  if (response.status === 401 && getAdminKey() === sentKey) {
    setAdminKey("");
  }

  // The Vite proxy answers 5xx with no JSON when the Flask API isn't running.
  if (!response.ok && !payload?.error && response.status >= 500) {
    throw new TeamApiError(
      "The API server isn't responding. Start it with " +
      "`python api/server.py` (or `python start_project.py`).",
      response.status
    );
  }

  if (!response.ok) {
    throw new TeamApiError(
      payload?.error || `Request failed (${response.status})`,
      response.status,
      { fromApi: typeof payload?.error === "string" }
    );
  }

  return { response, body: payload };
}


async function request(path, options) {
  const { body } = await send(path, options);
  return body;
}


function withDays(days) {
  return days ? `?days=${encodeURIComponent(days)}` : "";
}


export function loadTeamOverview({ days, signal } = {}) {
  return request(`/overview${withDays(days)}`, { signal });
}


export function loadMemberDetail(memberId, { days, signal } = {}) {
  return request(`/members/${memberId}${withDays(days)}`, { signal });
}


// Lightweight "who is doing what now" view. Sends the previous ETag so an
// unchanged team costs a 304 with no body.
export async function loadTeamLive({ etag, signal } = {}) {
  const { response, body } = await send("/live", {
    signal,
    headers: etag ? { "If-None-Match": etag } : undefined,
  });

  if (response.status === 304) {
    return { notModified: true, etag, data: null };
  }

  return {
    notModified: false,
    etag: response.headers.get("ETag"),
    data: body,
  };
}


// Focus sessions a member shared (team_sync.py focus):
// {sessions: [newest first, max 100], summary: {sessions, focus_score, deviation_count}}.
export function loadMemberFocus(memberId, { signal } = {}) {
  return request(`/members/${memberId}/focus`, { signal });
}


// Cursor paging, newest first: `before` loads older rows, `after` newer ones.
export function loadMemberActivity(memberId, { before, after, limit = 100, signal } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });

  if (before) params.set("before", before);
  if (after) params.set("after", after);

  return request(`/members/${memberId}/activity?${params}`, { signal });
}


export function createInvite(name, { signal } = {}) {
  return request("/invites", {
    method: "POST",
    body: JSON.stringify({ name }),
    signal,
  });
}


// New one-time code and expiry for a member who has not joined yet.
export function reissueInvite(memberId, { signal } = {}) {
  return request(`/members/${memberId}/invite`, {
    method: "POST",
    body: "{}",
    signal,
  });
}


// Deletes the member and everything they shared, whatever their state.
export function removeMember(memberId, { signal } = {}) {
  return request(`/members/${memberId}`, { method: "DELETE", signal });
}


/**
 * Revoke an invite that has not been used, without ever deleting a member
 * who joined in the meantime.
 *
 * The page decides "pending" from data that can be seconds (or, paused,
 * minutes) old, and DELETE /members/<id> removes a joined member and all
 * their data. So the code is first rotated with POST /members/<id>/invite,
 * which the server does only while the member has not joined (409
 * otherwise) and which makes the code they were sent useless. Nobody
 * knows the new code, so nobody can join between that check and the
 * DELETE.
 *
 * Throws TeamApiError with `alreadyJoined: true` (status 409) when the
 * member has joined; nothing is deleted then. If the DELETE itself fails,
 * the error has `inviteRotated: true`: the old code no longer works.
 */
export async function revokePendingInvite(memberId, { signal } = {}) {
  try {
    await reissueInvite(memberId, { signal });
  } catch (err) {
    if (err?.status === 409) {
      const joined = new TeamApiError(
        "They have already joined, so the invite was not revoked and nothing was deleted.",
        409,
        { fromApi: err.fromApi }
      );
      joined.alreadyJoined = true;
      throw joined;
    }

    throw err;
  }

  try {
    return await removeMember(memberId, { signal });
  } catch (err) {
    if (err && typeof err === "object") {
      err.inviteRotated = true;
    }

    throw err;
  }
}


// --------------------------------------------------
// Polling
// --------------------------------------------------

/**
 * The scheduling behind usePolling, kept free of React so it can be
 * tested on its own. `task({ signal, isCurrent })` does the work.
 *
 * - run(force) starts the task now. While a run is in flight, run()
 *   returns that run's promise instead; run(true) aborts it and starts
 *   again (Refresh buttons). run() works whether or not polling is started.
 * - Responses from a superseded run report isCurrent() === false.
 * - start({ intervalMs, paused, key }) begins polling and returns stop():
 *   - runs at once and then every intervalMs, skipping the immediate run
 *     when a run for the same key finished under 1 s ago (e.g. the
 *     request that just verified a newly entered key);
 *   - pauses while the tab is hidden and runs as soon as it is visible;
 *   - `paused` sets no timer, but a key that has not loaded yet still
 *     loads once;
 *   - stop() aborts and invalidates whatever is in flight.
 */
export function createPoller(task, key = "") {
  const state = {
    task,
    key,
    controller: null,
    promise: null,
    seq: 0,
    lastDone: 0,
    lastDoneKey: null,
  };

  // The latest task and key (the task closes over the page's state).
  function update(nextTask, nextKey = "") {
    state.task = nextTask;
    state.key = nextKey;
  }

  function run(force = false) {
    if (state.controller) {
      if (!force) {
        return state.promise;
      }

      state.controller.abort();
    }

    const controller = new AbortController();
    const seq = ++state.seq;
    const runKey = state.key;
    const isCurrent = () => state.seq === seq && !controller.signal.aborted;

    state.controller = controller;
    state.promise = (async () => {
      try {
        await state.task({ signal: controller.signal, isCurrent });
      } catch (err) {
        // Tasks handle their own errors; this only catches surprises.
        if (!isAbortError(err)) {
          console.error(err);
        }
      } finally {
        if (state.controller === controller) {
          state.controller = null;
          state.lastDone = Date.now();
          state.lastDoneKey = runKey;
        }
      }
    })();

    return state.promise;
  }

  function start({ intervalMs, paused = false, key: startKey = "" }) {
    const doc = typeof document === "undefined" ? null : document;
    let timer = null;

    function cancel() {
      // Abort and invalidate whatever is in flight.
      state.seq += 1;
      state.controller?.abort();
      state.controller = null;
    }

    if (paused) {
      if (state.lastDoneKey !== startKey) {
        run();
      }

      return cancel;
    }

    function stopTimer() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    }

    function startTimer() {
      stopTimer();

      if (state.lastDoneKey !== startKey || Date.now() - state.lastDone > 1000) {
        run();
      }

      timer = setInterval(() => run(), intervalMs);
    }

    function onVisibilityChange() {
      if (doc.hidden) {
        stopTimer();
      } else {
        startTimer();
      }
    }

    if (!doc?.hidden) {
      startTimer();
    }

    doc?.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      stopTimer();
      doc?.removeEventListener("visibilitychange", onVisibilityChange);
      cancel();
    };
  }

  return { run, start, update, state };
}


/**
 * Runs `task({ signal, isCurrent })` now and then every `intervalMs`
 * (see createPoller for the rules).
 *
 * - Changing `key` (e.g. the selected range) restarts with an immediate run.
 * - `paused` stops the timer, but a new `key` still loads once.
 * - Everything in flight is aborted on unmount or when disabled.
 *
 * Returns run(force): force aborts the in-flight request and starts again
 * (used by Refresh buttons). run() also works while polling is disabled.
 */
export function usePolling(task, intervalMs, { enabled = true, paused = false, key = "" } = {}) {
  const [poller] = useState(() => createPoller(task, key));

  useEffect(() => {
    poller.update(task, key);
  });

  useEffect(() => (
    enabled ? poller.start({ intervalMs, paused, key }) : undefined
  ), [poller, enabled, paused, intervalMs, key]);

  return poller.run;
}


// --------------------------------------------------
// One shared 1 s clock, so "12 s ago" labels tick without
// re-rendering whole pages
// --------------------------------------------------

const clockListeners = new Set();
let clockTimer = null;

function subscribeClock(listener) {
  clockListeners.add(listener);

  if (clockTimer === null) {
    clockTimer = setInterval(() => {
      clockListeners.forEach((notify) => notify());
    }, 1000);
  }

  return () => {
    clockListeners.delete(listener);

    if (clockListeners.size === 0) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

function clockSnapshot() {
  return Math.floor(Date.now() / 1000);
}


// Current time in ms, re-rendering the caller once a second.
export function useNow() {
  return useSyncExternalStore(subscribeClock, clockSnapshot) * 1000;
}


// Text-only components (no JSX in this module).
export function RelativeTime({ value }) {
  useNow();
  return formatRelative(value);
}

export function ExpiryText({ value }) {
  useNow();
  return formatExpiry(value);
}


// --------------------------------------------------
// Activity log rows (newest first)
// --------------------------------------------------

// Concatenate newest-first lists, keeping the first copy of each
// timestamp (timestamps are unique per member).
export function mergeRows(...lists) {
  const seen = new Set();
  const merged = [];

  for (const list of lists) {
    for (const row of list) {
      if (!seen.has(row.timestamp)) {
        seen.add(row.timestamp);
        merged.push(row);
      }
    }
  }

  return merged;
}


// Keep only the newest `max` rows. Dropping from the old end keeps the
// list gap-free, so "Load older" (before = oldest shown) still continues it.
export function trimRows(rows, max) {
  return rows.length > max ? rows.slice(0, max) : rows;
}


// --------------------------------------------------
// Time ranges for the Team pages (?days=)
// --------------------------------------------------

// The server's days=1 is a rolling 24 hours, not the calendar day.
export const TEAM_RANGES = [
  { value: "1", label: "Today", title: "Last 24 hours" },
  { value: "7", label: "7 days", title: "Last 7 days" },
  { value: "30", label: "30 days", title: "Last 30 days" },
  { value: "all", label: "All", title: "Everything stored" },
];

export const DEFAULT_TEAM_RANGE = "30";


export function normalizeTeamRange(value) {
  return TEAM_RANGES.some((range) => range.value === value)
    ? value
    : DEFAULT_TEAM_RANGE;
}


// Phrase that ends a sentence: "Activity shared by 2 of 5 members in the last 7 days."
export function describeTeamRange(value) {
  if (value === "1") return "in the last 24 hours";
  if (value === "all") return "overall";
  return `in the last ${value} days`;
}


// URL search string that carries the range to another Team page.
export function teamRangeSearch(value) {
  return value === DEFAULT_TEAM_RANGE ? "" : `?days=${value}`;
}


// --------------------------------------------------
// Formatting shared by the Team pages
// --------------------------------------------------

// Behaviour groups keep the same colour wherever they appear
// (validated categorical slots 1-4).
export const BEHAVIOR_COLORS = {
  "Stable-activity": "#2a78d6",
  "Multitasking": "#eb6834",
  "High-switching": "#1baf7a",
  "Idle-heavy": "#eda100",
  // ml/clustering.py names a left-over cluster "Mixed-activity".
  "Mixed-activity": "#e87ba4",
};

export const BEHAVIOR_ORDER = Object.keys(BEHAVIOR_COLORS);


export function formatDuration(seconds) {
  const minutes = Math.round((seconds || 0) / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}


function formatSpan(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}


// Accepts an ISO string, a Date or epoch milliseconds.
export function formatRelative(value) {
  if (value === null || value === undefined || value === "") {
    return "Never";
  }

  const time = new Date(value).getTime();

  if (Number.isNaN(time)) {
    return "--";
  }

  const seconds = Math.max(0, (Date.now() - time) / 1000);

  if (seconds < 2) {
    return "Just now";
  }

  return `${formatSpan(seconds)} ago`;
}


export function formatExpiry(value) {
  if (!value) {
    return "--";
  }

  const time = new Date(value).getTime();

  if (Number.isNaN(time)) {
    return "--";
  }

  const seconds = (time - Date.now()) / 1000;

  return seconds > 0
    ? `Expires in ${formatSpan(seconds)}`
    : `Expired ${formatSpan(-seconds)} ago`;
}


export function isInviteExpired(member) {
  if (member.status === "expired") {
    return true;
  }

  // new Date(null) is 1970, which would read as long expired.
  if (!member.invite_expires) {
    return false;
  }

  const time = new Date(member.invite_expires).getTime();
  return member.status === "invited" && !Number.isNaN(time) && time < Date.now();
}


// "2 minutes" for the online window.
export function formatWindow(seconds) {
  if (!seconds) {
    return "";
  }

  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }

  return `${seconds} seconds`;
}


export function formatFeatureValue(feature, value) {
  if (value === null || value === undefined) return "--";
  if (feature === "idle_ratio") return `${(value * 100).toFixed(0)}%`;
  if (feature === "duration_minutes") return `${value.toFixed(0)} min`;
  return value.toFixed(2);
}


export const STATUS_LABELS = {
  online: "Online",
  offline: "Offline",
  joined: "Joined, no data yet",
  invited: "Invite pending",
  expired: "Invite expired",
};


export function isPendingInvite(status) {
  return status === "invited" || status === "expired";
}


// --------------------------------------------------
// Focus sessions of members
// --------------------------------------------------

// A running session row counts as live for this long after it was last
// received (the collector uploads it every 5 s; the server uses 30 s).
export const MEMBER_FOCUS_LIVE_SECONDS = 30;


function finiteOrNull(value) {
  const number = value === null || value === undefined || value === "" ? NaN : Number(value);
  return Number.isFinite(number) ? number : null;
}


/**
 * A member's "focus" block from the overview or the live view. The live
 * view is fresher (5 s), so it wins whenever it has one; older servers
 * send neither.
 */
export function pickMemberFocus(overviewFocus, liveFocus) {
  if (liveFocus && typeof liveFocus === "object") return liveFocus;
  if (overviewFocus && typeof overviewFocus === "object") return overviewFocus;
  return null;
}


/**
 * What the members table's Focus column shows, `elapsedS` seconds after
 * the data arrived:
 *   {kind: "live", text, remaining, score, onTask (true | false | null), title}
 *   {kind: "score", score, sessions}
 *   {kind: "none"}
 */
export function describeMemberFocus(focus, elapsedS = 0) {
  const live = focus?.live;

  if (live && typeof live === "object") {
    const raw = finiteOrNull(live.remaining_s);
    const remaining = raw === null ? null : Math.max(0, raw - Math.max(0, elapsedS));
    const score = finiteOrNull(live.score);
    const parts = ["In focus"];

    if (remaining !== null) parts.push(`${formatCountdown(remaining)} left`);
    if (score !== null) parts.push(`score ${Math.round(score)}`);

    return {
      kind: "live",
      text: parts.join(" · "),
      remaining,
      score,
      onTask: typeof live.allowed === "boolean" ? live.allowed : null,
      title: live.title || "",
      currentApp: live.current_app || "",
    };
  }

  const score = finiteOrNull(focus?.score);

  if (score !== null) {
    return { kind: "score", score, sessions: Number(focus.sessions) || 0 };
  }

  return { kind: "none" };
}


function parseTime(value) {
  if (!value) return null;

  const text = String(value);
  const time = new Date(/^\d{4}-\d{2}-\d{2} \d/.test(text) ? text.replace(" ", "T") : text).getTime();

  return Number.isNaN(time) ? null : time;
}


/**
 * The running session in a /members/<id>/focus answer, or null: the
 * newest row with status "running", received (or, from older servers,
 * updated) within MEMBER_FOCUS_LIVE_SECONDS of `now`.
 */
export function runningMemberSession(data, now = Date.now()) {
  const newest = Array.isArray(data?.sessions) ? data.sessions[0] : null;

  if (!newest || newest.status !== "running") return null;

  const seen = parseTime(newest.received_at) ?? parseTime(newest.updated_at);

  if (seen === null || now - seen > MEMBER_FOCUS_LIVE_SECONDS * 1000) return null;

  return newest;
}


/** Polling interval for a member's focus sessions: 5 s while one runs, else 30 s. */
export function memberFocusIntervalMs(running) {
  return running ? 5000 : 30000;
}


// Allowed apps arrive as a list (or, from the database, a JSON string).
export function focusAllowedApps(value) {
  if (Array.isArray(value)) return value.filter((item) => typeof item === "string");

  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : [];
    } catch {
      return [];
    }
  }

  return [];
}
