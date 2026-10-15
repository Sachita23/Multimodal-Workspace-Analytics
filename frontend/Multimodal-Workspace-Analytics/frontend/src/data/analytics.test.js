// Unit tests for the single-user aggregation. Run with `npm test`
// (Node's built-in test runner; no extra dependencies).

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  autoRange,
  computeAnalytics,
  fetchAnalyticsRows,
  fetchStatus,
  groupAnomalyEpisodes,
  parseAnalyticsCsv,
  parseTimestamp,
  rangeCovering,
  rangeStart,
  searchActivity,
} from "./analytics.js";


const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});


const HEADER = [
  "timestamp", "application", "window_title", "activity_status", "idle_seconds",
  "cpu_percent", "memory_percent", "battery_percent", "charging", "duration_seconds",
  "application_switch", "window_switch", "active_duration_seconds", "idle_duration_seconds",
  "session_id", "idle_ratio", "switch_frequency", "unique_app_count", "hour",
  "cluster_id", "behavior_label", "anomaly_score", "is_anomaly",
];

const pad = (value) => String(value).padStart(2, "0");

function stamp(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 10:00-12:59 local on a fixed day, one sample every 5 s, with a 20-minute
// idle break at 11:00 that starts session 1. Session 1 is flagged anomalous
// between 11:30 and 11:40. "now" is 13:00:30, so 13:00 is the partial hour.
const DAY = new Date(2026, 9, 14).getTime();
const NOW = DAY + 13 * 3600e3 + 30e3;

function buildCsv({ trailingNewline = true, extraLine = "" } = {}) {
  const lines = [HEADER.join(",")];
  let previousApp = null;

  for (let t = DAY + 10 * 3600e3; t < DAY + 13 * 3600e3; t += 5000) {
    const minute = (t - DAY) / 60e3;
    const idle = minute >= 660 && minute < 680;
    const session = minute >= 680 ? 1 : 0;
    const app = idle ? "Finder" : Math.floor(minute / 10) % 2 ? "Code" : "Google Chrome";
    const anomaly = minute >= 690 && minute < 700;

    lines.push([
      stamp(t), app, `${app} window ${Math.floor(minute / 30)}`, idle ? "Idle" : "Active",
      idle ? 60 : 0, 20, 50, minute < 700 ? 90 : 70, "False", 5,
      previousApp !== null && previousApp !== app ? "True" : "False", "False",
      idle ? 0 : 5, idle ? 5 : 0,
      session, 0.1, 0.2, 2, new Date(t).getHours(),
      session ? 1 : 0, session ? "High-switching" : "Stable-activity",
      anomaly ? 0.9 : 0.1, anomaly ? "True" : "False",
    ].join(","));

    previousApp = app;
  }

  const text = lines.join("\n") + (extraLine ? `\n${extraLine}` : "");

  return trailingNewline ? `${text}\n` : text;
}


test("parseTimestamp reads local wall-clock time", () => {
  assert.equal(parseTimestamp("2026-10-14 13:00:30"), NOW);
  assert.ok(Number.isNaN(parseTimestamp("")));
});


test("torn rows are dropped instead of becoming 'Unknown' data", () => {
  const torn = buildCsv({ trailingNewline: false, extraLine: "2026-10-14 12:59:59,Code,Code" });
  const parsed = parseAnalyticsCsv(torn);
  const clean = parseAnalyticsCsv(buildCsv());

  assert.equal(parsed.rows.length, clean.rows.length);
  assert.ok(parsed.dropped >= 1);
  assert.ok(parsed.rows.every((row) => row.session_id !== "" && Number.isFinite(row.ts)));
});


test("a record Papa flags is dropped; the clean record after it is kept", () => {
  const lines = buildCsv().split("\n");
  const cells = lines[6].split(",");
  // Broken quoting that still yields the right number of values.
  cells[2] = '"x,"a,b"';
  lines[6] = cells.join(",");
  // Empty lines (which Papa counts in its error positions) before it.
  lines.splice(3, 0, "", "");

  const parsed = parseAnalyticsCsv(lines.join("\n"));
  const clean = parseAnalyticsCsv(buildCsv());
  const garbled = clean.rows[5];

  assert.equal(parsed.total, clean.total);
  assert.equal(parsed.dropped, 1);
  assert.deepEqual(
    parsed.rows.map((row) => row.timestamp),
    clean.rows.filter((row) => row !== garbled).map((row) => row.timestamp)
  );
  assert.ok(parsed.rows.every((row) => !row.window_title.includes('"')));
});


test("24h range gives zero-filled clock hours, never more than 60 min each", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  const data = computeAnalytics(rows, "24h", { now: NOW });

  assert.equal(data.hourlyMode, "timeline");
  // 13:00:30 yesterday to 13:00:30 today touches 25 clock hours.
  assert.equal(data.hourlyActivity.length, 25);
  assert.ok(data.hourlyActivity.every((bucket) => bucket.active + bucket.idle <= 60));

  const hourTotal = data.hourlyActivity.reduce((sum, bucket) => sum + bucket.active, 0) * 60;
  assert.ok(Math.abs(hourTotal - data.metrics.totalActiveSeconds) < 60);

  // The first (13:00 yesterday, from 13:00:30) and the current (13:00) hour
  // are partial and are never the peak or quiet window.
  assert.equal(data.hourlyActivity[0].label, "13:00");
  assert.equal(data.hourlyActivity[0].partial, true);
  assert.equal(data.hourlyActivity.at(-1).label, "13:00");
  assert.equal(data.hourlyActivity.at(-1).partial, true);
  assert.deepEqual(data.hourlyActivity.filter((bucket) => bucket.partial).length, 2);
  assert.notEqual(data.quietHour?.label, "13:00");
  assert.ok(["10:00", "11:00", "12:00"].includes(data.peakHour.label));
});


