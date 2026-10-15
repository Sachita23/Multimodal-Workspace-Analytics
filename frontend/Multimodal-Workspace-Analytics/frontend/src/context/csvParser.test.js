// Unit tests for parsing the analytics CSV off the main thread.
// Run with `npm test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseAnalyticsCsv } from "../data/analytics.js";
import { createCsvParser } from "./csvParser.js";
import { handleParseRequest, packRows, transferablesOf, unpackRows } from "./csvTransport.js";


const HEADER = [
  "timestamp", "application", "window_title", "activity_status", "idle_seconds",
  "cpu_percent", "memory_percent", "battery_percent", "charging", "duration_seconds",
  "application_switch", "window_switch", "active_duration_seconds", "idle_duration_seconds",
  "session_id", "idle_ratio", "switch_frequency", "unique_app_count", "hour",
  "cluster_id", "behavior_label", "anomaly_score", "is_anomaly",
];

const APPS = ["Code", "Google Chrome", "Slack", "Terminal", "Finder"];

const pad = (value) => String(value).padStart(2, "0");

function stamp(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}


// `count` samples at the collector's 5 s cadence, with quoted window titles
// (commas inside), unclustered rows (empty cluster_id) and flagged rows.
function buildCsv(count) {
  const start = new Date(2026, 8, 1, 9, 0, 0).getTime();
  const lines = [HEADER.join(",")];

  for (let i = 0; i < count; i += 1) {
    const app = APPS[i % APPS.length];
    const idle = i % 7 === 0;

    lines.push([
      stamp(start + i * 5000), app, `"${app} — file ${i % 40}.js, edited"`, idle ? "Idle" : "Active",
      idle ? 75.5 : 0.4, (i % 90) + 0.5, 61.2, 80 - (i % 50), i % 3 === 0 ? "True" : "False", 5,
      i % 4 === 0 ? "True" : "False", i % 5 === 0 ? "True" : "False", idle ? 0 : 5, idle ? 5 : 0,
      Math.floor(i / 720), 0.25, 1.5, 3, 9, i % 11 === 0 ? "" : i % 4, i % 11 === 0 ? "Not-enough-history" : "Stable-activity",
      (i % 100) / 100, i % 97 === 0 ? "True" : "False",
    ].join(","));
  }

  return `${lines.join("\n")}\n`;
}


// Runs the real worker code (handleParseRequest), with postMessage's copy
// and transfer simulated by structuredClone, on a later tick.
function fakeWorkerFactory(log = []) {
  return () => {
    const worker = {
      onmessage: null,
      onerror: null,
      terminated: false,
      postMessage(request) {
        log.push(request.id);

        setTimeout(() => {
          if (worker.terminated) return;

          const { message, transfer } = handleParseRequest(structuredClone(request));
          worker.onmessage?.({ data: structuredClone(message, { transfer }) });
        }, 0);
      },
      terminate() {
        worker.terminated = true;
      },
    };

    return worker;
  };
}


test("rows survive packing for the worker transfer unchanged", () => {
  const { rows } = parseAnalyticsCsv(buildCsv(500));
  const packed = packRows(rows);

  assert.equal(packed.format, "columns");
  assert.ok(transferablesOf(packed).length > 0);

  const copied = structuredClone(packed, { transfer: transferablesOf(packed) });
  const unpacked = unpackRows(copied);

  assert.deepEqual(unpacked, rows);
  // Same key order as normalizeRow(), so the objects share one shape.
  assert.deepEqual(Object.keys(unpacked[0]), Object.keys(rows[0]));
  assert.ok(unpacked.some((row) => row.cluster_id === null));
  assert.ok(unpacked.some((row) => row.window_title.includes(",")));
});


test("rows of an unknown shape are sent as they are, never altered", () => {
  const rows = [{ timestamp: "2026-09-01 09:00:00", ts: 1, extra: "field" }];
  const packed = packRows(rows);

  assert.equal(packed.format, "rows");
  assert.deepEqual(unpackRows(structuredClone(packed)), rows);
  assert.deepEqual(packRows([]).format, "columns");
  assert.deepEqual(unpackRows(packRows([])), []);
});


test("the worker parser returns what parseAnalyticsCsv returns", async () => {
  const csv = buildCsv(800);
  const log = [];
  const parser = createCsvParser({ createWorker: fakeWorkerFactory(log) });

  const result = await parser.parse(csv);
  const expected = parseAnalyticsCsv(csv);

  assert.deepEqual(result, expected);
  assert.deepEqual(log, [1]);

  // Errors raised in the worker come back as errors.
  await assert.rejects(parser.parse("no,header,here\n1,2,3\n"), /no header row/);

  parser.terminate();
});


test("without Web Workers the parser works on the main thread", async () => {
  const csv = buildCsv(50);
  const parser = createCsvParser({ createWorker: () => null });

  assert.deepEqual(await parser.parse(csv), parseAnalyticsCsv(csv));

  const throwing = createCsvParser({
    createWorker: () => {
      throw new Error("Workers are disabled");
    },
  });

  assert.deepEqual(await throwing.parse(csv), parseAnalyticsCsv(csv));
});


test("a worker that fails to load hands its pending work to the main thread", async () => {
  const csv = buildCsv(50);
  let created = 0;
  let worker = null;

  const parser = createCsvParser({
    createWorker: () => {
      created += 1;
      worker = { postMessage() {}, terminate() {} };
      return worker;
    },
  });

  const pending = parser.parse(csv);
  worker.onerror({ preventDefault() {} });

  assert.deepEqual(await pending, parseAnalyticsCsv(csv));

  // From then on it does not try the broken worker again.
  assert.deepEqual(await parser.parse(csv), parseAnalyticsCsv(csv));
  assert.equal(created, 1);
});


test("terminate() cancels pending parses; the next parse starts a new worker", async () => {
  const csv = buildCsv(50);
  let created = 0;
  const factory = fakeWorkerFactory();
  const parser = createCsvParser({
    createWorker: () => {
      created += 1;
      return factory();
    },
  });

  const pending = parser.parse(csv);
  parser.terminate();

  await assert.rejects(pending, (error) => error.name === "AbortError");

  assert.equal((await parser.parse(csv)).rows.length, 50);
  assert.equal(created, 2);
  parser.terminate();
});


// Performance guard. A month of collection (10 h a day at 5 s) is about
// 200k rows and 40+ MB. Parsing that takes over a second, so it must happen
// in the worker; what remains on the page (rebuilding rows from the
// transferred columns) has to stay a small fraction of it.
test("perf guard: 200k rows cost the page a fraction of the parse", () => {
  const csv = buildCsv(200_000);

  let started = performance.now();
  const { message, transfer } = handleParseRequest({ id: 1, text: csv });
  const workerMs = performance.now() - started;

  assert.equal(message.total, 200_000);
  assert.equal(message.dropped, 0);

  const received = structuredClone(message.packed, { transfer });

  started = performance.now();
  const rows = unpackRows(received);
  const pageMs = performance.now() - started;

  assert.equal(rows.length, 200_000);
  assert.equal(rows.at(-1).timestamp, stamp(new Date(2026, 8, 1, 9, 0, 0).getTime() + 199_999 * 5000));
  assert.ok(
    pageMs * 4 < workerMs,
    `rebuilding rows took ${pageMs.toFixed(0)} ms on the page vs ${workerMs.toFixed(0)} ms parsing in the worker`
  );
});
