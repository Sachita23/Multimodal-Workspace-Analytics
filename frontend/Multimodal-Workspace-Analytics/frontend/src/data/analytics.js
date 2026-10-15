import Papa from "papaparse";

import { fetchWithTimeout } from "../utils/http.js";


// --------------------------------------------------
// Constants shared with the provider and pages
// --------------------------------------------------

export const ANALYTICS_API = "/api/analytics.csv";
export const STATUS_API = "/api/status";

// Matches config.PIPELINE_REFRESH_SECONDS on the Python side.
export const REFRESH_SECONDS = 10;

// The newest observation is "stale" (collector stopped) after this long.
export const STALE_DATA_SECONDS = 60;

// Below this many samples, insights are not meaningful yet.
export const MIN_ROWS_FOR_INSIGHTS = 60;

// Anomalous rows closer together than this (same session) form one episode.
const EPISODE_GAP_SECONDS = 300;

const HOUR_MS = 3600 * 1000;
const DAY_MS = 24 * HOUR_MS;

export const RANGES = [
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "all", label: "All time" },
];

export function rangeLabel(range) {
  return RANGES.find((item) => item.value === range)?.label || "All time";
}


// --------------------------------------------------
// Parsing
// --------------------------------------------------

function toNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return 0;
  }

  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}


function toOptionalNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return null;
  }

  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}


function toBoolean(value) {
  if (typeof value === "boolean") {
    return value;
  }

  const normalized = String(value).toLowerCase().trim();

  return normalized === "true" || normalized === "1" || normalized === "yes";
}


function round(value, decimals = 2) {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}


const TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/;

// Pipeline timestamps are local wall-clock time ("YYYY-MM-DD HH:MM:SS").
export function parseTimestamp(text) {
  const match = TIMESTAMP_PATTERN.exec(String(text || ""));

  if (!match) {
    return Number.NaN;
  }

  const [, year, month, day, hour, minute, second] = match.map(Number);

  return new Date(year, month - 1, day, hour, minute, second).getTime();
}


// A desktop without a battery reports 100 % and not charging, and a failed
// read reports 0 %; neither is a real battery reading.
function hasBatteryReading(row) {
  if (row.charging) return true;
  return row.battery_percent > 0 && row.battery_percent < 100;
}


// `raw` is one CSV record (an array of strings) and `column` maps each header
// name to its index; a column the file does not have reads as undefined.
function normalizeRow(raw, column) {
  const value = (name) => raw[column[name]];
  const timestamp = value("timestamp");

  return {
    timestamp: timestamp || "",
    ts: parseTimestamp(timestamp),
    application: value("application") || "Unknown",
    window_title: value("window_title") || "",
    activity_status: value("activity_status") || "Unknown",
    idle_seconds: toNumber(value("idle_seconds")),
    cpu_percent: toNumber(value("cpu_percent")),
    memory_percent: toNumber(value("memory_percent")),
    battery_percent: toNumber(value("battery_percent")),
    charging: toBoolean(value("charging")),
    duration_seconds: toNumber(value("duration_seconds")),
    active_duration_seconds: toNumber(value("active_duration_seconds")),
    idle_duration_seconds: toNumber(value("idle_duration_seconds")),
    session_id: String(value("session_id") || ""),
    application_switch: toBoolean(value("application_switch")),
    window_switch: toBoolean(value("window_switch")),
    idle_ratio: toNumber(value("idle_ratio")),
    switch_frequency: toNumber(value("switch_frequency")),
    unique_app_count: toNumber(value("unique_app_count")),
    hour: toNumber(value("hour")),
    cluster_id: toOptionalNumber(value("cluster_id")),
    behavior_label: value("behavior_label") || "Unclassified",
    anomaly_score: toNumber(value("anomaly_score")),
    is_anomaly: toBoolean(value("is_anomaly")),
  };
}


// An empty line (Papa reads it as one empty value).
function isBlankRecord(raw) {
  return raw.length === 1 && raw[0] === "";
}


/**
 * Parse the pipeline CSV. Rows that do not have exactly one value per
 * column, or that Papa flags (a torn read while the file was being
 * rewritten), and rows without a timestamp or session are dropped.
 *
 * Records are read as arrays rather than keyed objects (Papa's header mode),
 * which parses a large history about twice as fast. Empty lines are kept
 * while parsing so that the record numbers in Papa's errors line up with
 * the records (they count empty lines), and skipped afterwards.
 *
 * Returns {rows, dropped, total}.
 */