test("'Last 24 hours' is the full 24 hours, not just the clock hours since 23 h ago", () => {
  // At 20:05, a 25-minute session that ran 23 h 55 min to 23 h 30 min ago
  // (20:10-20:35 yesterday) belongs to the last 24 hours.
  const now = new Date(2026, 9, 15, 20, 5, 0).getTime();
  const lines = [HEADER.join(",")];

  for (let t = now - 23.9 * 3600e3; t < now - 23.5 * 3600e3; t += 5000) {
    lines.push([
      stamp(t), "Code", "x", "Active", 0, 20, 50, 90, "False", 5, "False", "False", 5, 0,
      7, 0, 0, 1, new Date(t).getHours(), 0, "Stable-activity", 0.1, "False",
    ].join(","));
  }

  const { rows } = parseAnalyticsCsv(lines.join("\n") + "\n");

  assert.equal(rangeStart("24h", now), now - 24 * 3600e3);
  assert.equal(autoRange(rows, now), "24h");

  const data = computeAnalytics(rows, "24h", { now });

  assert.equal(data.metrics.totalRecords, rows.length);
  assert.equal(data.metrics.totalSessions, 1);
  assert.equal(data.metrics.totalActiveSeconds, rows.length * 5);

  // Those minutes sit in the leading, partial 20:00 bucket of yesterday.
  const first = data.hourlyActivity[0];
  assert.equal(first.label, "20:00");
  assert.equal(first.partial, true);
  assert.ok(Math.abs(first.active * 60 - rows.length * 5) < 60);
  assert.equal(data.hourlyActivity.length, 25);

  // A sample from just over 24 h ago is outside the range.
  const old = { ...rows[0], ts: now - 24 * 3600e3 - 1000 };
  assert.equal(autoRange([old], now), "all");
});


test("on the hour, the 24h timeline is 24 whole hours plus the empty current one", () => {
  const now = new Date(2026, 9, 15, 20, 0, 0).getTime();
  const data = computeAnalytics([], "24h", { now });

  assert.equal(data.hourlyActivity.length, 25);
  assert.equal(data.hourlyActivity[0].partial, false);
  assert.equal(data.hourlyActivity.at(-1).partial, true);
});


