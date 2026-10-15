import { memo, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import DashboardLayout from "../layouts/DashboardLayout";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import {
  FOCUS_HISTORY_SECONDS,
  FOCUS_LIVE_SECONDS,
  describeFocusLive,
  focusPageState,
  loadFocusLive,
  loadFocusSessions,
  loadFocusSummary,
  parseFocusTime,
} from "../data/focus";
import {
  BRAND_COLOR,
  formatCountdown,
  formatDateTime,
  formatDuration,
  formatTimeRange,
  truncate,
} from "../utils/format";
import { pollEvery } from "../utils/polling";


const CHART_SESSIONS = 12;

const NO_SESSIONS = [];

// After this long without a successful /live answer, the last one is too
// old to show as "right now".
const LIVE_STALE_MS = FOCUS_LIVE_SECONDS * 3 * 1000;


/**
 * Polls loader every `seconds` while the tab is visible (and once when it
 * becomes visible again). Keeps the last good value when a call fails.
 */
function usePolled(loader, seconds) {
  const [state, setState] = useState({ value: null, error: null, loading: true, fetchedAt: 0 });

  useEffect(() => pollEvery(loader, seconds, {
    onValue: (value) => setState({ value, error: null, loading: false, fetchedAt: Date.now() }),
    onError: (error) => setState((current) => ({ ...current, error, loading: false })),
  }), [loader, seconds]);

  return state;
}


function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);

  return now;
}


function HowToStart() {
  return (
    <ol className="focus-steps">
      <li>Start the Focus Assistant: <code>python run_focus_assistant.py</code></li>
      <li>Give the session a title and goal, choose the apps that count as on-task, and press Start.</li>
      <li>This page shows the running session live, and each finished session below.</li>
      <li>On macOS the assistant needs Tk: <code>brew install python-tk@3.14</code></li>
    </ol>
  );
}


// Only this card ticks every second (countdown, staleness); the charts and
// table below re-render only when the history itself changes.
function LiveCard({ live, error, fetchedAt }) {
  const now = useNow(1000);

  if (error && (!live || now - fetchedAt > LIVE_STALE_MS)) {
    return (
      <section className="focus-live-card idle">
        <div>
          <span className="section-kicker">Right now</span>
          <h2>Live status unavailable</h2>
          <p>{error.message}</p>
        </div>
      </section>
    );
  }

  if (!live || live.stale || !live.running) {
    return (
      <section className="focus-live-card idle">
        <div>
          <span className="section-kicker">Right now</span>
          <h2>No focus session is running</h2>
          <p>{!live || live.stale ? "The Focus Assistant is not running." : "The Focus Assistant is open, but no session has started."}</p>
        </div>
      </section>
    );
  }

  const remaining = live.remaining_s === null || live.remaining_s === undefined
    ? null
    : Math.max(0, live.remaining_s - (now - fetchedAt) / 1000);

  const state = describeFocusLive(live);

  return (
    <section className="focus-live-card running">
      <div>
        <span className="section-kicker">Session running</span>
        <h2>{live.title || "Focus session"}</h2>
        {live.goal && <p>{live.goal}</p>}
        <div className="focus-now">
          <span className={`focus-task-badge ${state.tone}`}>{state.label}</span>
          {state.note && <span>{state.note}</span>}
        </div>
      </div>
      <div className="focus-countdown" aria-label="Time remaining">
        <strong>{remaining === null ? "--" : formatCountdown(remaining)}</strong>
        <span>remaining</span>
      </div>
    </section>
  );
}