export function parseAnalyticsCsv(csvText) {
  const text = String(csvText || "");

  const { data, errors } = Papa.parse(text, {
    header: false,
    skipEmptyLines: false,
  });

  // The first line with content is the header; an empty file has none.
  const headerIndex = data.findIndex((raw) => !isBlankRecord(raw));
  const fields = headerIndex === -1 ? [] : data[headerIndex];
  const firstRecord = headerIndex === -1 ? data.length : headerIndex + 1;

  if (text.trim() && !fields.includes("timestamp")) {
    throw new Error("The analytics file has no header row.");
  }

  const column = Object.create(null);

  fields.forEach((name, index) => {
    if (!Object.hasOwn(column, name)) column[name] = index;
  });

  const badRows = new Set(
    errors
      .map((error) => error.row)
      .filter((row) => Number.isInteger(row))
  );

  // The pipeline always ends the file with a newline. Without one, the last
  // row may have been cut off mid-value even if its field count looks right.
  if (text && !text.endsWith("\n")) {
    badRows.add(data.length - 1);
  }

  const rows = [];
  let total = 0;

  for (let index = firstRecord; index < data.length; index += 1) {
    const raw = data[index];

    if (isBlankRecord(raw)) continue;

    total += 1;

    if (badRows.has(index) || raw.length !== fields.length) continue;

    const row = normalizeRow(raw, column);

    if (!row.timestamp || !Number.isFinite(row.ts) || !row.session_id) continue;

    rows.push(row);
  }

  rows.sort((a, b) => a.ts - b.ts);

  return {
    rows,
    dropped: total - rows.length,
    total,
  };
}


// --------------------------------------------------
// Fetching
// --------------------------------------------------

const UNREACHABLE_MESSAGE =
  "Can't reach the dashboard server. Start the project with " +
  "`python start_project.py` and reload this page.";

const NO_DATA_MESSAGE =
  "No processed data yet. The collector and live pipeline create it " +
  "within a minute of starting `python start_project.py`.";

const API_DOWN_MESSAGE =
  "The API server isn't responding. Start it with " +
  "`python api/server.py` (or `python start_project.py`).";


// An Error whose message is safe to show, tagged so callers can tell an
// authoritative answer ("no-data": the server is up but has no processed
// file) from a transient one ("unreachable", "server", "partial", "timeout").
function analyticsError(message, code) {
  const error = new Error(message);
  error.code = code;
  return error;
}


/**
 * Fetch the processed CSV. Sends If-None-Match when an etag is given.
 * Gives up with error.code "timeout" when the server goes quiet for
 * `timeoutMs` (see fetchWithTimeout).
 *
 * `parse(text)` turns the body into {rows, dropped, total} and may return a
 * promise; the dashboard passes one that parses in a Web Worker, because a
 * month of history is tens of MB and would block the page for over a second.
 *
 * Resolves to {notModified: true} on a 304, otherwise
 * {notModified: false, etag, rows, dropped, total}.
 * Rejects with an Error whose message is safe to show to the user and whose
 * `code` is "no-data" (404), "unreachable", "server", "partial" or "timeout".
 */
export async function fetchAnalyticsRows({ signal, etag, timeoutMs, parse = parseAnalyticsCsv } = {}) {
  let response;
  let text;

  try {
    ({ response, text } = await fetchWithTimeout(ANALYTICS_API, {
      cache: "no-store",
      signal,
      timeoutMs,
      headers: etag ? { "If-None-Match": etag } : {},
    }));
  } catch (error) {
    if (error?.name === "AbortError" || error?.code === "timeout") throw error;
    throw analyticsError(UNREACHABLE_MESSAGE, "unreachable");
  }

  if (response.status === 304) {
    return { notModified: true, etag };
  }

  if (response.status === 404) {
    throw analyticsError(NO_DATA_MESSAGE, "no-data");
  }

  if (!response.ok) {
    throw analyticsError(API_DOWN_MESSAGE, "server");
  }

  const parsed = await parse(text);

  if (signal?.aborted) {
    const error = new Error("The request was aborted.");
    error.name = "AbortError";
    throw error;
  }

  // Many dropped rows means this was not a clean snapshot; keep the old one.
  if (parsed.dropped > Math.max(5, parsed.total * 0.1)) {
    throw analyticsError("Received a partial analytics file; retrying shortly.", "partial");
  }

  return {
    notModified: false,
    etag: response.headers.get("ETag"),
    ...parsed,
  };
}


