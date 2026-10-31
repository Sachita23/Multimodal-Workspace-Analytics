// --------------------------------------------------
// The single-user live status pill ("Live · updated 6 s ago")
// --------------------------------------------------
//
// Pure, so it can be unit tested; LiveIndicator.jsx renders the result.

import { REFRESH_SECONDS, STALE_DATA_SECONDS } from "../data/analytics.js";
import { formatAge, formatClock } from "./format.js";


// The server is reachable but has nothing to show: say why, from /api/status.
function describeNoData(status, error) {
  const lastError = status?.pipeline?.last_error;

  if (lastError) {
    return { tone: "warn", label: "Pipeline error", detail: "no data yet", title: lastError };
  }

  if (status?.collector_stale === true) {
    return {
      tone: "warn",
      label: "Waiting for collector",
      detail: "",
      title: "No samples are being recorded yet. Keep `python start_project.py` running; the collector writes one every 5 seconds.",
    };
  }

  return {
    tone: "warn",
    label: "Waiting for data",
    detail: "",
    title: error?.message || "No samples have been processed yet.",
  };
}


export function describeLiveStatus({ loading, error, dataAsOf, lastUpdated, status, refreshSeconds = REFRESH_SECONDS }, now) {
  const age = dataAsOf ? (now - dataAsOf.getTime()) / 1000 : null;
  // A large history is downloaded less often than every REFRESH_SECONDS (see
  // analyticsPoller.js), so its newest sample may be that much older.
  const staleAfter = STALE_DATA_SECONDS + Math.max(0, refreshSeconds - REFRESH_SECONDS);
  const checked = lastUpdated ? `checked ${formatAge((now - lastUpdated.getTime()) / 1000)}` : "";

  if (!dataAsOf && loading) {
    return { tone: "idle", label: "Connecting…", detail: "" };
  }

  // No answer at all (network error, 5xx, timeout). A 404 ("no-data") means
  // the server is up and simply has no processed file yet.
  if (!dataAsOf && error && error.code !== "no-data") {
    const label = error.code === "timeout" ? "Not responding" : "Offline";
    return { tone: "error", label, detail: "", title: error.message };
  }

  if (!dataAsOf) {
    return describeNoData(status, error);
  }

  const dataTitle = `Newest sample ${formatClock(dataAsOf)}${checked ? `, ${checked}` : ""}`;

  if (error) {
    return { tone: "warn", label: "Reconnecting", detail: `data ${formatAge(age)}`, title: `${error.message} ${dataTitle}` };
  }

  // /api/status knows whether the collector is still writing; without it,
  // fall back to the age of the newest processed sample.
  const collectorStale = typeof status?.collector_stale === "boolean"
    ? status.collector_stale
    : age > staleAfter;

  if (collectorStale) {
    return { tone: "warn", label: "Collector stopped", detail: `last data ${formatAge(age)}`, title: dataTitle };
  }

  if (status?.pipeline?.last_error) {
    return { tone: "warn", label: "Pipeline error", detail: `data ${formatAge(age)}`, title: status.pipeline.last_error };
  }

  if (age > staleAfter) {
    return { tone: "warn", label: "Processing delayed", detail: `data ${formatAge(age)}`, title: dataTitle };
  }

  return { tone: "live", label: "Live", detail: `updated ${formatAge(age)}`, title: dataTitle };
}
