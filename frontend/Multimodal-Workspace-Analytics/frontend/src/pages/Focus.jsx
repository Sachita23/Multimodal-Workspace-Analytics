import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  FOCUS_DURATION_PRESETS,
  FOCUS_HISTORY_SECONDS,
  FOCUS_MAX_MINUTES,
  FOCUS_MAX_TEXT,
  FOCUS_RUNNING_POLL_SECONDS,
  INITIAL_ALERT_STATE,
  addCustomApp,
  alertMessage,
  allowFocusApp,
  appChoices,
  createFocusLivePoller,
  dashboardBrowserApp,
  describeFocusLive,
  extendFocusSession,
  finalSessionSummary,
  findFinishedSession,
  focusAlertStep,
  focusPageState,
  focusProgress,
  formatScore,
  hasApp,
  isLiveRunning,
  loadFocusApps,
  loadFocusLive,
  loadFocusSessions,
  loadFocusSummary,
  notifyDeviation,
  parseFocusTime,
  requestNotificationPermission,
  scoreBand,
  startFocusSession,
  stopFocusSession,
  toggleApp,
  validateFocusForm,
} from "../data/focus";
import {
  BRAND_COLOR,
  formatClock,
  formatCountdown,
  formatDateTime,
  formatDuration,
  formatTimeRange,
  truncate,
} from "../utils/format";
import { pollEvery } from "../utils/polling";


const CHART_SESSIONS = 12;

const NO_SESSIONS = [];

const DEFAULT_MINUTES = "25";

// After this long without a successful /live answer while a session runs,
// the last one is too old to show as "right now".
const LIVE_STALE_MS = FOCUS_RUNNING_POLL_SECONDS * 10 * 1000;


/**
 * Polls loader every `seconds` while the tab is visible (and once when it
 * becomes visible again). Keeps the last good value when a call fails.
 * reload() polls again now.
 */
function usePolled(loader, seconds) {
  const [state, setState] = useState({ value: null, error: null, loading: true, fetchedAt: 0 });
  const [version, setVersion] = useState(0);

  useEffect(() => pollEvery(loader, seconds, {
    onValue: (value) => setState({ value, error: null, loading: false, fetchedAt: Date.now() }),
    onError: (error) => setState((current) => ({ ...current, error, loading: false })),
  }), [loader, seconds, version]);

  const reload = useCallback(() => setVersion((value) => value + 1), []);

  return { ...state, reload };
}


/**
 * /api/focus/live, every second while a session runs (even in a hidden
 * tab), with the deviation alert worked out from each answer.
 *
 * `ended` is the last live payload of a session that has just finished
 * (seen running, then not), for the final score card.
 */
function useFocusLive() {
  const [state, setState] = useState({
    value: null, error: null, loading: true, fetchedAt: 0, alert: null, ended: null,
  });
  const pollerRef = useRef(null);

  useEffect(() => {
    let alertState = INITIAL_ALERT_STATE;
    let lastRunning = null;

    const poller = createFocusLivePoller(loadFocusLive, {
      onValue: (value) => {
        const now = Date.now();
        const step = focusAlertStep(alertState, value, now);
        let ended = null;

        alertState = step.state;

        if (step.fresh) notifyDeviation(step.fresh);

        if (isLiveRunning(value)) {
          lastRunning = value;
        } else if (lastRunning) {
          ended = lastRunning;
          lastRunning = null;
        }

        setState((current) => ({
          value,
          error: null,
          loading: false,
          fetchedAt: now,
          alert: step.state.alert,
          ended: ended || (isLiveRunning(value) ? null : current.ended),
        }));
      },
      onError: (error) => setState((current) => ({ ...current, error, loading: false })),
    });

    pollerRef.current = poller;

    return () => {
      poller.stop();
      if (pollerRef.current === poller) pollerRef.current = null;
    };
  }, []);

  const refresh = useCallback(() => pollerRef.current?.refresh(), []);
  const push = useCallback((value) => pollerRef.current?.push(value), []);
  const dismissEnded = useCallback(() => setState((current) => ({ ...current, ended: null })), []);

  return { ...state, refresh, push, dismissEnded };
}