/**
 * Fetch /api/status. Resolves to null when the endpoint does not exist
 * (older API server), cannot be reached or does not answer in time, so a
 * stale status is never kept around. Rejects only when aborted.
 */
export async function fetchStatus({ signal, timeoutMs } = {}) {
  let text;

  try {
    ({ text } = await fetchWithTimeout(STATUS_API, { cache: "no-store", signal, timeoutMs }));
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return null;
  }

  if (text === null) {
    return null;
  }

  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}


// --------------------------------------------------
// Ranges
// --------------------------------------------------

function startOfHour(ms) {
  const date = new Date(ms);
  date.setMinutes(0, 0, 0);
  return date.getTime();
}


// Every range is the trailing window that its label names ("Last 24 hours"
// starts exactly 24 h ago). The 24 h timeline shows that window as clock
// hours, so its first and last buckets are usually partial (see buildHourly).
export function rangeStart(range, now = Date.now()) {
  if (range === "24h") return now - DAY_MS;
  if (range === "7d") return now - 7 * DAY_MS;
  if (range === "30d") return now - 30 * DAY_MS;

  return null;
}


export function filterRowsByRange(rows, range, now = Date.now()) {
  const start = rangeStart(range, now);

  return start === null ? rows : rows.filter((row) => row.ts >= start);
}


// "24h" when there is recent data, otherwise everything.
export function autoRange(rows, now = Date.now()) {
  const start = rangeStart("24h", now);

  return rows.some((row) => row.ts >= start) ? "24h" : "all";
}


// Position of a range in RANGES (narrowest first); unknown ranges are "all".
function rangeIndex(range) {
  const index = RANGES.findIndex((item) => item.value === range);
  return index === -1 ? RANGES.length - 1 : index;
}


/**
 * The narrowest range that is at least as wide as `current` and starts at
 * or before `ms`, so something that happened at `ms` is shown in full.
 */
export function rangeCovering(ms, current = "24h", now = Date.now()) {
  for (let index = rangeIndex(current); index < RANGES.length; index += 1) {
    const { value } = RANGES[index];
    const start = rangeStart(value, now);

    if (start === null || start <= ms) return value;
  }

  return "all";
}


// --------------------------------------------------
// Aggregation helpers
// --------------------------------------------------

function sum(rows, key) {
  let total = 0;

  for (const row of rows) total += row[key];

  return total;
}


function average(values) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}


function hourLabel(hour) {
  return `${String(hour).padStart(2, "0")}:00`;
}


export function aggregateApplications(rows) {
  const map = new Map();

  for (const row of rows) {
    let item = map.get(row.application);

    if (!item) {
      item = { application: row.application, duration: 0, idle: 0, records: 0, switches: 0, lastSeen: 0 };
      map.set(row.application, item);
    }

    item.duration += row.active_duration_seconds;
    item.idle += row.idle_duration_seconds;
    item.records += 1;
    item.lastSeen = Math.max(item.lastSeen, row.ts);

    if (row.application_switch) item.switches += 1;
  }

  return [...map.values()]
    .map((item) => ({ ...item, duration: round(item.duration) }))
    .sort((a, b) => b.duration - a.duration || b.records - a.records);
}


// An hour of the profile counts as observed once it was watched for at
// least this much of the hour.
const FULL_HOUR_TOLERANCE_SECONDS = 60;


// Local midnight at the start of each calendar date that has samples.
function dataDayStarts(rows) {
  const starts = new Set();
  let dayStart = Number.NaN;
  let dayEnd = Number.NaN;

  for (const row of rows) {
    if (row.ts >= dayStart && row.ts < dayEnd) continue;

    const date = new Date(row.ts);
    dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    dayEnd = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1).getTime();
    starts.add(dayStart);
  }

  return starts;
}


/**
 * Seconds of each clock hour of the day (index 0-23) that lie inside
 * [start, end], counted only on the given calendar dates (dates without any
 * sample, such as a weekend away, are not "days with data").
 */
