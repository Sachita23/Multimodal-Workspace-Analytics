// --------------------------------------------------
// Shared formatting and colour helpers for the single-user pages
// --------------------------------------------------


// Brand colour, used for single-series charts.
export const BRAND_COLOR = "#6252df";

// Validated categorical palette (same slots as the Team pages, plus slot 5).
export const CHART_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"];

// Neutral for "idle" series and labels outside the palette.
export const NEUTRAL_COLOR = "#a0a9b9";

// Behaviour groups keep the same colour wherever they appear
// (matches team.js so the single-user and team pages agree).
export const BEHAVIOR_COLORS = {
  "Stable-activity": "#2a78d6",
  "Multitasking": "#eb6834",
  "High-switching": "#1baf7a",
  "Idle-heavy": "#eda100",
  // ml/clustering.py names left-over clusters "Mixed-activity", "Mixed-activity 2", ...
  "Mixed-activity": "#e87ba4",
};


// Labels the pipeline gives sessions it did not cluster (ml/clustering.py):
// the still-open session, very short ones, and too little history. They
// are shown, but are not behaviour patterns.
const UNCLASSIFIED_LABELS = new Set(["In-progress", "Too-short", "Not-enough-history", "Unknown"]);

export function isBehaviorLabel(label) {
  return Boolean(label) && !UNCLASSIFIED_LABELS.has(label);
}


export function behaviorColor(label) {
  if (BEHAVIOR_COLORS[label]) return BEHAVIOR_COLORS[label];
  if (String(label).startsWith("Mixed-activity")) return BEHAVIOR_COLORS["Mixed-activity"];

  return NEUTRAL_COLOR;
}


// "45 s", "12 min", "2h 05m"
export function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);

  if (value < 60) {
    return `${Math.round(value)} s`;
  }

  const minutes = Math.round(value / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  const hours = Math.floor(minutes / 60);
  const remaining = String(minutes % 60).padStart(2, "0");

  return `${hours}h ${remaining}m`;
}


// Minutes with one decimal while small, whole minutes after that.
export function formatMinutes(minutes) {
  const value = Number(minutes) || 0;

  return value < 10 ? `${value.toFixed(1)} min` : `${Math.round(value)} min`;
}


export function formatPercent(value, decimals = 1) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "--";
  }

  return `${number.toFixed(decimals)}%`;
}


// "just now", "6 s ago", "4 min ago", "3 h ago", "2 d ago"
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "never";
  }

  const value = Math.max(0, Math.round(seconds));

  if (value < 2) return "just now";
  if (value < 60) return `${value} s ago`;
  if (value < 3600) return `${Math.floor(value / 60)} min ago`;
  if (value < 86400) return `${Math.floor(value / 3600)} h ago`;

  return `${Math.floor(value / 86400)} d ago`;
}


export function formatClock(date) {
  if (!date) return "--";

  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}


export function formatDateTime(date) {
  if (!date) return "--";

  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}


// "Oct 14, 14:05 – 15:20" (the end drops the date when it is the same day)
export function formatTimeRange(start, end) {
  if (!start) return "--";

  const startText = formatDateTime(start);

  if (!end) return startText;

  const sameDay = start.toDateString() === end.toDateString();
  const endText = sameDay
    ? end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : formatDateTime(end);

  return `${startText} – ${endText}`;
}


// "mm:ss" or "h:mm:ss" for countdowns
export function formatCountdown(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = String(value % 60).padStart(2, "0");

  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}`
    : `${minutes}:${secs}`;
}


export function initials(name) {
  const words = String(name || "").trim().split(/\s+/).filter(Boolean);

  if (words.length === 0) return "W";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();

  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}


export function truncate(text, length) {
  const value = String(text || "");

  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}