// Summary cards, charts and the sessions table: they change only when the
// 30 s history polls bring new data, so they are not redrawn on live polls.
const FocusHistory = memo(function FocusHistory({ summary, sessions }) {
  const chartData = useMemo(() => sessions
    .slice(0, CHART_SESSIONS)
    .filter((session) => session.adherence_pct !== null && session.adherence_pct !== undefined)
    .reverse()
    .map((session) => {
      const start = parseFocusTime(session.start);

      return {
        id: session.id,
        label: start ? start.toLocaleDateString([], { month: "short", day: "numeric" }) : "",
        name: `${session.title || "Session"} · ${formatDateTime(start)}`,
        adherence: Number(session.adherence_pct),
      };
    }), [sessions]);

  const deviationData = useMemo(() => (summary?.deviation_sources || [])
    .slice(0, 8)
    .map((item) => ({
      app: truncate(item.app, 20),
      minutes: Math.round((Number(item.seconds) / 60) * 10) / 10,
    })), [summary]);

  return (
    <>
      <div className="stats-grid">
        <div className="stat-card">
          <span>Sessions</span>
          <h2>{summary?.sessions ?? "--"}</h2>
          <p>{summary ? `${summary.completed ?? 0} completed` : "Focus history"}</p>
        </div>
        <div className="stat-card">
          <span>Adherence</span>
          <h2>{summary?.adherence_pct === null || summary?.adherence_pct === undefined ? "--" : `${Number(summary.adherence_pct).toFixed(1)}%`}</h2>
          <p>Time on allowed apps</p>
        </div>
        <div className="stat-card">
          <span>Focus Today</span>
          <h2>{summary ? formatDuration(summary.focus_seconds_today) : "--"}</h2>
          <p>{summary ? `${formatDuration(summary.focus_seconds_total)} in total` : "Time in focus sessions"}</p>
        </div>
        <div className="stat-card">
          <span>Deviations</span>
          <h2>{summary?.deviation_count ?? "--"}</h2>
          <p>Times you left the allowed apps</p>
        </div>
      </div>

      {sessions.length === 0 ? (
        <EmptyState title="No focus sessions yet">
          Run a focus session and its adherence, deviations and timeline appear here.
          <HowToStart />
        </EmptyState>
      ) : (
        <>
          <div className="dashboard-grid">
            <div className="chart-card large">
              <div className="chart-header">
                <div>
                  <h3>Adherence by Session</h3>
                  <p>Share of each session spent on allowed apps (last {Math.min(CHART_SESSIONS, sessions.length)} sessions)</p>
                </div>
              </div>
              <div className="chart-container">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="label" minTickGap={8} />
                    <YAxis domain={[0, 100]} unit="%" />
                    <Tooltip
                      labelFormatter={(_, payload) => payload?.[0]?.payload?.name || ""}
                      formatter={(value) => [`${Number(value).toFixed(1)}%`, "Adherence"]}
                    />
                    <Bar dataKey="adherence" name="Adherence" fill={BRAND_COLOR} radius={[4, 4, 0, 0]} isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div className="chart-card">
              <div className="chart-header">
                <div>
                  <h3>Deviation Sources</h3>
                  <p>Minutes spent in apps outside the session&apos;s allowed list</p>
                </div>
              </div>
              <div className="chart-container">
                {deviationData.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={deviationData} layout="vertical" margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis type="number" unit=" min" />
                      <YAxis type="category" dataKey="app" width={120} />
                      <Tooltip formatter={(value) => [`${value} min`, "Off-task time"]} />
                      <Bar dataKey="minutes" name="Off-task time" fill={BRAND_COLOR} radius={[0, 4, 4, 0]} isAnimationActive={false} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="empty-table chart-empty">No deviations recorded. Nice focus.</p>
                )}
              </div>
            </div>
          </div>

          <div className="session-table-card">
            <div className="table-header">
              <h3>Focus Sessions</h3>
              <p>Newest first</p>
            </div>
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Status</th>
                    <th>Planned</th>
                    <th>Actual</th>
                    <th>Adherence</th>
                    <th>Deviations</th>
                    <th>Switches</th>
                    <th>Longest Focus</th>
                    <th>Top Distraction</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => (
                    <tr key={session.id}>
                      <td>
                        <strong>{session.title || "Focus session"}</strong>
                        <small>{formatTimeRange(parseFocusTime(session.start), parseFocusTime(session.end))}</small>
                      </td>
                      <td><span className="behavior-badge">{session.status}</span></td>
                      <td>{session.planned_min ? `${session.planned_min} min` : "--"}</td>
                      <td>{formatDuration(session.actual_s)}</td>
                      <td>{session.adherence_pct === null || session.adherence_pct === undefined ? "--" : `${Number(session.adherence_pct).toFixed(1)}%`}</td>
                      <td>{session.deviations ?? 0}</td>
                      <td>{session.switches ?? 0}</td>
                      <td>{session.longest_focus_s ? formatDuration(session.longest_focus_s) : "--"}</td>
                      <td>{session.top_deviation_app || "--"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
});


function FocusStatusPill({ live }) {
  const value = live.value;
  const tone = live.error ? "warn" : value && !value.stale && value.running ? "live" : "idle";
  const label = live.error
    ? "Focus API unavailable"
    : !value
      ? "Connecting…"
      : value.stale
        ? "Assistant not running"
        : value.running
          ? "Session running"
          : "Assistant idle";

  return (
    <div className={`live-indicator live-${tone}`}>
      <span className="live-indicator-dot" aria-hidden="true" />
      <strong role="status">{label}</strong>
    </div>
  );
}


function Focus() {
  const live = usePolled(loadFocusLive, FOCUS_LIVE_SECONDS);
  const summary = usePolled(loadFocusSummary, FOCUS_HISTORY_SECONDS);
  const sessions = usePolled(loadFocusSessions, FOCUS_HISTORY_SECONDS);

  const page = focusPageState({ live, summary, sessions });

  let body;

  if (page.state === "not-installed") {
    body = (
      <>
        <ErrorState title="Focus analytics aren't available on this server" error={page.error} />
        <EmptyState title="Using the Focus Assistant">
          <HowToStart />
        </EmptyState>
      </>
    );
  } else if (page.state === "loading") {
    body = <LoadingState message="Loading focus sessions..." />;
  } else if (page.state === "error") {
    body = <ErrorState title="Unable to load focus sessions" error={page.error} />;
  } else {
    body = (
      <>
        <LiveCard live={live.value} error={live.error} fetchedAt={live.fetchedAt} />
        <FocusHistory summary={summary.value} sessions={sessions.value || NO_SESSIONS} />
      </>
    );
  }

  return (
    <DashboardLayout>
      <div className="page-toolbar">
        <FocusStatusPill live={live} />
      </div>

      <div className="page-header">
        <div>
          <h1>Focus</h1>
          <p>Focus Assistant sessions: what you planned, how closely you stuck to it, and what pulled you away.</p>
        </div>
      </div>

      {body}
    </DashboardLayout>
  );
}

export default Focus;