function observedSecondsByHourOfDay(start, end, dayStarts) {
  const seconds = new Array(24).fill(0);

  for (const dayStart of dayStarts) {
    const date = new Date(dayStart);
    const dayEnd = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1).getTime();
    let hourStart = dayStart;

    while (hourStart < dayEnd) {
      let next = startOfHour(hourStart + HOUR_MS);

      // A DST fall-back repeats a local hour; never step backwards.
      if (next <= hourStart) next = hourStart + HOUR_MS;

      next = Math.min(next, dayEnd);

      const overlap = Math.min(next, end) - Math.max(hourStart, start);

      if (overlap > 0) {
        seconds[new Date(hourStart).getHours()] += overlap / 1000;
      }

      hourStart = next;
    }
  }

  return seconds;
}


/**
 * Hourly buckets.
 * - 24h: a zero-filled timeline of the clock hours that the last 24 hours
 *   touch ("timeline"): usually 25, because the window starts part-way
 *   through an hour. The first and the current (last) bucket are partial.
 * - longer ranges: an hour-of-day profile ("profile"), averaged per day with
 *   data. Each clock hour is divided by how often that hour was actually
 *   observed: a range that starts at 14:10, or data that starts at 10:35,
 *   covers some clock hours one time fewer than others, and counting those
 *   partial days as whole days would under-report every hour.
 */
function buildHourly(rows, range, now) {
  const timeline = range === "24h";
  const buckets = new Map();
  let observed = null;
  let lastKey = null;

  if (timeline) {
    const start = rangeStart("24h", now);
    lastKey = startOfHour(now);

    for (let key = startOfHour(start); key <= lastKey; key += HOUR_MS) {
      const hour = new Date(key).getHours();

      // Partial: the still-filling current hour, and the first hour when
      // the window starts after its beginning.
      buckets.set(key, { key, hour, label: hourLabel(hour), partial: key === lastKey || key < start });
    }
  } else {
    for (let hour = 0; hour < 24; hour += 1) {
      buckets.set(hour, { key: hour, hour, label: hourLabel(hour), partial: false });
    }

    observed = new Array(24).fill(0);

    if (rows.length) {
      let firstRow = rows[0];
      let lastRow = rows[0];

      for (const row of rows) {
        if (row.ts < firstRow.ts) firstRow = row;
        if (row.ts > lastRow.ts) lastRow = row;
      }

      // Samples are bucketed by their timestamp, so a bucket holds one
      // duration_seconds per sample from its first timestamp onwards. The
      // window that matches that bucketing runs from the first sample to the
      // end of the last sample's duration.
      observed = observedSecondsByHourOfDay(
        firstRow.ts,
        lastRow.ts + lastRow.duration_seconds * 1000,
        dataDayStarts(rows)
      );
    }
  }

  for (const bucket of buckets.values()) {
    Object.assign(bucket, { active: 0, idle: 0, records: 0, cpu: [], memory: [], battery: [] });
  }

  for (const row of rows) {
    const key = timeline
      ? Math.min(startOfHour(row.ts), lastKey)
      : new Date(row.ts).getHours();
    const bucket = buckets.get(key);

    if (!bucket) continue;

    bucket.active += row.active_duration_seconds;
    bucket.idle += row.idle_duration_seconds;
    bucket.records += 1;
    bucket.cpu.push(row.cpu_percent);
    bucket.memory.push(row.memory_percent);

    if (hasBatteryReading(row)) bucket.battery.push(row.battery_percent);
  }

  for (const bucket of buckets.values()) {
    const tracked = bucket.active + bucket.idle;

    if (timeline) {
      bucket.days = 1;
      continue;
    }

    const seconds = observed[bucket.hour];

    // How many times this clock hour was observed. Never less than one (an
    // hour seen only partly shows the minutes it actually had), and never
    // less than its samples cover, so an hour never exceeds 60 minutes.
    bucket.days = Math.max(1, seconds / 3600, tracked / 3600);

    // Seen for less than one whole hour (for example only the still-filling
    // current hour): not a fair candidate for the peak or quiet window.
    bucket.partial = seconds < 3600 - FULL_HOUR_TOLERANCE_SECONDS;
  }

  const days = timeline
    ? 1
    : Math.max(1, Math.round(Math.max(...observed) / 3600));

  const list = [...buckets.values()];

  const hourlyActivity = list.map((bucket) => ({
    key: bucket.key,
    hour: bucket.hour,
    label: bucket.label,
    partial: bucket.partial,
    records: bucket.records,
    active: round(bucket.active / 60 / bucket.days, 1),
    idle: round(bucket.idle / 60 / bucket.days, 1),
    coverage: (bucket.active + bucket.idle) / (3600 * bucket.days),
  }));

  const resourceByHour = list.map((bucket) => {
    const cpu = average(bucket.cpu);
    const memory = average(bucket.memory);
    const battery = average(bucket.battery);

    return {
      key: bucket.key,
      hour: bucket.hour,
      label: bucket.label,
      cpu: cpu === null ? null : round(cpu, 1),
      memory: memory === null ? null : round(memory, 1),
      battery: battery === null ? null : round(battery, 1),
    };
  });

  // Peak and quietest windows ignore partial hours (the still-filling
  // current hour, or a profile hour seen for less than one whole hour) and,
  // when possible, hours that are less than half covered by samples.
  const candidates = hourlyActivity.filter((item) => !item.partial && item.records > 0);
  const covered = candidates.filter((item) => item.coverage >= 0.5);
  const pool = covered.length ? covered : candidates;

  const peakHour = pool.length
    ? pool.reduce((best, item) => (item.active > best.active ? item : best))
    : null;

  const quietHour = pool.length > 1
    ? pool.reduce((best, item) => (item.active < best.active ? item : best))
    : null;

  const mostIdleHour = pool.length
    ? pool.reduce((best, item) => (item.idle > best.idle ? item : best))
    : null;

  return {
    hourlyActivity,
    resourceByHour,
    hourlyMode: timeline ? "timeline" : "profile",
    hourlyDays: days,
    peakHour,
    quietHour,
    mostIdleHour,
  };
}


