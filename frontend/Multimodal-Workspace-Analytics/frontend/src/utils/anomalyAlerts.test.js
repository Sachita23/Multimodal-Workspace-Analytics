// Unit tests for the navbar's anomaly alert. Run with `npm test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { autoRange, computeAnalytics, groupAnomalyEpisodes, parseAnalyticsCsv } from "../data/analytics.js";
import {
  episodesInRange,
  idsToMarkSeen,
  nextSeenIds,
  rangeShowing,
  unseenEpisodes,
} from "./anomalyAlerts.js";


const NOW = new Date(2026, 9, 15, 20, 0, 0).getTime();

const pad = (value) => String(value).padStart(2, "0");

function stamp(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const HEADER = "timestamp,application,window_title,activity_status,idle_seconds,cpu_percent,memory_percent,battery_percent,charging,duration_seconds,active_duration_seconds,idle_duration_seconds,session_id,application_switch,window_switch,cluster_id,behavior_label,anomaly_score,is_anomaly";


// The review's scenario: session 1 three days ago was flagged (for example
// by a later refit), session 2 an hour ago is normal, session 3 ten minutes
// ago is flagged.
function rows({ recentFlagged = false } = {}) {
  const lines = [HEADER];
  const session = (id, start, flagged) => {
    for (let i = 0; i < 120; i += 1) {
      lines.push(`${stamp(start + i * 5000)},Code,x,Active,0,${flagged ? 90 : 10},80,50,False,5,5,0,${id},False,False,0,Stable-activity,${flagged ? 0.9 : 0.1},${flagged ? "True" : "False"}`);
    }
  };

  session(1, NOW - 3 * 86400e3, true);
  session(2, NOW - 3600e3, false);
  if (recentFlagged) session(3, NOW - 600e3, true);

  return parseAnalyticsCsv(`${lines.join("\n")}\n`).rows;
}


test("the bell opens a range that lists the new episode it counts", () => {
  const all = rows();
  const episodes = groupAnomalyEpisodes(all);
  const range = autoRange(all, NOW);

  assert.equal(range, "24h");
  assert.equal(unseenEpisodes(episodes, new Set()).length, 1);

  // The default range does not list it ...
  assert.equal(computeAnalytics(all, range, { now: NOW }).anomalyEpisodes.length, 0);

  // ... so the bell widens it to one that does, in full.
  const target = rangeShowing(episodes, range, NOW);
  assert.equal(target, "7d");

  const shown = computeAnalytics(all, target, { now: NOW }).anomalyEpisodes;
  assert.deepEqual(shown.map((episode) => episode.id), episodes.map((episode) => episode.id));

  // A wider range the user picked is kept; no new episodes, no change.
  assert.equal(rangeShowing(episodes, "all", NOW), "all");
  assert.equal(rangeShowing([], "24h", NOW), "24h");
});


test("visiting Anomalies marks as seen only the episodes listed for the range", () => {
  const all = rows({ recentFlagged: true });
  const episodes = groupAnomalyEpisodes(all);
  const day = computeAnalytics(all, "24h", { now: NOW });

  assert.equal(episodes.length, 2);

  const marked = idsToMarkSeen(episodes, day.rangeStart, true);
  assert.deepEqual(marked, day.anomalyEpisodes.map((episode) => episode.id));
  assert.equal(marked.length, 1);

  // The older one stays new, so the bell still shows it.
  const seen = new Set(marked);
  assert.deepEqual(unseenEpisodes(episodes, seen).map((episode) => episode.session_id), ["1"]);

  // "All time" lists (and marks) everything.
  assert.equal(idsToMarkSeen(episodes, null, true).length, 2);
  assert.deepEqual(episodesInRange(episodes, null), episodes);
});


test("an episode that straddles the range start counts as listed", () => {
  const episode = { id: "a", startMs: NOW - 25 * 3600e3, endMs: NOW - 23 * 3600e3 };

  assert.deepEqual(episodesInRange([episode], NOW - 24 * 3600e3), [episode]);
  assert.deepEqual(episodesInRange([episode], NOW - 22 * 3600e3), []);
  // Opening it from the bell shows it in full.
  assert.equal(rangeShowing([episode], "24h", NOW), "7d");
});


test("first visit: all of history is marked, so old episodes never light the bell", () => {
  const episodes = groupAnomalyEpisodes(rows({ recentFlagged: true }));
  const day = computeAnalytics(rows({ recentFlagged: true }), "24h", { now: NOW });

  // Not seeded yet: the page (or the bell) marks everything, whatever the range.
  assert.equal(idsToMarkSeen(episodes, day.rangeStart, false).length, 2);

  // Seeding an empty history still stores a list, so it counts as seeded.
  assert.deepEqual(nextSeenIds(new Set(), [], { seeded: false }), []);
  assert.equal(nextSeenIds(new Set(), [], { seeded: true }), null);
});


test("nextSeenIds adds new ids once, keeps the newest when full", () => {
  assert.equal(nextSeenIds(new Set(["a"]), ["a"]), null);
  assert.deepEqual(nextSeenIds(new Set(["a"]), ["b", "b", 3]), ["a", "b", "3"]);
  assert.deepEqual(nextSeenIds(new Set(["a", "b"]), ["c"], { max: 2 }), ["b", "c"]);
});