// The app name of the browser showing this page (see dashboardBrowserApp).
function currentBrowserApp(runningApps) {
  const nav = typeof navigator === "undefined" ? null : navigator;

  return dashboardBrowserApp(nav?.userAgent, runningApps, { brave: Boolean(nav?.brave) });
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
      <li>Start a session above: give it a title, pick the apps you will work in and a duration, and press Start.</li>
      <li>Switching to any other app raises an alert here and on the desktop, and costs points. No app is ever closed.</li>
      <li>Team members run the same sessions with <code>python collector/team_sync.py focus --minutes 25 --apps &quot;Code,Terminal&quot;</code>.</li>
      <li>The desktop Focus Assistant (<code>python run_focus_assistant.py</code>, needs Tk) also shows up here.</li>
    </ol>
  );
}


function ScoreBadge({ score }) {
  const { band, label } = scoreBand(score);

  return <span className={`focus-score-badge is-${band}`}>{label}</span>;
}


// --------------------------------------------------
// Deviation alert
// --------------------------------------------------

function FocusAlert({ alert, canAllow, onAllow, busy }) {
  if (!alert) return null;

  return (
    <div className="focus-alert" role="alert">
      <div>
        <strong>Off task</strong>
        <p>{alertMessage(alert)}</p>
      </div>
      {canAllow && (
        <button type="button" className="range-selector" onClick={() => onAllow(alert.app)} disabled={busy}>
          Allow {truncate(alert.app, 28)} for this session
        </button>
      )}
    </div>
  );
}


// --------------------------------------------------
// Start form
// --------------------------------------------------