test("rangeCovering widens the range just enough to show a moment in full", () => {
  const now = new Date(2026, 9, 15, 20, 0, 0).getTime();
  const hoursAgo = (hours) => now - hours * 3600e3;

  assert.equal(rangeCovering(hoursAgo(2), "24h", now), "24h");
  assert.equal(rangeCovering(hoursAgo(72), "24h", now), "7d");
  assert.equal(rangeCovering(hoursAgo(10 * 24), "24h", now), "30d");
  assert.equal(rangeCovering(hoursAgo(40 * 24), "24h", now), "all");
  // Never narrower than the range already selected.
  assert.equal(rangeCovering(hoursAgo(2), "30d", now), "30d");
  assert.equal(rangeCovering(hoursAgo(2), "all", now), "all");
});


test("computeAnalytics reports where its range starts", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());

  assert.equal(computeAnalytics(rows, "24h", { now: NOW }).rangeStart, NOW - 24 * 3600e3);
  assert.equal(computeAnalytics(rows, "7d", { now: NOW }).rangeStart, NOW - 7 * 24 * 3600e3);
  assert.equal(computeAnalytics(rows, "all", { now: NOW }).rangeStart, null);
});


test("multi-day ranges average the hour-of-day profile per day with data", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  const later = rows.map((row) => ({ ...row, ts: row.ts + 24 * 3600e3 }));
  const data = computeAnalytics([...rows, ...later], "all", { now: NOW + 24 * 3600e3 });
  const byLabel = Object.fromEntries(data.hourlyActivity.map((bucket) => [bucket.label, bucket]));

  assert.equal(data.hourlyMode, "profile");
  assert.equal(data.hourlyDays, 2);
  assert.ok(data.hourlyActivity.every((bucket) => bucket.active + bucket.idle <= 60));

  // Each day: 10:00 and 12:00 fully active, 11:00 has a 20-minute idle break.
  assert.equal(byLabel["10:00"].active, 60);
  assert.equal(byLabel["11:00"].active, 40);
  assert.equal(byLabel["11:00"].idle, 20);
  assert.equal(byLabel["12:00"].active, 60);
});


// One sample every 5 s, each 5 s active, from `start` to `end` (inclusive).
function continuousRows(start, end, { active = () => true } = {}) {
  const rows = [];

  for (let t = start; t <= end; t += 5000) {
    const isActive = active(t);

    rows.push({
      timestamp: stamp(t), ts: t, application: "Code", window_title: "", activity_status: isActive ? "Active" : "Idle",
      idle_seconds: 0, cpu_percent: 10, memory_percent: 50, battery_percent: 100, charging: false,
      duration_seconds: 5, active_duration_seconds: isActive ? 5 : 0, idle_duration_seconds: isActive ? 0 : 5,
      session_id: "0", application_switch: false, window_switch: false, idle_ratio: 0, switch_frequency: 0,
      unique_app_count: 1, hour: new Date(t).getHours(), cluster_id: 0, behavior_label: "Stable-activity",
      anomaly_score: 0, is_anomaly: false,
    });
  }

  return rows;
}


test("a 7-day profile of continuous activity reads 60 min for every hour", () => {
  // "now" is not midnight-aligned, so the 7-day window touches 8 dates but
  // each clock hour only 7 times; counting dates under-reported every hour.
  const now = DAY + 14 * 3600e3 + 10 * 60e3;
  const rows = continuousRows(now - 10 * 24 * 3600e3, now);

  for (const range of ["7d", "30d"]) {
    const data = computeAnalytics(rows, range, { now });

    assert.equal(data.hourlyMode, "profile");
    assert.equal(data.hourlyDays, range === "7d" ? 7 : 10);

    for (const bucket of data.hourlyActivity) {
      assert.equal(bucket.active, 60, `${range} ${bucket.label}`);
    }
  }
});


