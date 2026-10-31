// --------------------------------------------------
// Moving parsed analytics rows from the parser worker to the page
// --------------------------------------------------
//
// postMessage() copies plain row objects one key at a time: for a month of
// history (about 200k rows) the page would spend more time unpacking the
// copy than the worker saved by parsing. The worker therefore sends columns
// (typed arrays, transferred without copying, plus small string tables), and
// the page rebuilds the row objects in one tight loop.
//
// The column layout must match normalizeRow() in data/analytics.js. If the
// row shape ever changes without this file, packRows() falls back to sending
// the objects as they are (slower, never wrong).

import { parseAnalyticsCsv } from "../data/analytics.js";


const STRING_FIELDS = ["application", "window_title", "activity_status", "session_id", "behavior_label"];

const NUMBER_FIELDS = [
  "ts", "idle_seconds", "cpu_percent", "memory_percent", "battery_percent",
  "duration_seconds", "active_duration_seconds", "idle_duration_seconds",
  "idle_ratio", "switch_frequency", "unique_app_count", "hour", "cluster_id", "anomaly_score",
];

const FLAG_FIELDS = ["charging", "application_switch", "window_switch", "is_anomaly"];

// Every key normalizeRow() produces ("timestamp" travels as a plain array).
const ROW_KEYS = new Set(["timestamp", ...STRING_FIELDS, ...NUMBER_FIELDS, ...FLAG_FIELDS]);


function hasKnownShape(row) {
  const keys = Object.keys(row);
  return keys.length === ROW_KEYS.size && keys.every((key) => ROW_KEYS.has(key));
}


// Repeated values (application names, session ids, labels) as a table of
// distinct strings plus one index per row.
function encodeStrings(rows, field) {
  const table = [];
  const ids = new Map();
  const index = new Uint32Array(rows.length);

  for (let i = 0; i < rows.length; i += 1) {
    const value = rows[i][field];
    let id = ids.get(value);

    if (id === undefined) {
      id = table.length;
      table.push(value);
      ids.set(value, id);
    }

    index[i] = id;
  }

  return { table, index };
}


/**
 * Rows (as parseAnalyticsCsv returns them) -> a compact, transferable form.
 */
export function packRows(rows) {
  if (rows.length && !hasKnownShape(rows[0])) {
    return { format: "rows", rows };
  }

  const length = rows.length;
  const strings = {};
  const numbers = {};
  const flags = new Uint8Array(length);

  for (const field of STRING_FIELDS) strings[field] = encodeStrings(rows, field);

  for (const field of NUMBER_FIELDS) {
    const column = new Float64Array(length);

    // cluster_id is the only nullable number; null travels as NaN.
    for (let i = 0; i < length; i += 1) column[i] = rows[i][field] ?? Number.NaN;

    numbers[field] = column;
  }

  for (let i = 0; i < length; i += 1) {
    const row = rows[i];
    flags[i] = (row.charging ? 1 : 0)
      | (row.application_switch ? 2 : 0)
      | (row.window_switch ? 4 : 0)
      | (row.is_anomaly ? 8 : 0);
  }

  return {
    format: "columns",
    length,
    timestamps: rows.map((row) => row.timestamp),
    strings,
    numbers,
    flags,
  };
}


/**
 * The ArrayBuffers in a packed payload, for postMessage's transfer list.
 */
export function transferablesOf(packed) {
  if (packed.format !== "columns") return [];

  return [
    packed.flags.buffer,
    ...Object.values(packed.numbers).map((column) => column.buffer),
    ...Object.values(packed.strings).map((column) => column.index.buffer),
  ];
}


/**
 * The inverse of packRows(). Rows get the same keys, in the same order, as
 * normalizeRow() gives them.
 */
export function unpackRows(packed) {
  if (packed.format === "rows") return packed.rows;

  const { length, timestamps, strings: s, numbers: n, flags } = packed;
  const rows = new Array(length);

  const apps = s.application;
  const titles = s.window_title;
  const statuses = s.activity_status;
  const sessions = s.session_id;
  const labels = s.behavior_label;

  for (let i = 0; i < length; i += 1) {
    const flag = flags[i];
    const cluster = n.cluster_id[i];

    rows[i] = {
      timestamp: timestamps[i],
      ts: n.ts[i],
      application: apps.table[apps.index[i]],
      window_title: titles.table[titles.index[i]],
      activity_status: statuses.table[statuses.index[i]],
      idle_seconds: n.idle_seconds[i],
      cpu_percent: n.cpu_percent[i],
      memory_percent: n.memory_percent[i],
      battery_percent: n.battery_percent[i],
      charging: (flag & 1) !== 0,
      duration_seconds: n.duration_seconds[i],
      active_duration_seconds: n.active_duration_seconds[i],
      idle_duration_seconds: n.idle_duration_seconds[i],
      session_id: sessions.table[sessions.index[i]],
      application_switch: (flag & 2) !== 0,
      window_switch: (flag & 4) !== 0,
      idle_ratio: n.idle_ratio[i],
      switch_frequency: n.switch_frequency[i],
      unique_app_count: n.unique_app_count[i],
      hour: n.hour[i],
      cluster_id: Number.isNaN(cluster) ? null : cluster,
      behavior_label: labels.table[labels.index[i]],
      anomaly_score: n.anomaly_score[i],
      is_anomaly: (flag & 8) !== 0,
    };
  }

  return rows;
}


/**
 * The worker's side: a {id, text} request -> {message, transfer} for
 * postMessage. Errors (such as a file without a header) travel as text.
 */
export function handleParseRequest({ id, text }) {
  try {
    const { rows, dropped, total } = parseAnalyticsCsv(text);
    const packed = packRows(rows);

    return { message: { id, packed, dropped, total }, transfer: transferablesOf(packed) };
  } catch (error) {
    return { message: { id, error: error?.message || String(error) }, transfer: [] };
  }
}
