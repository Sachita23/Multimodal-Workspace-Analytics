import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import {
  REFRESH_SECONDS,
  computeAnalytics,
  groupAnomalyEpisodes,
} from "../data/analytics";
import { createAnalyticsPoller, followRange, startPolling } from "./analyticsPoller";
import { createCsvParser } from "./csvParser";


const AnalyticsContext = createContext(null);


/**
 * One shared, live copy of the single-user analytics.
 *
 * Fetches /api/analytics.csv once, then with If-None-Match (an unchanged
 * file costs a 304) every REFRESH_SECONDS, or less often when the history
 * has grown so large that one download and parse costs over a second (see
 * analyticsPoller.js). /api/status is checked every REFRESH_SECONDS. The CSV
 * is parsed in a Web Worker so the page stays responsive.
 *
 * `background` is true on pages that show no single-user analytics (Team,
 * Focus): the CSV then only feeds the navbar's anomaly alert and is fetched
 * at most every BACKGROUND_REFRESH_SECONDS, without /api/status.
 *
 * Polling pauses while the tab is hidden and catches up as soon as it is
 * shown again. Every request has a deadline, so a server that stops
 * answering shows up as an error instead of freezing polling. A failed poll
 * keeps the last good data and reports the error, except a 404: the server
 * then says there is no processed data, so the old snapshot is dropped.
 */
export function AnalyticsProvider({ children, background = false }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [status, setStatus] = useState(null);
  const [refreshSeconds, setRefreshSeconds] = useState(REFRESH_SECONDS);
  // `picked` once the user chooses a range; until then it follows the data.
  const [rangeState, setRangeState] = useState({ value: "24h", picked: false });
  const range = rangeState.value;

  // Created once; every callback only calls state setters, which are stable.
  const [parser] = useState(() => createCsvParser());
  const [poller] = useState(() => createAnalyticsPoller({
    parse: parser.parse,
    onStatus: setStatus,
    onRows(nextRows, { refreshMs }) {
      setRangeState((current) => followRange(current, nextRows));
      setRows(nextRows);
      setRefreshSeconds(refreshMs / 1000);
    },
    onSuccess() {
      setLoading(false);
      setError(null);
      setLastUpdated(new Date());
    },
    onNoData() {
      setRows(null);
    },
    onError(nextError) {
      setLoading(false);
      setError(nextError);
    },
  }));

  const backgroundRef = useRef(background);


  useEffect(() => {
    const stop = startPolling(poller, { isBackground: () => backgroundRef.current });

    return () => {
      stop();
      parser.terminate();
    };
  }, [poller, parser]);


  // Back on a single-user page after Team or Focus: catch up right away
  // (a no-op while the first poll is still running).
  useEffect(() => {
    backgroundRef.current = background;

    if (!background && !document.hidden) {
      poller.tick().catch(() => {});
    }
  }, [background, poller]);


  const setRange = useCallback((value) => {
    setRangeState({ value, picked: true });
  }, []);


  const data = useMemo(
    () => (rows ? computeAnalytics(rows, range) : null),
    [rows, range]
  );

  const dataAsOf = useMemo(
    () => (rows && rows.length ? new Date(rows[rows.length - 1].ts) : null),
    [rows]
  );

  // All-time episodes, for the navbar alert (independent of the range).
  const anomalyEpisodes = useMemo(
    () => (rows ? groupAnomalyEpisodes(rows) : []),
    [rows]
  );


  const value = useMemo(() => ({
    data,
    rows: rows || [],
    loading,
    error,
    lastUpdated,
    dataAsOf,
    range,
    setRange,
    refresh: poller.poll,
    refreshSeconds,
    status,
    anomalyEpisodes,
  }), [data, rows, loading, error, lastUpdated, dataAsOf, range, setRange, poller, refreshSeconds, status, anomalyEpisodes]);


  return (
    <AnalyticsContext.Provider value={value}>
      {children}
    </AnalyticsContext.Provider>
  );
}


// eslint-disable-next-line react/only-export-components
export function useAnalytics() {
  const context = useContext(AnalyticsContext);

  if (!context) {
    throw new Error("useAnalytics() must be used inside <AnalyticsProvider>.");
  }

  return context;
}