test("partial first and last days are not counted as whole days", () => {
  // Data from 10:35 one day to 13:47 the next (the owner's real span). Hours
  // 00-09 and 14-23 were seen once, not twice, and must not be halved.
  const start = DAY + 10 * 3600e3 + 35 * 60e3;
  const end = DAY + 24 * 3600e3 + 13 * 3600e3 + 47 * 60e3;
  const data = computeAnalytics(continuousRows(start, end), "all", { now: end + 60e3 });

  assert.equal(data.hourlyDays, 2);

  for (const bucket of data.hourlyActivity) {
    assert.equal(bucket.active, 60, bucket.label);
  }
});


test("an hour seen only once shows that hour's real minutes", () => {
  // 15:00 has data on the first day only: 41 of its 60 minutes active.
  const start = DAY + 10 * 3600e3 + 35 * 60e3;
  const end = DAY + 24 * 3600e3 + 13 * 3600e3 + 47 * 60e3;
  const rows = continuousRows(start, end, {
    active: (t) => new Date(t).getHours() !== 15 || new Date(t).getMinutes() < 41,
  });

  const profile = computeAnalytics(rows, "all", { now: end + 60e3 });
  const fifteen = profile.hourlyActivity.find((bucket) => bucket.label === "15:00");

  assert.equal(fifteen.active, 41);
  assert.equal(fifteen.idle, 19);
});


test("dates without any samples are not days with data", () => {
  // 09:00-17:00 on Monday and Wednesday, nothing on Tuesday.
  const monday = DAY;
  const wednesday = DAY + 2 * 24 * 3600e3;
  const rows = [
    ...continuousRows(monday + 9 * 3600e3, monday + 17 * 3600e3 - 5000),
    ...continuousRows(wednesday + 9 * 3600e3, wednesday + 17 * 3600e3 - 5000),
  ];
  const data = computeAnalytics(rows, "all", { now: wednesday + 18 * 3600e3 });
  const byLabel = Object.fromEntries(data.hourlyActivity.map((bucket) => [bucket.label, bucket]));

  assert.equal(data.hourlyDays, 2);
  assert.equal(byLabel["09:00"].active, 60);
  assert.equal(byLabel["16:00"].active, 60);
  assert.equal(byLabel["20:00"].active, 0);
});


test("profile peak and quiet windows skip hours seen for less than a full hour", () => {
  // 10:35-13:47 on one day: 10:00 and 13:00 are only partly observed. 13:00
  // is fully active for its 47 minutes, the whole hours only half.
  const start = DAY + 10 * 3600e3 + 35 * 60e3;
  const end = DAY + 13 * 3600e3 + 47 * 60e3;
  const rows = continuousRows(start, end, {
    active: (t) => {
      const hour = new Date(t).getHours();
      return hour === 10 || hour === 13 || new Date(t).getMinutes() < 30;
    },
  });

  const data = computeAnalytics(rows, "all", { now: end + 60e3 });
  const partial = data.hourlyActivity.filter((bucket) => bucket.partial).map((bucket) => bucket.label);

  assert.deepEqual(partial.filter((label) => ["10:00", "11:00", "12:00", "13:00"].includes(label)), ["10:00", "13:00"]);
  assert.ok(["11:00", "12:00"].includes(data.peakHour.label));
  assert.ok(["11:00", "12:00"].includes(data.quietHour.label));
});


test("sessions are newest first with start/end and a weighted idle ratio", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  const data = computeAnalytics(rows, "all", { now: NOW });

  assert.deepEqual(data.sessionAnalytics.map((s) => s.session_id), ["1", "0"]);
  assert.equal(data.sessionAnalytics[0].isLatest, true);
  assert.equal(data.sessionAnalytics[1].isLatest, false);
  assert.ok(data.sessionAnalytics[0].start < data.sessionAnalytics[0].end);

  const { totalActiveSeconds: active, totalIdleSeconds: idle } = data.metrics;
  assert.equal(data.metrics.avgIdleRatio, Math.round((idle / (active + idle)) * 1000) / 10);
});


test("behaviour ratios are 0-100 percentages and clusters exclude -1", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  const unlabelled = rows.map((row, index) => (index < 10 ? { ...row, cluster_id: -1 } : row));
  const data = computeAnalytics(unlabelled, "all", { now: NOW });

  const stable = data.behaviorAnalytics.find((item) => item.behavior === "Stable-activity");
  assert.ok(stable.avgIdleRatio > 1 && stable.avgIdleRatio < 100);
  assert.equal(data.metrics.clusterCount, 2);
});


