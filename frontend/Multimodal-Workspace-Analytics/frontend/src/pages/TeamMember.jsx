import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Lock, RefreshCw, Trash2 } from "lucide-react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";

import DashboardLayout from "../layouts/DashboardLayout";
import AdminKeyForm from "../components/AdminKeyForm";
import {
  BEHAVIOR_COLORS,
  DEFAULT_TEAM_RANGE,
  RelativeTime,
  STATUS_LABELS,
  TEAM_RANGES,
  describeTeamRange,
  focusAllowedApps,
  formatDuration,
  formatFeatureValue,
  formatRelative,
  formatWindow,
  getAdminKey,
  isAbortError,
  isInviteExpired,
  isPendingInvite,
  keyWasAccepted,
  loadMemberActivity,
  loadMemberDetail,
  loadMemberFocus,
  loadTeamLive,
  memberFocusIntervalMs,
  mergeRows,
  normalizeTeamRange,
  removeMember,
  revokePendingInvite,
  runningMemberSession,
  setAdminKey,
  subscribeAdminKey,
  teamRangeSearch,
  trimRows,
  useNow,
  usePolling,
} from "../data/team";
import { formatScore, scoreBand } from "../data/focus";
import { formatCountdown } from "../utils/format";
import "./team.css";


const DETAIL_REFRESH_MS = 15000;
const LIVE_REFRESH_MS = 5000;
const ACTIVITY_REFRESH_MS = 10000;
const STALE_MS = 45000;

const PAGE_SIZE = 100;
const NEW_ROWS_LIMIT = 500;

// Rows kept in the log while new samples stream in at the top (an online
// member adds ~720 an hour). Older ones stay reachable with "Load older".
const MAX_LIVE_ROWS = 500;


function DemoBadge() {
  return <span className="demo-badge" title="Simulated demo member">Demo</span>;
}


function ActivityBadge({ status }) {
  const idle = status === "Idle";

  return (
    <span className={`activity-badge ${idle ? "is-idle" : "is-active"}`}>
      {idle ? "Idle" : "Active"}
    </span>
  );
}


function RangeSelector({ value, onChange }) {
  return (
    <div className="range-group" role="group" aria-label="Time range">
      {TEAM_RANGES.map((range) => (
        <button
          key={range.value}
          type="button"
          title={range.title}
          aria-pressed={value === range.value}
          onClick={() => onChange(range.value)}
        >
          {range.label}
        </button>
      ))}
    </div>
  );
}


function LiveStatus({ checkedAt }) {
  const now = useNow();
  const stale = checkedAt && now - checkedAt > STALE_MS;

  // Only the label is a live region, so screen readers hear "Live,
  // delayed" when it changes but not the ticking "N s ago".
  return (
    <span className="team-live-indicator is-plain">
      <span className={`team-live-dot ${!checkedAt ? "is-paused" : stale ? "is-stale" : "is-live"}`} aria-hidden="true" />
      <span role="status">{stale ? "Live, delayed" : "Live"}</span>
      {checkedAt && <span className="team-live-updated">{formatRelative(checkedAt)}</span>}
    </span>
  );
}


function formatMinutes(seconds) {
  const value = Number(seconds) || 0;
  return value < 60 ? `${Math.round(value)} s` : `${(value / 60).toFixed(1)} min`;
}


function formatPercent(value) {
  return value === null || value === undefined ? "--" : `${Math.round(value)}%`;
}


// --------------------------------------------------
// Right now (from /api/team/live)
// --------------------------------------------------

function RightNow({ entry, onlineWindow }) {
  const { current } = entry;
  const recent = entry.last_10_min;
  const hasRecent = recent && (recent.active_seconds || 0) + (recent.idle_seconds || 0) > 0;

  return (
    <div className="right-now-card">
      <div>
        <h3>Right now</h3>
        {current ? (
          <>
            <div className="right-now-app">
              <ActivityBadge status={current.activity_status} />
              <strong title={current.application}>{current.application}</strong>
            </div>
            <p className="right-now-window" title={current.window_title}>
              {current.window_title || "No window title"}
            </p>
            <p className="section-sub">
              Sample from {current.timestamp} (their local time)
              {current.activity_status === "Idle" && `, idle for ${formatMinutes(current.idle_seconds)}`}
              {` · CPU ${formatPercent(current.cpu_percent)}`}
              {current.battery_percent !== null && current.battery_percent !== undefined
                && ` · Battery ${formatPercent(current.battery_percent)}`}
            </p>
          </>
        ) : (
          <p className="section-sub">
            Not online
            {onlineWindow ? ` (nothing uploaded in the last ${onlineWindow})` : ""}.
            {entry.last_seen
              ? <> Last upload <RelativeTime value={entry.last_seen} />.</>
              : " Nothing uploaded yet."}
          </p>
        )}
      </div>

      {hasRecent && (
        <dl className="right-now-stats">
          <div><dt>Active</dt><dd>{formatMinutes(recent.active_seconds)}</dd></div>
          <div><dt>Idle</dt><dd>{formatMinutes(recent.idle_seconds)}</dd></div>
          <div><dt>Switches</dt><dd>{recent.switches}</dd></div>
          <div><dt>Apps</dt><dd>{recent.distinct_apps}</dd></div>
        </dl>
      )}

      {hasRecent && (
        <p className="right-now-caption">
          Figures cover the 10 minutes up to their newest sample.
        </p>
      )}
    </div>
  );
}