function buildSessions(rows) {
  const map = new Map();

  for (const row of rows) {
    let session = map.get(row.session_id);

    if (!session) {
      session = {
        session_id: row.session_id,
        startMs: row.ts,
        endMs: row.ts,
        duration: 0,
        active: 0,
        idle: 0,
        apps: new Set(),
        switches: 0,
        switchFrequency: 0,
        cpu: 0,
        memory: 0,
        records: 0,
        cluster: row.cluster_id,
        behavior: row.behavior_label,
        anomalous: false,
      };
      map.set(row.session_id, session);
    }

    session.startMs = Math.min(session.startMs, row.ts);
    session.endMs = Math.max(session.endMs, row.ts);
    session.duration += row.duration_seconds;
    session.active += row.active_duration_seconds;
    session.idle += row.idle_duration_seconds;
    session.apps.add(row.application);
    session.switches += row.application_switch ? 1 : 0;
    session.switchFrequency += row.switch_frequency;
    session.cpu += row.cpu_percent;
    session.memory += row.memory_percent;
    session.records += 1;
    session.anomalous = session.anomalous || row.is_anomaly;

    // Labels are per session; the newest row carries the current one.
    session.cluster = row.cluster_id;
    session.behavior = row.behavior_label;
  }

  const newest = rows.length ? rows[rows.length - 1].session_id : null;

  return [...map.values()]
    .map((session) => {
      const tracked = session.active + session.idle;

      return {
        session_id: session.session_id,
        start: new Date(session.startMs),
        end: new Date(session.endMs),
        startMs: session.startMs,
        endMs: session.endMs,
        duration: round(session.duration),
        active: round(session.active),
        idle: round(session.idle),
        applications: session.apps.size,
        switches: session.switches,
        switchFrequency: round(session.switchFrequency / session.records),
        idleRatio: tracked > 0 ? round((session.idle / tracked) * 100, 1) : 0,
        avgCPU: round(session.cpu / session.records, 1),
        avgMemory: round(session.memory / session.records, 1),
        cluster: session.cluster,
        behavior: session.behavior,
        records: session.records,
        anomalous: session.anomalous,
        isLatest: session.session_id === newest,
      };
    })
    .sort((a, b) => b.endMs - a.endMs);
}