test("anomalous rows are grouped into episodes", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  const episodes = groupAnomalyEpisodes(rows);
  const data = computeAnalytics(rows, "all", { now: NOW });

  assert.equal(episodes.length, 1);
  assert.equal(episodes[0].records, 120);
  assert.equal(data.metrics.anomalyCount, 1);
  assert.equal(data.metrics.anomalousRecords, 120);
  assert.deepEqual(episodes[0].topApps, ["Code"]);
});


test("a session flagged as a whole (what the pipeline emits) is one episode", () => {
  // ml/ flags every row of an anomalous session, never a slice of one.
  const { rows } = parseAnalyticsCsv(buildCsv());
  const flagged = rows.map((row) => ({
    ...row,
    is_anomaly: row.session_id === "0",
    anomaly_score: row.session_id === "0" ? 0.8 : 0,
  }));
  const sessionRows = flagged.filter((row) => row.session_id === "0");

  const episodes = groupAnomalyEpisodes(flagged);

  assert.equal(episodes.length, 1);
  assert.equal(episodes[0].session_id, "0");
  assert.equal(episodes[0].records, sessionRows.length);
  assert.equal(episodes[0].startMs, sessionRows[0].ts);
  assert.equal(episodes[0].endMs, sessionRows.at(-1).ts);

  // Two back-to-back flagged sessions stay two episodes.
  const both = rows.map((row) => ({ ...row, is_anomaly: true, anomaly_score: 0.8 }));
  const twoEpisodes = groupAnomalyEpisodes(both);

  assert.deepEqual(twoEpisodes.map((episode) => episode.session_id), ["1", "0"]);
  assert.equal(computeAnalytics(both, "all", { now: NOW }).metrics.anomalyCount, 2);
});


test("battery minimum comes from rows; a desktop without battery hides it", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());
  assert.equal(computeAnalytics(rows, "all", { now: NOW }).metrics.minBattery, 70);

  const desktop = rows.map((row) => ({ ...row, battery_percent: 100, charging: false }));
  const metrics = computeAnalytics(desktop, "all", { now: NOW }).metrics;
  assert.equal(metrics.hasBattery, false);
  assert.equal(metrics.minBattery, null);
});


test("range filtering and the automatic default", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());

  assert.equal(autoRange(rows, NOW), "24h");
  assert.equal(autoRange(rows, NOW + 3 * 24 * 3600e3), "all");
  assert.equal(computeAnalytics(rows, "24h", { now: NOW + 3 * 24 * 3600e3 }).metrics.totalRecords, 0);
  assert.equal(computeAnalytics(rows, "7d", { now: NOW + 3 * 24 * 3600e3 }).metrics.totalRecords, rows.length);
});


test("search matches application names and window titles", () => {
  const { rows } = parseAnalyticsCsv(buildCsv());

  const byApp = searchActivity(rows, "chrome");
  assert.deepEqual(byApp.applications.map((app) => app.application), ["Google Chrome"]);

  const byWindow = searchActivity(rows, "window 21");
  assert.ok(byWindow.windows.length > 0);
  assert.ok(byWindow.windows.every((item) => item.window_title.includes("window 21")));
});


// --------------------------------------------------
// fetchAnalyticsRows / fetchStatus with a stubbed fetch
// --------------------------------------------------

function stubFetch(handler) {
  const calls = [];

  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, ifNoneMatch: init.headers?.["If-None-Match"] ?? null, signal: init.signal });
    return handler(url, init, calls.length);
  };

  return calls;
}


function neverAnswers(url, init) {
  return new Promise((_, reject) => {
    init.signal?.addEventListener("abort", () => {
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    });
  });
}