// --------------------------------------------------
// Activity log: cursor paging + live prepends
// --------------------------------------------------

// Memoised (with a stable onAuthError from the page) so the page's 5 s
// live poll and other state changes don't re-render every row.
const ActivityLog = memo(function ActivityLog({ memberId, onAuthError }) {
  const [rows, setRows] = useState([]);
  const [pending, setPending] = useState([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  const scrollRef = useRef(null);
  const newestRef = useRef(null);
  const olderRef = useRef(null);

  // The rows on screen, for read-modify-write from async callbacks (the
  // live tick and "Load older" can finish in either order).
  const rowsRef = useRef([]);

  // How many rows live prepends may keep: MAX_LIVE_ROWS, or more once the
  // reader has loaded older pages themselves.
  const rowLimitRef = useRef(MAX_LIVE_ROWS);

  // Abort a pending "Load more" on unmount.
  useEffect(() => () => olderRef.current?.abort(), []);


  function scrolledDown() {
    return (scrollRef.current?.scrollTop ?? 0) > 8;
  }


  function replaceRows(next) {
    rowsRef.current = next;
    setRows(next);
  }


  // New rows go on top. Only done while the reader is at the top, so the
  // oldest rows beyond the limit can be dropped unseen ("Load older"
  // brings them back). Not while "Load older" is in flight: its page
  // continues from the current oldest row, and trimming would leave a gap.
  function prependRows(newRows) {
    const merged = mergeRows(newRows, rowsRef.current);
    const kept = olderRef.current ? merged : trimRows(merged, rowLimitRef.current);

    if (kept.length < merged.length) {
      setHasMore(true);
    }

    replaceRows(kept);
  }


  async function loadNewestPage(signal, isCurrent) {
    olderRef.current?.abort();

    const page = await loadMemberActivity(memberId, { limit: PAGE_SIZE, signal });

    if (!isCurrent()) return;

    rowLimitRef.current = MAX_LIVE_ROWS;
    replaceRows(page.rows);
    setPending([]);
    setTotal(page.total);
    setHasMore(page.rows.length >= PAGE_SIZE && page.has_more !== false);
    setLoaded(true);
    newestRef.current = page.rows[0]?.timestamp ?? null;
  }


  // First page, then every tick only the samples newer than the newest one
  // shown (after=), so streaming data never shifts or duplicates rows.
  usePolling(async ({ signal, isCurrent }) => {
    try {
      const after = newestRef.current;

      if (!after) {
        await loadNewestPage(signal, isCurrent);
      } else {
        const page = await loadMemberActivity(memberId, { after, limit: NEW_ROWS_LIMIT, signal });

        if (!isCurrent()) return;

        if (page.has_more === true || page.rows.length >= NEW_ROWS_LIMIT) {
          // A long offline queue just arrived: more new samples than one
          // request returns, so start again from the newest page instead of
          // walking through the whole backlog.
          await loadNewestPage(signal, isCurrent);
        } else if (page.rows.length > 0) {
          newestRef.current = page.rows[0].timestamp;
          setTotal((count) => count + page.rows.length);

          // Don't move rows under someone reading further down.
          if (scrolledDown()) {
            setPending((current) => mergeRows(page.rows, current));
          } else {
            prependRows(page.rows);
          }
        }
      }

      if (isCurrent()) {
        setError("");
      }
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;

      if (!onAuthError(err)) {
        setError(err.message);
      }
    }
  }, ACTIVITY_REFRESH_MS, { key: String(memberId) });


  function showPending() {
    prependRows(pending);
    setPending([]);

    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }


  function onScroll() {
    if (pending.length > 0 && !scrolledDown()) {
      showPending();
    }
  }


  async function loadOlder() {
    const oldest = rowsRef.current[rowsRef.current.length - 1]?.timestamp;

    if (!oldest || loadingMore) {
      return;
    }

    const controller = new AbortController();
    olderRef.current = controller;
    setLoadingMore(true);

    try {
      const page = await loadMemberActivity(memberId, {
        before: oldest,
        limit: PAGE_SIZE,
        signal: controller.signal,
      });

      // The newest page was reloaded meanwhile; this page no longer joins on.
      if (controller.signal.aborted) return;

      const next = mergeRows(rowsRef.current, page.rows);

      // Rows the reader asked for stay until the newest page is reloaded.
      rowLimitRef.current = Math.max(rowLimitRef.current, next.length);
      replaceRows(next);
      setHasMore(page.rows.length >= PAGE_SIZE && page.has_more !== false);
      setError("");
    } catch (err) {
      if (isAbortError(err)) return;

      if (!onAuthError(err)) {
        setError(err.message);
      }
    } finally {
      if (olderRef.current === controller) {
        olderRef.current = null;
      }

      setLoadingMore(false);
    }
  }


  const remaining = total - rows.length - pending.length;

  return (
    <div className="session-table-card">
      <div className="table-header">
        <h3>Activity log</h3>
        <p>
          Every sample this member shared, newest first ({total.toLocaleString()} total).
          New samples appear as they arrive.
        </p>
      </div>
      {error && <p className="form-error table-error" role="alert">{error}</p>}
      {/* Same text while the count grows, so it is announced once. */}
      <p className="sr-only" role="status">
        {pending.length > 0 ? "New activity samples arrived above. Use the show button to see them." : ""}
      </p>
      {pending.length > 0 && (
        <div className="new-samples-bar">
          <button type="button" className="new-samples-button" onClick={showPending}>
            {pending.length} new sample{pending.length === 1 ? "" : "s"}, show
          </button>
        </div>
      )}
      <div className="table-wrapper activity-log" ref={scrollRef} onScroll={onScroll}>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Application</th>
              <th>Window title</th>
              <th>Status</th>
              <th>Idle</th>
              <th>CPU</th>
              <th>Memory</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan="7" className="empty-table">
                  {loaded ? "No activity shared yet." : "Loading activity..."}
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.timestamp}>
                <td>{row.timestamp}</td>
                <td><strong>{row.application}</strong></td>
                <td className="window-title" title={row.window_title}>{row.window_title}</td>
                <td>{row.activity_status}</td>
                <td>{Math.round(row.idle_seconds)} s</td>
                <td>{row.cpu_percent}%</td>
                <td>{row.memory_percent}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMore && rows.length > 0 && (
        <div className="table-footer">
          <button
            type="button"
            className="range-selector"
            disabled={loadingMore}
            aria-busy={loadingMore}
            onClick={loadOlder}
          >
            {loadingMore
              ? "Loading..."
              : remaining > 0
                ? `Load ${Math.min(PAGE_SIZE, remaining)} older samples`
                : "Load older samples"}
          </button>
        </div>
      )}
    </div>
  );
});


