// --------------------------------------------------
// Parsing /api/analytics.csv off the main thread
// --------------------------------------------------
//
// The processed CSV holds the whole history. After a week of collection it is
// about 10 MB (0.35 s to parse), after a month over 40 MB (1.5 s), and the
// live pipeline rewrites it every ~10 s while the collector runs. Parsing it
// on the page would freeze the dashboard for that long on every refresh, so
// it is parsed in a Web Worker and only the finished rows come back
// (as columns, see csvTransport.js).

import { parseAnalyticsCsv } from "../data/analytics.js";
import { unpackRows } from "./csvTransport.js";


function abortError() {
  const error = new Error("The request was aborted.");
  error.name = "AbortError";
  return error;
}


function createDefaultWorker() {
  if (typeof Worker === "undefined") return null;

  // Vite bundles the worker from this exact expression.
  return new Worker(new URL("./analyticsCsv.worker.js", import.meta.url), { type: "module" });
}


/**
 * Returns {parse(text) -> Promise<{rows, dropped, total}>, terminate()}.
 *
 * parse() uses a Web Worker, started on first use. Without Worker support
 * (Node tests, very old browsers), or once the worker fails (for example a
 * blocked script), it parses on the main thread instead, including the
 * requests the failed worker still had. terminate() stops the worker and
 * rejects pending requests with an AbortError; the next parse() starts a new
 * worker.
 */
export function createCsvParser({ createWorker = createDefaultWorker } = {}) {
  let worker = null;
  let workerFailed = false;
  let nextId = 0;
  const pending = new Map();


  function parseHere(text) {
    return new Promise((resolve) => resolve(parseAnalyticsCsv(text)));
  }


  function stopWorker() {
    if (!worker) return;

    worker.onmessage = null;
    worker.onerror = null;
    worker.onmessageerror = null;
    worker.terminate();
    worker = null;
  }


  function onMessage(event) {
    const { id, packed, dropped, total, error } = event.data || {};
    const request = pending.get(id);

    if (!request) return;

    pending.delete(id);

    if (error !== undefined) {
      request.reject(new Error(error));
      return;
    }

    try {
      request.resolve({ rows: unpackRows(packed), dropped, total });
    } catch (unpackError) {
      request.reject(unpackError);
    }
  }


  function onFailure(event) {
    event?.preventDefault?.();
    workerFailed = true;

    const waiting = [...pending.values()];
    pending.clear();
    stopWorker();

    for (const request of waiting) {
      parseHere(request.text).then(request.resolve, request.reject);
    }
  }


  function getWorker() {
    if (worker || workerFailed) return worker;

    try {
      worker = createWorker();
    } catch {
      worker = null;
    }

    if (!worker) {
      workerFailed = true;
      return null;
    }

    worker.onmessage = onMessage;
    worker.onerror = onFailure;
    worker.onmessageerror = onFailure;

    return worker;
  }


  function parse(text) {
    const target = getWorker();

    if (!target) return parseHere(text);

    nextId += 1;
    const id = nextId;

    return new Promise((resolve, reject) => {
      pending.set(id, { text, resolve, reject });

      try {
        target.postMessage({ id, text });
      } catch (error) {
        pending.delete(id);
        reject(error);
      }
    });
  }


  function terminate() {
    stopWorker();

    const waiting = [...pending.values()];
    pending.clear();

    for (const request of waiting) request.reject(abortError());
  }


  return { parse, terminate };
}