function buildBehaviors(rows, sessions) {
  const map = new Map();

  for (const row of rows) {
    let item = map.get(row.behavior_label);

    if (!item) {
      item = { behavior: row.behavior_label, records: 0, seconds: 0, active: 0, idle: 0, switches: 0, applications: 0 };
      map.set(row.behavior_label, item);
    }

    item.records += 1;
    item.seconds += row.duration_seconds;
    item.active += row.active_duration_seconds;
    item.idle += row.idle_duration_seconds;
    item.switches += row.application_switch ? 1 : 0;
    item.applications += row.unique_app_count;
  }

  const sessionCounts = new Map();

  for (const session of sessions) {
    sessionCounts.set(session.behavior, (sessionCounts.get(session.behavior) || 0) + 1);
  }

  const behaviorAnalytics = [...map.values()]
    .map((item) => {
      const tracked = item.active + item.idle;

      return {
        behavior: item.behavior,
        records: item.records,
        sessions: sessionCounts.get(item.behavior) || 0,
        active: round(item.active),
        idle: round(item.idle),
        switches: item.switches,
        avgApplications: round(item.applications / item.records, 1),
        // 0-100, duration-weighted
        avgIdleRatio: tracked > 0 ? round((item.idle / tracked) * 100, 1) : 0,
      };
    })
    .sort((a, b) => b.records - a.records);

  const behaviorDistribution = [...map.values()]
    .map((item) => ({
      behavior: item.behavior,
      count: item.records,
      // Fall back to sample count when durations are not available yet.
      seconds: round(item.seconds) || item.records,
    }))
    .sort((a, b) => b.seconds - a.seconds);

  return { behaviorAnalytics, behaviorDistribution };
}


/**
 * Group anomalous rows into episodes: consecutive flagged rows of the same
 * session, split when there is a gap of more than EPISODE_GAP_SECONDS.
 * Newest episode first.
 */
export function groupAnomalyEpisodes(rows) {
  const flagged = rows.filter((row) => row.is_anomaly);
  const episodes = [];
  let current = null;

  for (const row of flagged) {
    const continues = current
      && current.session_id === row.session_id
      && row.ts - current.endMs <= EPISODE_GAP_SECONDS * 1000;

    if (!continues) {
      current = {
        session_id: row.session_id,
        startMs: row.ts,
        endMs: row.ts,
        duration: 0,
        maxScore: row.anomaly_score,
        behavior: row.behavior_label,
        apps: new Map(),
        cpu: 0,
        memory: 0,
        idle: 0,
        active: 0,
        rows: [],
      };
      episodes.push(current);
    }

    current.endMs = row.ts;
    current.duration += row.duration_seconds;
    current.maxScore = Math.max(current.maxScore, row.anomaly_score);
    current.behavior = row.behavior_label;
    // Rank by time, then by sample count when durations are zero.
    current.apps.set(row.application, (current.apps.get(row.application) || 0) + row.duration_seconds + 0.001);
    current.cpu += row.cpu_percent;
    current.memory += row.memory_percent;
    current.idle += row.idle_duration_seconds;
    current.active += row.active_duration_seconds;
    current.rows.push(row);
  }

  return episodes
    .map((episode) => {
      const tracked = episode.active + episode.idle;

      return {
        id: `${episode.session_id}-${episode.startMs}`,
        session_id: episode.session_id,
        start: new Date(episode.startMs),
        end: new Date(episode.endMs),
        startMs: episode.startMs,
        endMs: episode.endMs,
        duration: round(Math.max(episode.duration, (episode.endMs - episode.startMs) / 1000)),
        records: episode.rows.length,
        maxScore: round(episode.maxScore, 3),
        behavior: episode.behavior,
        topApps: [...episode.apps.entries()]
          .sort((a, b) => b[1] - a[1])
          .slice(0, 3)
          .map(([name]) => name),
        avgCPU: round(episode.cpu / episode.rows.length, 1),
        avgMemory: round(episode.memory / episode.rows.length, 1),
        idleRatio: tracked > 0 ? round((episode.idle / tracked) * 100, 1) : 0,
        rows: episode.rows,
      };
    })
    .sort((a, b) => b.endMs - a.endMs);
}


/**
 * Rows whose application or window title contains the query, summarised by
 * application and by window title.
 */