// --------------------------------------------------
// Focus sessions (team_sync.py focus): /members/<id>/focus, every 5 s
// while one runs and every 30 s otherwise
// --------------------------------------------------

function FocusScore({ score }) {
  const band = scoreBand(score);

  return (
    <span className="team-focus-score">
      <strong>{formatScore(score)}</strong>
      <span className={`focus-score-badge is-${band.band}`}>{band.label}</span>
    </span>
  );
}


// The running session: a row from /focus, or the live view's summary of it.
function LiveFocusSession({ session, receivedAt }) {
  const now = useNow();
  const elapsed = receivedAt ? Math.max(0, (now - receivedAt) / 1000) : 0;
  const remaining = session.remaining_s === null || session.remaining_s === undefined
    ? null
    : Math.max(0, Number(session.remaining_s) - elapsed);
  const allowed = session.current_allowed ?? session.allowed ?? null;
  const apps = focusAllowedApps(session.allowed_apps);
  const band = scoreBand(session.score);

  return (
    <div className={`member-focus-live${allowed === false ? " is-off-task" : ""}`}>
      <div>
        <span className="section-kicker">Focus session running</span>
        <h3>{session.title || "Focus session"}</h3>
        {session.goal && session.goal !== session.title && <p className="section-sub">{session.goal}</p>}
        <div className="now-line member-focus-now">
          {allowed !== null && (
            <span className={`focus-task-badge ${allowed ? "on-task" : "off-task"}`}>
              {allowed ? "On task" : "Off task"}
            </span>
          )}
          {session.current_app && <span>Using <strong>{session.current_app}</strong></span>}
        </div>
        {apps.length > 0 && (
          <div className="member-focus-apps">
            <span>Allowed:</span>
            {apps.map((app) => <span key={app} className="member-focus-app">{app}</span>)}
          </div>
        )}
      </div>
      <dl className="member-focus-figures">
        <div>
          <dt>Score</dt>
          <dd className={`focus-score-value is-${band.band}`}>{formatScore(session.score)}</dd>
          <dd><span className={`focus-score-badge is-${band.band}`}>{band.label}</span></dd>
        </div>
        <div>
          <dt>Left</dt>
          <dd>{remaining === null ? "--" : formatCountdown(remaining)}</dd>
        </div>
        {session.deviations !== undefined && (
          <div>
            <dt>Deviations</dt>
            <dd>{session.deviations}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}


const FocusSessionsTable = memo(function FocusSessionsTable({ sessions }) {
  return (
    <div className="table-wrapper">
      <table>
        <thead>
          <tr>
            <th>Start</th>
            <th>Session</th>
            <th>Status</th>
            <th>Planned</th>
            <th>Allowed apps</th>
            <th>Deviations</th>
            <th>Off task</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {sessions.map((s) => {
            const apps = focusAllowedApps(s.allowed_apps);

            return (
              <tr key={s.session_id}>
                <td>{s.started_at ? String(s.started_at).replace("T", " ") : "--"}</td>
                <td>
                  <strong>{s.title || "Focus session"}</strong>
                  {s.goal && s.goal !== s.title && <small>{s.goal}</small>}
                </td>
                <td>
                  {s.status === "running"
                    ? <span className="session-badge is-live">Running</span>
                    : <span className="behavior-badge">{s.status}</span>}
                </td>
                <td>{s.planned_minutes ? `${s.planned_minutes} min` : "--"}</td>
                <td className="window-title" title={apps.join(", ")}>{apps.join(", ") || "--"}</td>
                <td>{s.deviations ?? 0}</td>
                <td>{formatMinutes(s.deviation_seconds)}</td>
                <td><FocusScore score={s.score} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
});


function MemberFocus({ memberId, liveFocus, onAuthError }) {
  const [data, setData] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(null);
  const [error, setError] = useState("");
  const [unsupported, setUnsupported] = useState(false);

  const runningRow = runningMemberSession(data, fetchedAt ?? 0);
  const liveSummary = liveFocus?.live ?? null;
  const running = Boolean(runningRow || liveSummary);

  usePolling(async ({ signal, isCurrent }) => {
    if (!getAdminKey()) return;

    try {
      const result = await loadMemberFocus(memberId, { signal });

      if (!isCurrent()) return;

      setData(result);
      setFetchedAt(Date.now());
      setError("");
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;
      if (onAuthError(err)) return;

      // No JSON error body: an API server without the focus endpoint.
      if (err.status === 404 && !err.fromApi) {
        setUnsupported(true);
        return;
      }

      setError(err.message);
    }
  }, memberFocusIntervalMs(running), { enabled: !unsupported, key: `focus:${memberId}` });

  const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
  const summary = data?.summary;

  return (
    <div className="session-table-card">
      <div className="table-header">
        <h3>Focus sessions</h3>
        <p>
          {unsupported
            ? "This API server doesn't collect focus sessions yet. Restart it with the current code."
            : summary
              ? <>
                  {summary.sessions} session{summary.sessions === 1 ? "" : "s"} shared
                  {" · "}focus score {formatScore(summary.focus_score)} (finished sessions, last 30 days)
                  {" · "}{summary.deviation_count ?? 0} deviations
                </>
              : "Timed sessions on chosen apps, shared by the member's collector."}
        </p>
      </div>

      {error && <p className="form-error table-error" role="alert">{error}</p>}

      {runningRow ? (
        <LiveFocusSession session={runningRow} receivedAt={fetchedAt} />
      ) : liveSummary ? (
        <LiveFocusSession session={liveSummary} receivedAt={null} />
      ) : null}

      {!unsupported && data && sessions.length === 0 && (
        <p className="section-sub member-focus-empty">
          No focus sessions shared yet. Members start one with
          {" "}<code>python collector/team_sync.py focus --minutes 25 --apps &quot;Code,Terminal&quot;</code>.
        </p>
      )}

      {sessions.length > 0 && <FocusSessionsTable sessions={sessions} />}
    </div>
  );
}


// --------------------------------------------------
// Charts (memoised so live polls don't redraw them)
// --------------------------------------------------

const MemberCharts = memo(function MemberCharts({ member }) {
  const topApps = member.top_applications.slice(0, 8);

  return (
    <div className="dashboard-grid">
      <div className="chart-card">
        <div className="chart-header">
          <div>
            <h3>Activity by hour</h3>
            <p>Minutes active and idle in each hour of the day (member's local time)</p>
          </div>
        </div>
        <div className="chart-container">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={member.hourly_activity}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="hour" tickFormatter={(h) => `${h}:00`} interval={2} />
              <YAxis unit=" m" />
              <Tooltip
                labelFormatter={(h) => `${h}:00 – ${h}:59`}
                formatter={(value, name) => [`${value} min`, name]}
              />
              <Legend />
              <Bar dataKey="active_minutes" name="Active" stackId="t" fill="#2a78d6" stroke="#ffffff" strokeWidth={2} />
              <Bar dataKey="idle_minutes" name="Idle" stackId="t" fill="#eb6834" stroke="#ffffff" strokeWidth={2} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="chart-card">
        <div className="chart-header">
          <div>
            <h3>Top applications</h3>
            <p>Active minutes per app</p>
          </div>
        </div>
        <div className="chart-container">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={topApps} layout="vertical" margin={{ left: 10, right: 16 }}>
              <CartesianGrid horizontal={false} />
              <XAxis type="number" unit=" m" />
              <YAxis type="category" dataKey="application" width={130} />
              <Tooltip formatter={(value) => [`${value} min`, "Active"]} />
              <Bar dataKey="active_minutes" fill="#6252df" radius={[0, 4, 4, 0]} barSize={16} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
});


// --------------------------------------------------
// Page
// --------------------------------------------------

function TeamMember() {
  const { memberId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const days = normalizeTeamRange(searchParams.get("days"));
  const rangeSearch = teamRangeSearch(days);

  const [member, setMember] = useState(null);
  const [loadedDays, setLoadedDays] = useState(null);
  const [checkedAt, setCheckedAt] = useState(null);
  const [error, setError] = useState(null);
  const [needsKey, setNeedsKey] = useState(() => !getAdminKey());
  const [keyRejected, setKeyRejected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [removing, setRemoving] = useState(false);
  // Errors from Remove / Revoke; unlike `error`, the next poll keeps them.
  const [actionError, setActionError] = useState("");
  const [announcement, setAnnouncement] = useState("");

  const [live, setLive] = useState(null);
  const [liveSupported, setLiveSupported] = useState(true);
  const liveEtagRef = useRef(null);
  // { id, status } last seen in /live, to announce online/offline changes.
  const liveStatusRef = useRef(null);


  // A 401 means the stored key was rejected (request() already forgot it),
  // or there was no key to send (locked in another tab). Stable, so the
  // memoised ActivityLog is not re-rendered by every page update.
  const handleAuthError = useCallback((err) => {
    if (err?.status !== 401) {
      return false;
    }

    setNeedsKey(true);
    setKeyRejected(!err.missingKey);
    return true;
  }, []);


  const runDetail = usePolling(async ({ signal, isCurrent }) => {
    if (!getAdminKey()) {
      setNeedsKey(true);
      return;
    }

    try {
      const data = await loadMemberDetail(memberId, { days, signal });

      if (!isCurrent()) return;

      setMember(data);
      setLoadedDays(days);
      setCheckedAt(Date.now());
      setError(null);
      setNeedsKey(false);
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;
      if (handleAuthError(err)) return;

      // The API answered past its key check (e.g. 404 for a member deleted
      // since), so leave the key form for the error state and back link.
      if (keyWasAccepted(err)) {
        setNeedsKey(false);
      }

      if (err.status === 404) {
        setMember(null);
      }

      setError(err.message);
    }
  }, DETAIL_REFRESH_MS, { enabled: !needsKey, key: `${memberId}:${days}` });


  function announceStatusChange(members) {
    const entry = members.find((m) => String(m.id) === String(memberId));

    if (!entry) {
      return;
    }

    const previous = liveStatusRef.current;

    if (
      previous
      && previous.id === entry.id
      && previous.status !== entry.status
      && (entry.status === "online" || entry.status === "offline")
    ) {
      setAnnouncement(`${entry.name} is now ${entry.status}.`);
    }

    liveStatusRef.current = { id: entry.id, status: entry.status };
  }


  const runLive = usePolling(async ({ signal, isCurrent }) => {
    if (!getAdminKey()) {
      return;
    }

    try {
      const result = await loadTeamLive({ etag: liveEtagRef.current, signal });

      if (!isCurrent()) return;

      if (!result.notModified && result.data) {
        liveEtagRef.current = result.etag;
        announceStatusChange(result.data.members || []);
        setLive(result.data);
      }

      setCheckedAt(Date.now());
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;
      if (handleAuthError(err)) return;

      // An API server from before the live endpoint: the detail poll still works.
      if (err.status === 404) {
        setLiveSupported(false);
      }
    }
  }, LIVE_REFRESH_MS, { enabled: !needsKey && liveSupported, key: "live" });


  function reloadAll() {
    return Promise.all([runDetail(true), liveSupported ? runLive(true) : null]);
  }


  function unlock() {
    setKeyRejected(false);
    setError(null);
    return runDetail(true);
  }


  // Hide everything shown with the key and go back to the key form.
  // Only touches state setters and refs, so any render's copy will do.
  function clearMemberView() {
    liveEtagRef.current = null;
    liveStatusRef.current = null;
    setMember(null);
    setLoadedDays(null);
    setLive(null);
    setCheckedAt(null);
    setError(null);
    setActionError("");
    setAnnouncement("");
    setKeyRejected(false);
    setNeedsKey(true);
  }


  function lock() {
    setAdminKey("");
    clearMemberView();
  }


  // Locked (or the key forgotten) in another tab: hide this member's data
  // here straight away, and show the plain key form, not "not accepted".
  useEffect(() => subscribeAdminKey((key) => {
    if (!key) {
      clearMemberView();
    }
  }), []);


  async function refresh() {
    if (refreshing) {
      return;
    }

    setRefreshing(true);

    try {
      await reloadAll();
    } finally {
      setRefreshing(false);
    }
  }


  function changeRange(value) {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);

      if (value === DEFAULT_TEAM_RANGE) {
        next.delete("days");
      } else {
        next.set("days", value);
      }

      return next;
    }, { replace: true });
  }


  // Only show data for the member in the URL (not the previous one while
  // the next loads).
  const shown = member && String(member.id) === String(memberId) ? member : null;
  const liveEntry = live?.members?.find((m) => String(m.id) === String(memberId)) ?? null;

  const status = shown
    ? (isInviteExpired({ ...shown, status: liveEntry?.status ?? shown.status })
      ? "expired"
      : liveEntry?.status ?? shown.status)
    : null;
  const invitePending = isPendingInvite(status);


  async function remove() {
    // Decided from what is on screen, which can be seconds old. Revoking
    // re-checks on the server (revokePendingInvite), so a member who joined
    // meanwhile is never deleted behind the "never joined" text.
    const revoking = invitePending;
    const { id, name } = shown;

    const confirmed = window.confirm(
      revoking
        ? `Revoke the invite for ${name}?\n\n` +
          "Their invite code stops working straight away. They never joined, " +
          "so no shared activity is deleted."
        : `Remove ${name} from the team?\n\n` +
          "This permanently deletes everything they have shared and revokes their collector."
    );

    if (!confirmed) {
      return;
    }

    setRemoving(true);
    setActionError("");

    try {
      if (revoking) {
        await revokePendingInvite(id);
      } else {
        await removeMember(id);
      }

      navigate(`/team${rangeSearch}`);
    } catch (err) {
      if (handleAuthError(err)) return;

      if (err.alreadyJoined) {
        setActionError(
          `${name} has already joined, so the invite was not revoked and nothing was deleted.`
        );
      } else if (err.inviteRotated) {
        setActionError(`Could not revoke the invite: ${err.message} Their old invite code no longer works.`);
      } else {
        setActionError(err.message);
      }

      // This page was out of date (joined, or removed elsewhere).
      if (err.alreadyJoined || err.status === 404) {
        await reloadAll();
      }
    } finally {
      setRemoving(false);
    }
  }


  const backLink = (
    <Link to={`/team${rangeSearch}`} className="back-link">
      <ArrowLeft size={14} aria-hidden="true" /> Team
    </Link>
  );

  if (needsKey) {
    return (
      <DashboardLayout>
        {backLink}
        <div className="page-header">
          <div>
            <span className="page-kicker">Team</span>
            <h1>Team member</h1>
            <p>Unlock team analytics to see this member's shared activity.</p>
          </div>
        </div>
        <AdminKeyForm rejected={keyRejected} onSaved={unlock} />
        {error && <p className="form-error" role="alert">{error}</p>}
      </DashboardLayout>
    );
  }

  if (error && !shown) {
    return (
      <DashboardLayout>
        {backLink}
        <div className="error-state" role="alert">
          <h2>Unable to load member</h2>
          <p>{error}</p>
          <p>
            <button type="button" className="range-selector" onClick={refresh} disabled={refreshing}>
              <RefreshCw size={13} className={refreshing ? "spin" : ""} aria-hidden="true" /> Try again
            </button>
          </p>
        </div>
      </DashboardLayout>
    );
  }

  if (!shown) {
    return (
      <DashboardLayout>
        {backLink}
        <div className="loading-state" role="status">Loading member...</div>
      </DashboardLayout>
    );
  }


  const { summary, comparison } = shown;
  const mix = Object.entries(shown.behavior_mix).sort((a, b) => b[1] - a[1]);
  const lastSeen = liveEntry?.last_seen ?? shown.last_seen;
  const expires = shown.invite_expires ? new Date(shown.invite_expires).toLocaleString() : "--";
  const rangeLoading = loadedDays !== days;
  const rangeLabel = TEAM_RANGES.find((range) => range.value === days)?.label;


  let intro;

  if (status === "expired") {
    intro = `Invite expired ${expires}. Issue a new code from the Members table on the Team page.`;
  } else if (status === "invited") {
    intro = `Invite pending until ${expires}.`;
  } else if (shown.consent_at) {
    intro = (
      <>
        Agreed to share on {new Date(shown.consent_at).toLocaleDateString()}.
        {" "}Last upload <RelativeTime value={lastSeen} />.
      </>
    );
  } else {
    intro = "Joined.";
  }


  return (
    <DashboardLayout>

      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      {backLink}

      <div className="page-header">
        <div>
          <span className="page-kicker">{STATUS_LABELS[status] || status}</span>
          <h1>{shown.name}{shown.simulated && <DemoBadge />}</h1>
          <p>
            {intro}
            {!invitePending && (
              <>
                {" "}Stats {describeTeamRange(loadedDays || days)}
                {shown.data_as_of && <>, newest sample {shown.data_as_of}</>}.
              </>
            )}
            {rangeLoading && <span className="range-loading"> Loading {rangeLabel}...</span>}
          </p>
        </div>
        <div className="dashboard-actions team-actions">
          {!invitePending && <RangeSelector value={days} onChange={changeRange} />}
          {!invitePending && <LiveStatus checkedAt={checkedAt} />}
          <button
            type="button"
            className="range-selector"
            onClick={refresh}
            disabled={refreshing}
            aria-busy={refreshing}
          >
            <RefreshCw size={13} className={refreshing ? "spin" : ""} aria-hidden="true" /> Refresh
          </button>
          <button
            type="button"
            className="range-selector"
            onClick={lock}
            title="Forget the admin key in this browser"
          >
            <Lock size={13} aria-hidden="true" /> Lock
          </button>
          <button
            type="button"
            className="range-selector danger-button"
            onClick={remove}
            disabled={removing}
            aria-busy={removing}
          >
            <Trash2 size={13} aria-hidden="true" /> {invitePending ? "Revoke invite" : "Remove and delete data"}
          </button>
        </div>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}
      {actionError && <p className="form-error" role="alert">{actionError}</p>}


      {/* Right now */}

      {!invitePending && liveEntry && (
        <RightNow
          entry={liveEntry}
          onlineWindow={formatWindow(live?.online_window_seconds)}
        />
      )}


      <div className="stats-grid">
        <div className="stat-card">
          <span>Active Time</span>
          <h2>{formatDuration(summary.active_seconds)}</h2>
          <p>{summary.active_hours_per_day} h per active day ({summary.days_active} days)</p>
        </div>
        <div className="stat-card">
          <span>Active Share</span>
          <h2>{summary.focus_score === null ? "--" : `${summary.focus_score}%`}</h2>
          <p>Active share of tracked time</p>
        </div>
        <div className="stat-card">
          <span>Sessions</span>
          <h2>{summary.sessions}</h2>
          <p>{summary.unusual_sessions} unusual for this member</p>
        </div>
        <div className="stat-card">
          <span>App Switches</span>
          <h2>{summary.application_switches.toLocaleString()}</h2>
          <p>{summary.window_switches.toLocaleString()} window switches</p>
        </div>
      </div>


      {/* Focus sessions */}

      {!invitePending && (
        <MemberFocus
          key={`focus-${shown.id}`}
          memberId={shown.id}
          liveFocus={liveEntry?.focus ?? null}
          onAuthError={handleAuthError}
        />
      )}


      {/* Comparison with team */}

      <div className="session-table-card">
        <div className="table-header">
          <h3>Compared with the rest of the team</h3>
          <p>
            {comparison.status === "insufficient_data"
              ? "Needs 3 sessions from this member and 5 from the rest of the team."
              : comparison.status === "differs"
                ? "Highlighted rows are 2 or more typical spreads away from the team."
                : "This member's typical session is in line with the team."}
          </p>
        </div>
        {comparison.features.length > 0 && (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>{shown.name}</th>
                  <th>Rest of team</th>
                  <th>Difference</th>
                </tr>
              </thead>
              <tbody>
                {comparison.features.map((f) => (
                  <tr key={f.feature} className={Math.abs(f.z_score) >= 2 ? "row-flagged" : ""}>
                    <td><strong>{f.label}</strong></td>
                    <td>{formatFeatureValue(f.feature, f.member_value)}</td>
                    <td>{formatFeatureValue(f.feature, f.team_value)}</td>
                    <td>
                      {Math.abs(f.z_score) >= 2
                        ? <span className="anomaly-score">{Math.abs(f.z_score)}× {f.direction}</span>
                        : `${Math.abs(f.z_score)}× ${f.direction}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>


      {/* Charts */}

      {summary.observations > 0 && <MemberCharts member={shown} />}


      {/* Behaviour mix */}

      {mix.length > 0 && (
        <div className="data-summary-card behavior-mix-card">
          <div>
            <h3>Behaviour mix</h3>
            <p>Share of this member's session time in each team-wide behaviour group.</p>
          </div>
          <div className="mix-list">
            {mix.map(([label, share]) => (
              <div key={label}>
                <span className="swatch" style={{ background: BEHAVIOR_COLORS[label] }} />
                {label} <strong>{Math.round(share * 100)}%</strong>
              </div>
            ))}
          </div>
        </div>
      )}


      {/* Top windows */}

      {shown.top_windows.length > 0 && (
        <div className="session-table-card">
          <div className="table-header">
            <h3>Top windows</h3>
            <p>Windows with the most active time.</p>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr><th>Application</th><th>Window title</th><th>Active</th></tr>
              </thead>
              <tbody>
                {shown.top_windows.map((w) => (
                  <tr key={`${w.application}-${w.window_title}`}>
                    <td><strong>{w.application}</strong></td>
                    <td className="window-title" title={w.window_title}>{w.window_title}</td>
                    <td>{w.active_minutes} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}


      {/* Sessions */}

      {shown.sessions.length > 0 && (
        <div className="session-table-card">
          <div className="table-header">
            <h3>Recent sessions</h3>
            <p>Latest 50 workspace sessions.</p>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Start</th>
                  <th>Length</th>
                  <th>Active</th>
                  <th>Idle share</th>
                  <th>Switches</th>
                  <th>Apps</th>
                  <th>Top app</th>
                  <th>Group</th>
                  <th className="sr-only-heading"><span className="sr-only">Unusual</span></th>
                </tr>
              </thead>
              <tbody>
                {shown.sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td>{s.start}</td>
                    <td>{s.duration_minutes} min</td>
                    <td>{s.active_minutes} min</td>
                    <td>{(s.idle_ratio * 100).toFixed(0)}%</td>
                    <td>{s.switches}</td>
                    <td>{s.unique_apps}</td>
                    <td>{s.top_application}</td>
                    <td>
                      {s.behavior_group ? (
                        <span className="behavior-badge">{s.behavior_group}</span>
                      ) : s.duration_minutes < 2 ? (
                        <small title="Sessions under 2 minutes are not grouped">Too short</small>
                      ) : (
                        <small title="Groups appear once the team has 8 sessions of 2+ minutes">Not grouped yet</small>
                      )}
                    </td>
                    <td>{s.unusual && <span className="anomaly-score">Unusual</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}


      {!invitePending && (
        <ActivityLog key={shown.id} memberId={shown.id} onAuthError={handleAuthError} />
      )}

    </DashboardLayout>
  );
}


export default TeamMember;