function StartSessionCard({ onStarted, disabledReason }) {
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [minutes, setMinutes] = useState(DEFAULT_MINUTES);
  const [customMinutes, setCustomMinutes] = useState(false);
  const [selected, setSelected] = useState([]);
  const [customApp, setCustomApp] = useState("");
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [apps, setApps] = useState({ value: null, error: null, loading: true });
  const [appsVersion, setAppsVersion] = useState(0);
  const submittingRef = useRef(false);
  const titleRef = useRef(null);
  const goalRef = useRef(null);
  const minutesRef = useRef(null);
  const firstPresetRef = useRef(null);
  const addAppRef = useRef(null);

  useEffect(() => {
    const controller = new AbortController();

    loadFocusApps({ signal: controller.signal })
      .then((value) => setApps({ value, error: null, loading: false }))
      .catch((error) => {
        if (error?.name !== "AbortError") setApps({ value: null, error, loading: false });
      });

    return () => controller.abort();
  }, [appsVersion]);

  const browser = useMemo(() => currentBrowserApp(apps.value?.running), [apps.value]);
  const choices = useMemo(() => appChoices(apps.value, selected, browser), [apps.value, selected, browser]);
  const browserAllowed = browser ? hasApp(selected, browser) : false;

  function addApp(event) {
    event.preventDefault();

    const result = addCustomApp(selected, customApp);

    setSelected(result.selected);
    setErrors((current) => ({ ...current, apps: result.error || undefined }));

    if (!result.error) setCustomApp("");
  }

  async function submit(event) {
    event.preventDefault();

    if (submittingRef.current) return;

    const { errors: found, payload } = validateFocusForm({ title, goal, minutes, apps: selected });

    setErrors(found);
    setServerError("");

    if (!payload) {
      // Move to the first field to fix (its error is read with it).
      const target = found.title ? titleRef.current
        : found.goal ? goalRef.current
          : found.minutes ? (customMinutes ? minutesRef.current : firstPresetRef.current)
            : found.apps ? addAppRef.current
              : null;

      target?.focus();
      return;
    }

    // Asked inside the click; used only if the user allows it.
    requestNotificationPermission();

    submittingRef.current = true;
    setSubmitting(true);

    try {
      const live = await startFocusSession(payload);
      onStarted(live);
    } catch (error) {
      setServerError(
        error.status === 409
          ? `${error.message} Stop the running session (or close the Focus Assistant window) first.`
          : error.message
      );
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  const appsUnavailable = apps.error?.status === 404;

  return (
    <section className="focus-start-card" aria-labelledby="focus-start-heading">
      <div className="chart-header">
        <div>
          <span className="section-kicker">New session</span>
          <h2 id="focus-start-heading">Start a focus session</h2>
          <p className="focus-form-note">
            Only the apps you choose count as on task. Switching to anything else raises an alert and lowers your score
            (−5 per switch, −6 per minute off task). Apps are never closed.
          </p>
          <p className="focus-form-note">
            Coming back to this dashboard (to stop or extend the session) is a switch too, unless you allow
            {browser ? ` ${browser}` : " this browser"}. Allowing it makes every tab in it count as on task.
          </p>
        </div>
      </div>

      <form onSubmit={submit} noValidate>
        <div className="focus-form-grid">
          <label className="settings-field">
            <span>Title</span>
            <input
              ref={titleRef}
              value={title}
              maxLength={FOCUS_MAX_TEXT}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="e.g. Write the project report"
              aria-invalid={Boolean(errors.title)}
              aria-describedby={errors.title ? "focus-title-error" : undefined}
            />
            {errors.title && <small id="focus-title-error" className="focus-field-error">{errors.title}</small>}
          </label>
          <label className="settings-field">
            <span>Goal (optional)</span>
            <input
              ref={goalRef}
              value={goal}
              maxLength={FOCUS_MAX_TEXT}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="e.g. Finish the results section"
              aria-invalid={Boolean(errors.goal)}
              aria-describedby={errors.goal ? "focus-goal-error" : undefined}
            />
            {errors.goal && <small id="focus-goal-error" className="focus-field-error">{errors.goal}</small>}
          </label>
        </div>

        <fieldset className="focus-fieldset" aria-describedby={errors.minutes ? "focus-minutes-error" : undefined}>
          <legend>Duration</legend>
          <div className="focus-presets">
            {FOCUS_DURATION_PRESETS.map((value, index) => (
              <button
                key={value}
                ref={index === 0 ? firstPresetRef : undefined}
                type="button"
                className="focus-chip"
                aria-pressed={!customMinutes && String(value) === minutes}
                onClick={() => {
                  setCustomMinutes(false);
                  setMinutes(String(value));
                }}
              >
                {value} min
              </button>
            ))}
            <label className="focus-custom-minutes">
              <span>Custom</span>
              <input
                ref={minutesRef}
                type="number"
                inputMode="numeric"
                min="1"
                max={FOCUS_MAX_MINUTES}
                step="1"
                value={customMinutes ? minutes : ""}
                placeholder="min"
                onChange={(event) => {
                  setCustomMinutes(true);
                  setMinutes(event.target.value);
                }}
                aria-label="Custom duration in minutes"
                aria-invalid={Boolean(errors.minutes)}
                aria-describedby={errors.minutes ? "focus-minutes-error" : undefined}
              />
            </label>
          </div>
          {errors.minutes && <small id="focus-minutes-error" className="focus-field-error">{errors.minutes}</small>}
        </fieldset>

        <fieldset className="focus-fieldset" aria-describedby={errors.apps ? "focus-apps-error" : undefined}>
          <legend>Allowed apps ({selected.length} selected)</legend>
          {apps.loading ? (
            <p className="focus-form-note">Looking for open apps…</p>
          ) : apps.error ? (
            <p className="focus-form-note">
              {appsUnavailable ? "This server can't list open apps." : `Couldn't list open apps: ${apps.error.message}`}
              {" "}Add the apps by name below.
            </p>
          ) : choices.length === 0 ? (
            <p className="focus-form-note">No apps found. Add them by name below.</p>
          ) : null}

          {choices.length > 0 && (
            <div className="focus-chip-list">
              {choices.map((choice) => (
                <button
                  key={choice.name.toLowerCase()}
                  type="button"
                  className="focus-chip"
                  aria-pressed={hasApp(selected, choice.name)}
                  onClick={() => setSelected((current) => toggleApp(current, choice.name))}
                >
                  {choice.name}
                  {choice.dashboard
                    ? <small>this dashboard</small>
                    : choice.running && <small>open</small>}
                </button>
              ))}
            </div>
          )}

          <div className="focus-add-app">
            <input
              ref={addAppRef}
              value={customApp}
              maxLength={FOCUS_MAX_TEXT}
              onChange={(event) => setCustomApp(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") addApp(event);
              }}
              placeholder="Add app by name, e.g. Code"
              aria-label="App name to allow"
              aria-invalid={Boolean(errors.apps)}
              aria-describedby={errors.apps ? "focus-apps-error" : undefined}
            />
            <button type="button" className="range-selector" onClick={addApp}>Add app</button>
            {!apps.loading && (
              <button type="button" className="range-selector" onClick={() => setAppsVersion((value) => value + 1)}>
                Refresh list
              </button>
            )}
          </div>
          {errors.apps && <small id="focus-apps-error" className="focus-field-error">{errors.apps}</small>}
          {browser && !browserAllowed && selected.length > 0 && (
            <p className="focus-form-note">{browser} (this dashboard) is not allowed: using Stop or +5 min here costs points.</p>
          )}
        </fieldset>

        <div className="focus-form-actions">
          <button
            type="submit"
            className="settings-save-button"
            disabled={submitting || Boolean(disabledReason)}
            aria-busy={submitting}
          >
            {submitting ? "Starting…" : "Start session"}
          </button>
          {disabledReason && <span className="focus-form-note">{disabledReason}</span>}
        </div>

        {Object.values(errors).some(Boolean) && (
          <p className="form-error" role="alert">Fix the highlighted fields to start the session.</p>
        )}
        {serverError && <p className="form-error" role="alert">{serverError}</p>}
      </form>
    </section>
  );
}


// --------------------------------------------------
// Running session
// --------------------------------------------------

function RunningSession({ live, fetchedAt, error, onLive, onStop, onGone, autoFocus, onFocused }) {
  const now = useNow(1000);
  const headingRef = useRef(null);

  // Started from the form: the Start button is gone, so focus lands here.
  useEffect(() => {
    if (autoFocus) {
      headingRef.current?.focus();
      onFocused?.();
    }
  }, [autoFocus, onFocused]);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  const elapsed = (now - fetchedAt) / 1000;
  const { remaining, planned, fraction } = focusProgress(live, elapsed);
  const state = describeFocusLive(live);
  const band = scoreBand(live.score);
  const controllable = live.source === "dashboard";
  const delayed = error && now - fetchedAt > LIVE_STALE_MS;
  const allowedApps = Array.isArray(live.allowed_apps) ? live.allowed_apps : [];
  const recent = Array.isArray(live.recent_deviations) ? live.recent_deviations : [];
  const browser = controllable ? currentBrowserApp([]) : null;
  const browserOffTask = browser && allowedApps.length > 0 && !hasApp(allowedApps, browser);

  async function act(action) {
    if (busy) return;

    setBusy(true);
    setActionError("");

    try {
      await action();
    } catch (err) {
      if (err.status === 409) {
        setActionError("This session is no longer running.");
        onGone();
      } else {
        setActionError(err.message);
      }
    } finally {
      setBusy(false);
    }
  }

  const extend = (minutes) => act(async () => onLive(await extendFocusSession(minutes)));
  const stop = () => act(async () => onStop(await stopFocusSession(), live));

  return (
    <section
      className={`focus-session${live.allowed === false ? " is-off-task" : ""}`}
      aria-label="Running focus session"
    >
      <div className="focus-session-top">
        <div className="focus-session-info">
          <span className="section-kicker">
            Session running{live.source === "assistant" ? " · Focus Assistant" : ""}
          </span>
          <h2 ref={headingRef} tabIndex={-1}>{live.title || "Focus session"}</h2>
          {live.goal && live.goal !== live.title && <p>{live.goal}</p>}
          <div className="focus-now">
            <span className={`focus-task-badge ${state.tone}`}>{state.label}</span>
            {state.note && <span>{state.note}</span>}
          </div>
        </div>

        <div className="focus-session-figures">
          <div className="focus-figure" aria-label={`Score ${formatScore(live.score)}, ${band.label}`}>
            <strong className={`focus-score-value is-${band.band}`}>{formatScore(live.score)}</strong>
            <span>score · <ScoreBadge score={live.score} /></span>
          </div>
          <div className="focus-figure" aria-label="Time remaining">
            <strong>{remaining === null ? "--" : formatCountdown(remaining)}</strong>
            <span>{planned ? `left of ${formatCountdown(planned)}` : "remaining"}</span>
          </div>
        </div>
      </div>

      {fraction !== null && (
        <div
          className="focus-progress"
          role="progressbar"
          aria-label="Session progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fraction * 100)}
        >
          <span style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
        </div>
      )}

      <div className="focus-session-meta">
        <span>Allowed:</span>
        {allowedApps.length === 0
          ? <span>--</span>
          : allowedApps.map((app) => <span key={app} className="focus-app-tag">{app}</span>)}
        {Number(live.deviations) > 0 && (
          <span className="focus-meta-note">
            {live.deviations} deviation{live.deviations === 1 ? "" : "s"} · {formatDuration(live.deviation_seconds)} off task
          </span>
        )}
      </div>

      {controllable ? (
        <div className="focus-controls">
          <button type="button" className="focus-control" onClick={() => extend(5)} disabled={busy}>+5 min</button>
          <button type="button" className="focus-control" onClick={() => extend(15)} disabled={busy}>+15 min</button>
          <button type="button" className="focus-control is-stop" onClick={stop} disabled={busy} aria-busy={busy}>
            Stop session
          </button>
          {browserOffTask && (
            <span className="focus-meta-note">Coming back to this page counts as a switch: {browser} isn&apos;t an allowed app.</span>
          )}
        </div>
      ) : (
        <p className="focus-meta-note focus-controls-note">
          This session is controlled from the Focus Assistant window.
        </p>
      )}

      {actionError && <p className="focus-action-error" role="alert">{actionError}</p>}
      {delayed && <p className="focus-action-error">Live status delayed: {error.message}</p>}

      {recent.length > 0 && (
        <div className="focus-deviations">
          <h3>Deviations this session</h3>
          <ul>
            {recent.map((item) => (
              <li key={`${item.at}-${item.app}`}>
                <span>{formatClock(parseFocusTime(item.at))}</span>
                <strong>{item.app}</strong>
                <span>{formatDuration(item.seconds)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}


function IdleCard({ live, error }) {
  if (error && !live) {
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

  return null;
}


function EndedCard({ summary, onDismiss, autoFocus, onFocused }) {
  const headingRef = useRef(null);
  const shown = Boolean(summary);

  // Stopped from here: the Stop button is gone, so focus lands here.
  useEffect(() => {
    if (shown && autoFocus) {
      headingRef.current?.focus();
      onFocused?.();
    }
  }, [shown, autoFocus, onFocused]);

  if (!summary) return null;

  const band = scoreBand(summary.score);

  return (
    <section className="focus-summary-card" aria-label="Session finished">
      <div>
        <span className="section-kicker">Session finished · {summary.status}</span>
        <h2 ref={headingRef} tabIndex={-1}>{summary.title}</h2>
        <p className="focus-form-note">
          {summary.recorded ? "Final score from the recorded session." : "Final score from the last live update; the recorded session appears below shortly."}
        </p>
      </div>
      <div className="focus-summary-score">
        <strong className={`focus-score-value is-${band.band}`}>{formatScore(summary.score)}</strong>
        <ScoreBadge score={summary.score} />
      </div>
      <dl className="focus-summary-stats">
        <div><dt>Deviations</dt><dd>{summary.deviations ?? 0}</dd></div>
        {summary.deviationSeconds !== null && (
          <div><dt>Off task</dt><dd>{formatDuration(summary.deviationSeconds)}</dd></div>
        )}
        {summary.actualSeconds !== null && (
          <div><dt>Length</dt><dd>{formatDuration(summary.actualSeconds)}</dd></div>
        )}
        {summary.adherencePct !== null && (
          <div><dt>Adherence</dt><dd>{Number(summary.adherencePct).toFixed(1)}%</dd></div>
        )}
      </dl>
      <button type="button" className="range-selector" onClick={onDismiss}>Dismiss</button>
    </section>
  );
}


// --------------------------------------------------
// History
// --------------------------------------------------

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

  const overall = summary?.focus_score;

  return (
    <>
      <div className="stats-grid focus-stats">
        <div className="stat-card">
          <span>Focus Score</span>
          <h2>{formatScore(overall)}</h2>
          <p>
            {overall === null || overall === undefined
              ? "Average of finished sessions (last 30 days)"
              : <><ScoreBadge score={overall} /> average of finished sessions, last 30 days</>}
          </p>
        </div>
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
          Run a focus session and its score, adherence, deviations and timeline appear here.
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
                    <th>Score</th>
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
                      <td>
                        {session.score === null || session.score === undefined
                          ? "--"
                          : <span className="focus-score-cell"><strong>{formatScore(session.score)}</strong> <ScoreBadge score={session.score} /></span>}
                      </td>
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
  const running = isLiveRunning(value);
  const tone = live.error ? "warn" : running ? (value.allowed === false ? "error" : "live") : "idle";
  const label = live.error
    ? "Focus API unavailable"
    : !value
      ? "Connecting…"
      : running
        ? (value.allowed === false ? "Session running · off task" : "Session running")
        : "No session running";

  return (
    <div className={`live-indicator live-${tone}`}>
      <span className="live-indicator-dot" aria-hidden="true" />
      <strong role="status">{label}</strong>
    </div>
  );
}


function Focus() {
  const live = useFocusLive();
  const summary = usePolled(loadFocusSummary, FOCUS_HISTORY_SECONDS);
  const sessions = usePolled(loadFocusSessions, FOCUS_HISTORY_SECONDS);
  const [stopResult, setStopResult] = useState(null);
  const [allowBusy, setAllowBusy] = useState(false);
  const [allowError, setAllowError] = useState("");
  // Where keyboard focus goes after Start / Stop swap the card.
  const [focusNext, setFocusNext] = useState(null);
  const focused = useCallback(() => setFocusNext(null), []);

  const { push, refresh, ended, dismissEnded } = live;
  const reloadSummary = summary.reload;
  const reloadSessions = sessions.reload;

  // A session ended (stopped here, completed, or stopped elsewhere): load
  // the recorded session for the final score card.
  useEffect(() => {
    if (ended) {
      reloadSummary();
      reloadSessions();
    }
  }, [ended, reloadSummary, reloadSessions]);

  const page = focusPageState({ live, summary, sessions });
  const running = isLiveRunning(live.value);
  const current = live.value;

  const finished = useMemo(() => {
    if (!ended) return null;

    const stopped = stopResult && stopResult.sessionId === (ended.session_id ?? null)
      ? stopResult.session
      : null;

    return finalSessionSummary(ended, stopped || findFinishedSession(sessions.value, ended));
  }, [ended, stopResult, sessions.value]);

  function started(value) {
    dismissEnded();
    setStopResult(null);
    setAllowError("");
    setFocusNext("running");
    push(value);
    reloadSummary();
    reloadSessions();
  }

  function stopped(result, lastLive) {
    setStopResult({ sessionId: lastLive.session_id ?? null, session: result?.session ?? null });
    setFocusNext("ended");
    refresh();
  }

  async function allow(app) {
    if (allowBusy) return;

    setAllowBusy(true);
    setAllowError("");

    try {
      push(await allowFocusApp(app));
    } catch (error) {
      setAllowError(error.status === 409 ? "This session is no longer running." : error.message);
      refresh();
    } finally {
      setAllowBusy(false);
    }
  }

  let body;

  if (page.state === "not-installed") {
    body = (
      <>
        <ErrorState title="Focus analytics aren't available on this server" error={page.error} />
        <EmptyState title="Using focus sessions">
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
        {running && (
          <FocusAlert
            alert={live.alert}
            canAllow={current.source === "dashboard"}
            onAllow={allow}
            busy={allowBusy}
          />
        )}
        {allowError && <p className="form-error" role="alert">{allowError}</p>}

        {running ? (
          <RunningSession
            live={current}
            fetchedAt={live.fetchedAt}
            error={live.error}
            onLive={push}
            onStop={stopped}
            onGone={refresh}
            autoFocus={focusNext === "running"}
            onFocused={focused}
          />
        ) : (
          <>
            <IdleCard live={current} error={live.error} />
            <EndedCard summary={finished} onDismiss={dismissEnded} autoFocus={focusNext === "ended"} onFocused={focused} />
            <StartSessionCard
              onStarted={started}
              disabledReason={live.error && !current ? "Waiting for the dashboard server…" : ""}
            />
          </>
        )}

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
          <p>Timed sessions on the apps you choose: an alert and lost points for every switch away, and a score for each session.</p>
        </div>
      </div>

      {body}
    </DashboardLayout>
  );
}

export default Focus;
