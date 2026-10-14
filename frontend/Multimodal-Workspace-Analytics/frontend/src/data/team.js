const TEAM_API = "/api/team";
const ADMIN_KEY_STORAGE = "workspace-team-admin-key";


export class TeamApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}


// The admin key is remembered in this browser only, as a convenience.
// Reads and writes are guarded because storage can be unavailable.

export function getAdminKey() {
  try {
    return localStorage.getItem(ADMIN_KEY_STORAGE) || "";
  } catch {
    return "";
  }
}


export function setAdminKey(key) {
  try {
    if (key) {
      localStorage.setItem(ADMIN_KEY_STORAGE, key);
    } else {
      localStorage.removeItem(ADMIN_KEY_STORAGE);
    }
  } catch {
    // Key just won't persist across reloads.
  }
}


async function request(path, options = {}) {
  let response;

  try {
    response = await fetch(`${TEAM_API}${path}`, {
      ...options,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Key": getAdminKey(),
        ...options.headers,
      },
    });
  } catch {
    throw new TeamApiError(
      "Can't reach the dashboard server. Start the project with " +
      "`python start_project.py` and reload this page.",
      0
    );
  }

  let body = null;

  try {
    body = await response.json();
  } catch {
    body = null;
  }

  // The Vite proxy answers 5xx with no JSON when the Flask API isn't running.
  if (!response.ok && !body?.error && response.status >= 500) {
    throw new TeamApiError(
      "The API server isn't responding. Start it with " +
      "`python api/server.py` (or `python start_project.py`).",
      response.status
    );
  }

  if (!response.ok) {
    throw new TeamApiError(
      body?.error || `Request failed (${response.status})`,
      response.status
    );
  }

  return body;
}


export function loadTeamOverview() {
  return request("/overview");
}


export function loadMemberDetail(memberId) {
  return request(`/members/${memberId}`);
}


export function loadMemberActivity(memberId, offset = 0, limit = 100) {
  return request(`/members/${memberId}/activity?offset=${offset}&limit=${limit}`);
}


export function createInvite(name) {
  return request("/invites", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}


export function removeMember(memberId) {
  return request(`/members/${memberId}`, { method: "DELETE" });
}


// --------------------------------------------------
// Formatting shared by the Team pages
// --------------------------------------------------

// Behaviour groups keep the same colour wherever they appear
// (validated categorical slots 1-4).
export const BEHAVIOR_COLORS = {
  "Stable-activity": "#2a78d6",
  "Multitasking": "#eb6834",
  "High-switching": "#1baf7a",
  "Idle-heavy": "#eda100",
};

export const BEHAVIOR_ORDER = Object.keys(BEHAVIOR_COLORS);


export function formatDuration(seconds) {
  const minutes = Math.round((seconds || 0) / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}


export function formatRelative(isoTime) {
  if (!isoTime) {
    return "Never";
  }

  const seconds = Math.max(0, (Date.now() - new Date(isoTime).getTime()) / 1000);

  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;

  return `${Math.floor(seconds / 86400)} d ago`;
}


export function formatFeatureValue(feature, value) {
  if (value === null || value === undefined) return "--";
  if (feature === "idle_ratio") return `${(value * 100).toFixed(0)}%`;
  if (feature === "duration_minutes") return `${value.toFixed(0)} min`;
  return value.toFixed(2);
}


export const STATUS_LABELS = {
  online: "Online",
  offline: "Offline",
  joined: "Joined, no data yet",
  invited: "Invite pending",
};
