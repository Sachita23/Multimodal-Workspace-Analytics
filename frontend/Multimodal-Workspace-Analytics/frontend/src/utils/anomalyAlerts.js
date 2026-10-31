// --------------------------------------------------
// The navbar's "new unusual episodes" alert
// --------------------------------------------------
//
// The bell counts all-time anomaly episodes the user has not seen, but the
// Anomalies page lists only the selected range. These helpers keep the two
// in step: the bell opens the page on a range that lists every new episode,
// and a visit marks as seen only the episodes the page actually lists.

import { rangeCovering } from "../data/analytics.js";


// Most episode ids remembered as seen (oldest dropped first).
export const MAX_SEEN_IDS = 5000;


export function unseenEpisodes(episodes, seen) {
  return episodes.filter((episode) => !seen.has(episode.id));
}


/**
 * The (all-time) episodes the Anomalies page lists for a range that starts
 * at `rangeStartMs` (null for all time): every one with a sample in range.
 */
export function episodesInRange(episodes, rangeStartMs) {
  if (rangeStartMs === null || rangeStartMs === undefined) return episodes;

  return episodes.filter((episode) => episode.endMs >= rangeStartMs);
}


/**
 * The range to show `episodes` in full: the narrowest one that is at least
 * as wide as `current` and starts before the oldest of them.
 */
export function rangeShowing(episodes, current, now = Date.now()) {
  if (!episodes.length) return current;

  const oldest = episodes.reduce((low, episode) => Math.min(low, episode.startMs), Infinity);

  return rangeCovering(oldest, current, now);
}


/**
 * Ids to mark as seen when the Anomalies page shows a range. On a first
 * visit (nothing stored yet) that is all of history, so episodes from
 * before the dashboard was first opened never light the bell.
 */
export function idsToMarkSeen(episodes, rangeStartMs, seeded) {
  const shown = seeded ? episodesInRange(episodes, rangeStartMs) : episodes;
  return shown.map((episode) => episode.id);
}


/**
 * The new stored list after marking `ids` as seen, or null when nothing
 * changes. An unseeded store is always written (even with no ids), so it
 * counts as seeded from then on.
 */
export function nextSeenIds(current, ids, { seeded = true, max = MAX_SEEN_IDS } = {}) {
  const fresh = new Set();

  for (const id of ids) {
    const key = String(id);
    if (!current.has(key)) fresh.add(key);
  }

  if (fresh.size === 0 && seeded) return null;

  return [...current, ...fresh].slice(-max);
}