test("fetchAnalyticsRows: 200 with an ETag, then a 304 keeps the old snapshot", async () => {
  const csv = buildCsv();
  const calls = stubFetch((url, init) => (
    init.headers?.["If-None-Match"] === '"v1"'
      ? new Response(null, { status: 304 })
      : new Response(csv, { status: 200, headers: { ETag: '"v1"' } })
  ));

  const first = await fetchAnalyticsRows({ timeoutMs: 500 });

  assert.equal(first.notModified, false);
  assert.equal(first.etag, '"v1"');
  assert.equal(first.rows.length, parseAnalyticsCsv(csv).rows.length);
  assert.equal(calls[0].url, "/api/analytics.csv");
  assert.equal(calls[0].ifNoneMatch, null);

  const second = await fetchAnalyticsRows({ etag: first.etag, timeoutMs: 500 });

  assert.deepEqual(second, { notModified: true, etag: '"v1"' });
  assert.equal(calls[1].ifNoneMatch, '"v1"');
});


test("fetchAnalyticsRows hands the body to the given parser (the dashboard's worker)", async () => {
  const csv = buildCsv();
  stubFetch(() => new Response(csv, { status: 200, headers: { ETag: '"v1"' } }));

  const seen = [];
  const parse = async (text) => {
    seen.push(text.length);
    return parseAnalyticsCsv(text);
  };

  const result = await fetchAnalyticsRows({ timeoutMs: 500, parse });

  assert.deepEqual(seen, [csv.length]);
  assert.equal(result.rows.length, parseAnalyticsCsv(csv).rows.length);

  // A parse that finishes after the poll was cancelled is not delivered.
  const controller = new AbortController();
  const late = async (text) => {
    controller.abort();
    return parseAnalyticsCsv(text);
  };

  await assert.rejects(
    fetchAnalyticsRows({ timeoutMs: 500, parse: late, signal: controller.signal }),
    (error) => error.name === "AbortError"
  );
});


test("fetchAnalyticsRows: each failure has a code and a message for the user", async () => {
  stubFetch(() => new Response(JSON.stringify({ error: "Processed analytics data is not available yet." }), { status: 404 }));
  await assert.rejects(fetchAnalyticsRows({ timeoutMs: 500 }), (error) => (
    error.code === "no-data" && /No processed data yet/.test(error.message)
  ));

  stubFetch(() => new Response("boom", { status: 502 }));
  await assert.rejects(fetchAnalyticsRows({ timeoutMs: 500 }), (error) => error.code === "server");

  stubFetch(() => Promise.reject(new TypeError("fetch failed")));
  await assert.rejects(fetchAnalyticsRows({ timeoutMs: 500 }), (error) => (
    error.code === "unreachable" && /start_project\.py/.test(error.message)
  ));

  // A torn snapshot (many rows cut off) is rejected so the old one is kept.
  const lines = buildCsv().split("\n");
  const torn = lines.map((line, index) => (index > 0 && index % 3 === 0 ? line.split(",").slice(0, 4).join(",") : line)).join("\n");
  stubFetch(() => new Response(torn, { status: 200, headers: { ETag: '"v2"' } }));
  await assert.rejects(fetchAnalyticsRows({ timeoutMs: 500 }), (error) => error.code === "partial");
});


test("fetchAnalyticsRows: a server that never answers times out", async () => {
  stubFetch(neverAnswers);

  const started = Date.now();
  await assert.rejects(fetchAnalyticsRows({ timeoutMs: 50 }), (error) => error.code === "timeout");
  assert.ok(Date.now() - started < 1000);
});


test("fetchStatus: parsed JSON, or null when missing, unreachable or silent", async () => {
  const status = { status: "ok", collector_stale: false, pipeline: null };

  stubFetch(() => new Response(JSON.stringify(status), { status: 200 }));
  assert.deepEqual(await fetchStatus({ timeoutMs: 500 }), status);

  stubFetch(() => new Response("{}", { status: 404 }));
  assert.equal(await fetchStatus({ timeoutMs: 500 }), null);

  stubFetch(() => Promise.reject(new TypeError("fetch failed")));
  assert.equal(await fetchStatus({ timeoutMs: 500 }), null);

  stubFetch(neverAnswers);
  assert.equal(await fetchStatus({ timeoutMs: 50 }), null);
});

