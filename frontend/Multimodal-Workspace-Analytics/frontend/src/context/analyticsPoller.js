import { REFRESH_SECONDS, autoRange, fetchAnalyticsRows, fetchStatus } from "../data/analytics.js";


// --------------------------------------------------
// How often the CSV is downloaded
// --------------------------------------------------
//
// /api/analytics.csv is the whole history, and the live pipeline rewrites it
// every ~10 s while the collector runs, so the ETag rarely saves a download.
// Every new snapshot is downloaded and parsed in full: about 0.4 s for a
// week of data, 1.5 s for a month. To keep that from taking over the
// machine as history grows, the CSV is fetched every REFRESH_SECONDS only
// while one snapshot costs at most 1/COST_FACTOR of that; larger histories
// are fetched less often (never less than every MAX_REFRESH_SECONDS).
// /api/status, which is tiny, is still checked every REFRESH_SECONDS.

export const COST_FACTOR = 10;
export const MAX_REFRESH_SECONDS = 60;

// Pages that need the CSV only for the navbar's anomaly alert (Team, Focus).
export const BACKGROUND_REFRESH_SECONDS = 60;

// Timers never fire early, but a cycle starts a moment after its tick; this
// slack keeps such a cycle from making the next tick look "not due yet".
const TICK_SLACK_MS = 1000;


/**
 * Milliseconds between CSV downloads, given what the last new snapshot cost
 * (download + parse, in ms).
 */
export function rowsRefreshMs(costMs = 0, { background = false } = {}) {
  const base = REFRESH_SECONDS * 1000;
  const adaptive = Math.min(MAX_REFRESH_SECONDS * 1000, Math.max(base, costMs * COST_FACTOR));

  return background ? Math.max(adaptive, BACKGROUND_REFRESH_SECONDS * 1000) : adaptive;
}


/**
 * The range state after new rows arrive: it follows the data ("24h" when
 * there is recent activity, otherwise "all") until the user picks a range.
 */
export function followRange(current, rows, now = Date.now()) {
  if (current.picked) return current;

  const automatic = autoRange(rows, now);

  return current.value === automatic ? current : { value: automatic, picked: false };
}


/**
 * The polling machinery behind AnalyticsProvider, free of React so it can be
 * unit tested.
 *
 * poll() runs one full cycle now: /api/analytics.csv (with If-None-Match)
 * and /api/status. tick() is what the timer calls: it downloads the CSV only
 * when it is due (see rowsRefreshMs) and otherwise just checks /api/status
 * (nothing at all on a background page). A cycle that is already running is
 * shared rather than duplicated. Every request has a deadline
 * (fetchWithTimeout), so a cycle always settles and a hung request can never
 * stop polling or leave a refresh spinning.
 *
 * Callbacks (each called only while the cycle was not aborted):
 *   onStatus(status | null)  as soon as /api/status answers, independent of
 *                            the CSV, so a slow download never holds back
 *                            the collector-stale check
 *   onRows(rows, {refreshMs}) a new snapshot (not on 304), and the CSV
 *                            refresh interval that its cost leads to
 *   onSuccess()              the CSV request succeeded (200 or 304)
 *   onNoData(error)          404: the server is up but has no processed file;
 *                            the old snapshot is gone and must not be shown
 *   onError(error)           any failed CSV request (after onNoData)
 *
 * `parse` is handed to fetchRows (the provider passes a Web Worker parser).
 * abortAll() cancels running requests (unmount); poll() works again after.
 */
export function createAnalyticsPoller({
  onStatus = () => {},
  onRows = () => {},
  onSuccess = () => {},
  onNoData = () => {},
  onError = () => {},
  fetchRows = fetchAnalyticsRows,
  loadStatus = fetchStatus,
  parse,
  timeoutMs,
  clock = () => Date.now(),
} = {}) {
  let etag = null;
  let inFlight = null;
  let generation = 0;
  let lastRowsRequest = Number.NEGATIVE_INFINITY;
  let costMs = 0;
  const controllers = new Set();


  function run(withRows) {
    const controller = new AbortController();
    const { signal } = controller;
    controllers.add(controller);

    const statusTask = loadStatus({ signal, timeoutMs }).then(
      (status) => {
        if (!signal.aborted) onStatus(status);
      },
      () => {}
    );

    let rowsTask = Promise.resolve();

    if (withRows) {
      const started = clock();
      lastRowsRequest = started;

      rowsTask = fetchRows({ signal, etag, timeoutMs, parse }).then(
        (result) => {
          if (signal.aborted) return;

          if (!result.notModified) {
            etag = result.etag;
            costMs = Math.max(0, clock() - started);
            onRows(result.rows, { refreshMs: rowsRefreshMs(costMs) });
          }

          onSuccess();
        },
        (error) => {
          if (signal.aborted) return;

          if (error?.code === "no-data") {
            // Forget the etag too: if the same file comes back, it must be
            // downloaded again rather than answered with a 304.
            etag = null;
            onNoData(error);
          }

          onError(error);
          throw error;
        }
      );
    }

    const cycle = { withRows, promise: null };

    cycle.promise = Promise.allSettled([rowsTask, statusTask])
      .then(([rowsResult]) => {
        if (rowsResult.status === "rejected") throw rowsResult.reason;
      })
      .finally(() => {
        controllers.delete(controller);

        if (inFlight === cycle) {
          inFlight = null;
        }
      });

    inFlight = cycle;

    return cycle.promise;
  }


  // A full cycle now (first load, the Refresh button, the tab coming back).
  function poll() {
    if (inFlight?.withRows) {
      return inFlight.promise;
    }

    if (inFlight) {
      // Only /api/status is running: let it finish, then download the CSV.
      const started = generation;

      return inFlight.promise
        .catch(() => {})
        .then(() => (started === generation ? poll() : undefined));
    }

    return run(true);
  }


  // One timer tick: the CSV when it is due, otherwise only /api/status.
  function tick({ background = false } = {}) {
    if (inFlight) {
      return inFlight.promise;
    }

    const due = clock() - lastRowsRequest + TICK_SLACK_MS >= rowsRefreshMs(costMs, { background });

    if (due) return run(true);
    if (background) return Promise.resolve();

    return run(false);
  }


  function abortAll() {
    generation += 1;
    controllers.forEach((controller) => controller.abort());
    controllers.clear();
    inFlight = null;
  }


  // The current CSV refresh interval on a single-user page, in ms.
  function refreshMs() {
    return rowsRefreshMs(costMs);
  }


  return { poll, tick, abortAll, refreshMs };
}


/**
 * Drives a poller: one full poll now, then tick() every REFRESH_SECONDS
 * while the page is visible, and once more as soon as it becomes visible
 * again. Nothing is requested while the tab is hidden. isBackground() says
 * whether the current page needs the CSV only for the navbar alert.
 *
 * Returns stop(), which clears the timer and listener and aborts requests.
 */
export function startPolling(poller, {
  doc = globalThis.document,
  isBackground = () => false,
  setTimer = setInterval,
  clearTimer = clearInterval,
} = {}) {
  const quiet = (promise) => {
    promise.catch(() => {});
  };

  const tick = () => {
    if (!doc.hidden) quiet(poller.tick({ background: isBackground() }));
  };

  quiet(poller.poll());

  const timer = setTimer(tick, REFRESH_SECONDS * 1000);
  doc.addEventListener("visibilitychange", tick);

  return () => {
    clearTimer(timer);
    doc.removeEventListener("visibilitychange", tick);
    poller.abortAll();
  };
}