export function searchActivity(rows, query) {
  const needle = String(query || "").trim().toLowerCase();

  if (!needle) {
    return { rows, applications: aggregateApplications(rows), windows: [] };
  }

  const matches = rows.filter((row) =>
    row.application.toLowerCase().includes(needle)
    || row.window_title.toLowerCase().includes(needle)
  );

  const windowMap = new Map();

  for (const row of matches) {
    const key = `${row.application}\u0000${row.window_title}`;
    let item = windowMap.get(key);

    if (!item) {
      item = { key, application: row.application, window_title: row.window_title || "(no title)", duration: 0, records: 0, lastSeen: 0 };
      windowMap.set(key, item);
    }

    item.duration += row.active_duration_seconds;
    item.records += 1;
    item.lastSeen = Math.max(item.lastSeen, row.ts);
  }

  const windows = [...windowMap.values()]
    .sort((a, b) => b.duration - a.duration || b.records - a.records)
    .slice(0, 100);

  return { rows: matches, applications: aggregateApplications(matches), windows };
}


// --------------------------------------------------
// The single aggregation entry point
// --------------------------------------------------

/**
 * Pure: compute every number the single-user pages show from parsed rows.
 * Ratios are returned as 0-100 percentages.
 */
export function computeAnalytics(allRows, range = "all", { now = Date.now() } = {}) {
  const rows = filterRowsByRange(allRows, range, now);

  const totalActiveSeconds = sum(rows, "active_duration_seconds");
  const totalIdleSeconds = sum(rows, "idle_duration_seconds");
  const trackedSeconds = totalActiveSeconds + totalIdleSeconds;

  const sessionAnalytics = buildSessions(rows);
  const { behaviorAnalytics, behaviorDistribution } = buildBehaviors(rows, sessionAnalytics);
  const hourly = buildHourly(rows, range, now);
  const anomalyEpisodes = groupAnomalyEpisodes(rows);
  const applicationUsage = aggregateApplications(rows);

  const batteryRows = rows.filter(hasBatteryReading);

  const sessionSeconds = sessionAnalytics.reduce((total, session) => total + session.duration, 0);
  const avgSwitchFrequency = sessionSeconds > 0
    ? sessionAnalytics.reduce((total, session) => total + session.switchFrequency * session.duration, 0) / sessionSeconds
    : average(sessionAnalytics.map((session) => session.switchFrequency)) || 0;

  const clusterCount = new Set(
    sessionAnalytics
      .map((session) => session.cluster)
      .filter((cluster) => cluster !== null && cluster >= 0)
  ).size;

  const focusScore = trackedSeconds > 0 ? (totalActiveSeconds / trackedSeconds) * 100 : 0;

  return {
    range,
    rangeLabel: rangeLabel(range),
    // Epoch ms where the range begins (null for "all"), with the same `now`.
    rangeStart: rangeStart(range, now),
    rows,
    totalRowsAll: allRows.length,

    metrics: {
      totalRecords: rows.length,
      totalSessions: sessionAnalytics.length,
      totalActiveSeconds,
      totalIdleSeconds,
      focusScore: round(focusScore, 1),
      avgIdleRatio: trackedSeconds > 0 ? round((totalIdleSeconds / trackedSeconds) * 100, 1) : 0,
      anomalyCount: anomalyEpisodes.length,
      anomalousRecords: rows.filter((row) => row.is_anomaly).length,
      applicationSwitches: rows.filter((row) => row.application_switch).length,
      windowSwitches: rows.filter((row) => row.window_switch).length,
      avgSwitchFrequency: round(avgSwitchFrequency),
      clusterCount,
      avgCPU: rows.length ? round(sum(rows, "cpu_percent") / rows.length, 1) : 0,
      avgMemory: rows.length ? round(sum(rows, "memory_percent") / rows.length, 1) : 0,
      hasBattery: batteryRows.length > 0,
      avgBattery: batteryRows.length ? round(sum(batteryRows, "battery_percent") / batteryRows.length, 1) : null,
      minBattery: batteryRows.length
        ? batteryRows.reduce((low, row) => Math.min(low, row.battery_percent), 100)
        : null,
      dataStart: rows.length ? new Date(rows[0].ts) : null,
      dataEnd: rows.length ? new Date(rows[rows.length - 1].ts) : null,
    },

    applicationUsage,
    behaviorDistribution,
    behaviorAnalytics,
    sessionAnalytics,
    anomalyEpisodes,
    ...hourly,
  };
}
