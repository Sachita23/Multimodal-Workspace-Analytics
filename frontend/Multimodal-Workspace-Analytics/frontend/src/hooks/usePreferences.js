import { useCallback, useSyncExternalStore } from "react";

import { nextSeenIds } from "../utils/anomalyAlerts";


// --------------------------------------------------
// Small reactive stores backed by localStorage.
// Every read and write is guarded: storage can be blocked (private mode,
// disabled site data), and the app must still render without it.
// --------------------------------------------------

const PREFERENCES_KEY = "workspace-analytics-preferences";
const ANOMALIES_SEEN_KEY = "workspace-analytics-anomalies-seen";

export const DEFAULT_PREFERENCES = {
  workspaceName: "Workspace User",
  anomalyAlerts: true,
  compactTables: false,
};


function readStorage(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}


function writeStorage(key, value) {
  try {
    if (value === null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
    return true;
  } catch {
    return false;
  }
}


function createStore(key, parse) {
  let value = parse(readStorage(key));
  const listeners = new Set();

  function emit() {
    listeners.forEach((listener) => listener());
  }

  function subscribe(listener) {
    listeners.add(listener);

    // Keep several open tabs in sync.
    function onStorage(event) {
      if (event.key === key) {
        value = parse(event.newValue);
        emit();
      }
    }

    window.addEventListener("storage", onStorage);

    return () => {
      listeners.delete(listener);
      window.removeEventListener("storage", onStorage);
    };
  }

  function get() {
    return value;
  }

  // Updates the in-memory value even when storage is unavailable, so the
  // change still applies for this visit. Returns whether it persisted.
  function set(next, serialized) {
    value = next;
    const persisted = writeStorage(key, serialized);
    emit();
    return persisted;
  }

  return { subscribe, get, set };
}


function parsePreferences(stored) {
  if (!stored) return DEFAULT_PREFERENCES;

  try {
    const parsed = JSON.parse(stored);

    return {
      workspaceName: typeof parsed.workspaceName === "string" ? parsed.workspaceName : DEFAULT_PREFERENCES.workspaceName,
      anomalyAlerts: typeof parsed.anomalyAlerts === "boolean" ? parsed.anomalyAlerts : DEFAULT_PREFERENCES.anomalyAlerts,
      compactTables: typeof parsed.compactTables === "boolean" ? parsed.compactTables : DEFAULT_PREFERENCES.compactTables,
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}


// Episode ids the user has already seen (newest last, capped).
// UNSEEDED: nothing stored yet (first visit, or site data was cleared).
const UNSEEDED = new Set();

function parseSeen(stored) {
  if (!stored) return UNSEEDED;

  try {
    const parsed = JSON.parse(stored);
    return Array.isArray(parsed) ? new Set(parsed.map(String)) : UNSEEDED;
  } catch {
    return UNSEEDED;
  }
}


const preferencesStore = createStore(PREFERENCES_KEY, parsePreferences);
const anomaliesSeenStore = createStore(ANOMALIES_SEEN_KEY, parseSeen);


/**
 * Returns [preferences, savePreferences]. savePreferences(next) returns
 * true when the preferences were persisted to this browser.
 */
export function usePreferences() {
  const preferences = useSyncExternalStore(preferencesStore.subscribe, preferencesStore.get);

  const savePreferences = useCallback((next) => {
    const merged = parsePreferences(JSON.stringify({ ...preferencesStore.get(), ...next }));
    return preferencesStore.set(merged, JSON.stringify(merged));
  }, []);

  return [preferences, savePreferences];
}


/**
 * Returns [seenIds, markSeen, seeded]. The bell counts anomaly episodes
 * whose id is not in seenIds; markSeen(ids) adds ids (a no-op when nothing
 * is new). `seeded` is false until the first markSeen() in this browser:
 * callers then mark all of history, so old episodes are not "new".
 */
export function useAnomaliesSeen() {
  const seen = useSyncExternalStore(anomaliesSeenStore.subscribe, anomaliesSeenStore.get);

  const markSeen = useCallback((ids) => {
    const current = anomaliesSeenStore.get();
    const next = nextSeenIds(current, ids, { seeded: current !== UNSEEDED });

    if (next === null) return;

    anomaliesSeenStore.set(new Set(next), JSON.stringify(next));
  }, []);

  return [seen, markSeen, seen !== UNSEEDED];
}
