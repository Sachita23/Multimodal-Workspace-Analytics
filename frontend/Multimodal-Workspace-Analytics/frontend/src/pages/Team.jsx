import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Copy, Lock, Pause, Play, RefreshCw, RotateCcw, UserPlus, X } from "lucide-react";
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
  BEHAVIOR_ORDER,
  DEFAULT_TEAM_RANGE,
  ExpiryText,
  RelativeTime,
  STATUS_LABELS,
  TEAM_RANGES,
  createInvite,
  describeTeamRange,
  formatDuration,
  formatFeatureValue,
  formatRelative,
  formatWindow,
  getAdminKey,
  isAbortError,
  isInviteExpired,
  isPendingInvite,
  keyWasAccepted,
  loadTeamLive,
  loadTeamOverview,
  normalizeTeamRange,
  reissueInvite,
  revokePendingInvite,
  setAdminKey,
  subscribeAdminKey,
  teamRangeSearch,
  useNow,
  usePolling,
} from "../data/team";
import "./team.css";


// The live view is a cheap SQL-only call; the overview runs the team ETL.
const LIVE_REFRESH_MS = 5000;
const OVERVIEW_REFRESH_MS = 30000;
const LIVE_STALE_MS = 20000;

const IS_MAC = typeof navigator !== "undefined"
  && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");


function StatusPill({ status }) {
  return (
    <span className={`status-pill status-${status}`}>
      <span aria-hidden="true" />
      {STATUS_LABELS[status] || status}
    </span>
  );
}


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


function NowCell({ current }) {
  if (!current) {
    return <span className="muted-text">--</span>;
  }

  return (
    <div className="now-cell">
      <div className="now-line">
        <ActivityBadge status={current.activity_status} />
        <strong title={current.application}>{current.application}</strong>
      </div>
      {current.window_title && (
        <span className="now-window" title={current.window_title}>
          {current.window_title}
        </span>
      )}
    </div>
  );
}


function ComparisonBadge({ comparison }) {
  if (comparison.status === "differs") {
    return <span className="anomaly-score">Differs</span>;
  }

  if (comparison.status === "typical") {
    return <span className="behavior-badge">Typical</span>;
  }

  return <span className="muted-badge">Not enough data</span>;
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


function LiveIndicator({ paused, checkedAt, error, onToggle }) {
  const now = useNow();

  let state = "is-live";
  let label = "Live";

  if (paused) {
    state = "is-paused";
    label = "Paused";
  } else if (error) {
    state = "is-stale";
    label = "Live view unavailable";
  } else if (!checkedAt) {
    state = "is-paused";
    label = "Connecting";
  } else if (now - checkedAt > LIVE_STALE_MS) {
    state = "is-stale";
    label = "Live, delayed";
  }

  return (
    <div className="team-live-indicator" title={error || undefined}>
      <span className={`team-live-dot ${state}`} aria-hidden="true" />
      <span>{label}</span>
      {!paused && checkedAt && (
        <span className="team-live-updated">{formatRelative(checkedAt)}</span>
      )}
      <button type="button" className="team-live-toggle" onClick={onToggle}>
        {paused ? <Play size={11} aria-hidden="true" /> : <Pause size={11} aria-hidden="true" />}
        {paused ? "Resume" : "Pause"}
      </button>
    </div>
  );
}


// --------------------------------------------------
// Invites
// --------------------------------------------------

function joinCommand(invite) {
  if (invite.join_command) {
    return invite.join_command;
  }

  // Older servers send no ready-made command.
  const server = invite.server_url || `http://YOUR-IP:${invite.port || "PORT"}`;
  return `python collector/team_sync.py join --server ${server} --invite ${invite.invite_code}`;
}


function InviteResult({ invite, resultRef }) {
  const [copyState, setCopyState] = useState("idle");
  const codeRef = useRef(null);

  const command = joinCommand(invite);
  const expires = invite.expires ? new Date(invite.expires) : null;
  const hasExpiry = expires && !Number.isNaN(expires.getTime());

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard unavailable");
      }

      await navigator.clipboard.writeText(command);
      setCopyState("copied");
    } catch {
      // No clipboard API on a non-secure origin (dashboard opened via a
      // LAN IP): select the command so it can be copied by hand.
      const selection = window.getSelection();
      const range = document.createRange();

      range.selectNodeContents(codeRef.current);
      selection.removeAllRanges();
      selection.addRange(range);
      setCopyState("manual");
    }
  }

  return (
    <div className="invite-result" ref={resultRef} tabIndex={-1}>
      <p>
        Send <strong>{invite.name}</strong> this command to run on their machine
        from the project folder. The code works once
        {hasExpiry ? <> and expires <strong>{expires.toLocaleString()}</strong></> : null}.
        It is shown only here; use <strong>New code</strong> in the Members table if it gets lost.
        {!invite.server_url && (
          <> The server only accepts connections from this computer right now:
          restart it with <code>WORKSPACE_API_HOST=0.0.0.0</code> so members can reach it,
          and replace <code>YOUR-IP</code> with this computer's address.</>
        )}
      </p>
      <div className="invite-command">
        <code ref={codeRef}>{command}</code>
        <button type="button" className="range-selector" onClick={copy}>
          <Copy size={13} aria-hidden="true" /> {copyState === "copied" ? "Copied" : "Copy"}
        </button>
      </div>
      <p className={copyState === "manual" ? "copy-hint" : "sr-only"} role="status">
        {copyState === "manual" && `Command selected. Press ${IS_MAC ? "Cmd" : "Ctrl"}+C to copy it.`}
        {copyState === "copied" && "Command copied to the clipboard."}
      </p>
    </div>
  );
}


function InvitePanel({ invite, onInvite, onAuthError, resultRef }) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // The ref also blocks a second submit that lands before the re-render
  // that disables the button.
  const submittingRef = useRef(false);

  async function submit(event) {
    event.preventDefault();

    const trimmed = name.trim();

    if (!trimmed || submittingRef.current) {
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    setError("");

    try {
      const result = await createInvite(trimmed);
      setName("");
      onInvite(result);
    } catch (err) {
      if (!onAuthError(err)) {
        setError(err.message);
      }
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <div className="settings-panel invite-panel">
      <div className="settings-panel-heading">
        <h2>Invite a member</h2>
        <p>
          Each invite code works once and expires after a few days. The member sees
          exactly what is shared, including window titles, and must agree before any
          data is sent.
        </p>
      </div>

      <form className="invite-form" onSubmit={submit}>
        <label className="settings-field">
          <span>Member name</span>
          <input
            value={name}
            maxLength={80}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Asha"
          />
        </label>
        <button
          type="submit"
          className="settings-save-button"
          disabled={submitting || !name.trim()}
          aria-busy={submitting}
        >
          <UserPlus size={14} aria-hidden="true" /> {submitting ? "Creating..." : "Create invite"}
        </button>
      </form>

      {error && <p className="form-error" role="alert">{error}</p>}

      {invite && (
        <InviteResult key={invite.invite_code} invite={invite} resultRef={resultRef} />
      )}
    </div>
  );
}


// --------------------------------------------------
// Overview sections that only change with the overview
// (memoised so the 5 s live poll and 1 s clock don't redraw charts)
// --------------------------------------------------

const TeamInsights = memo(function TeamInsights({ members, behaviorGroups, groupsCohort, rangeSearch }) {
  const withData = members.filter((m) => m.summary.observations > 0);

  const hoursChart = withData.map((m) => ({
    name: m.name,
    hours: m.summary.active_hours_per_day,
  }));

  const mixChart = withData
    .filter((m) => Object.keys(m.behavior_mix).length > 0)
    .map((m) => ({
      name: m.name,
      ...Object.fromEntries(
        Object.entries(m.behavior_mix).map(([label, share]) => [label, Math.round(share * 100)])
      ),
    }));

  const groupsPresent = BEHAVIOR_ORDER.filter((label) =>
    behaviorGroups.some((group) => group.label === label)
  );

  const differing = members.filter((m) => m.comparison.status === "differs");

  return (
    <>

      {/* Differs from team */}

      <div className="recommendations-card">
        <div className="section-heading">
          <div>
            <h2>Who works differently from the team</h2>
            <p className="section-sub">
              Each member's typical session compared with everyone else's. Differences
              of 2 or more typical spreads are listed with the features behind them.
            </p>
          </div>
        </div>

        {differing.length === 0 ? (
          <p className="section-sub">
            {members.some((m) => m.comparison.status === "typical")
              ? "Nobody stands out from the rest of the team right now."
              : "Not enough sessions yet. Each member needs 3 sessions, and the rest of the team 5, before they can be compared."}
          </p>
        ) : (
          <div className="recommendations-grid">
            {differing.map((m) => (
              <Link key={m.id} to={`/team/${m.id}${rangeSearch}`} className="recommendation differs-card">
                <span
                  role="img"
                  aria-label={`${m.comparison.score} typical spreads from the team`}
                  title="Distance from the team, in typical spreads"
                >
                  {m.comparison.score}×
                </span>
                <div>
                  <h4>{m.name}{m.simulated && <DemoBadge />}</h4>
                  {m.comparison.drivers.map((d) => (
                    <p key={d.feature}>
                      {d.label}: <strong>{formatFeatureValue(d.feature, d.member_value)}</strong>
                      {" "}vs team {formatFeatureValue(d.feature, d.team_value)}
                      {" "}({d.direction}, {Math.abs(d.z_score)}×)
                    </p>
                  ))}
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>


      {/* Charts */}

      {withData.length > 0 && (
        <div className="dashboard-grid">

          <div className="chart-card">
            <div className="chart-header">
              <div>
                <h3>Behaviour mix per member</h3>
                <p>Share of each member's session time in each team-wide behaviour group (%)</p>
              </div>
            </div>
            {mixChart.length === 0 ? (
              <p className="section-sub">
                Behaviour groups appear once the team has 8 sessions of 2+ minutes.
              </p>
            ) : (
              <div className="chart-container" style={{ height: Math.max(220, mixChart.length * 44 + 70) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={mixChart} layout="vertical" margin={{ left: 10, right: 16 }}>
                    <CartesianGrid horizontal={false} />
                    <XAxis type="number" domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow unit="%" />
                    <YAxis type="category" dataKey="name" width={130} />
                    <Tooltip formatter={(value) => `${value}%`} />
                    <Legend />
                    {groupsPresent.map((label) => (
                      <Bar
                        key={label}
                        dataKey={label}
                        stackId="mix"
                        fill={BEHAVIOR_COLORS[label]}
                        stroke="#ffffff"
                        strokeWidth={2}
                        barSize={22}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
            {mixChart.length > 0 && (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Member</th>
                    {groupsPresent.map((label) => <th key={label}>{label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {mixChart.map((row) => (
                    <tr key={row.name}>
                      <td>{row.name}</td>
                      {groupsPresent.map((label) => <td key={label}>{row[label] ?? 0}%</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="chart-card">
            <div className="chart-header">
              <div>
                <h3>Active hours per day</h3>
                <p>Average active time on days each member worked</p>
              </div>
            </div>
            <div className="chart-container" style={{ height: Math.max(220, hoursChart.length * 44 + 40) }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={hoursChart} layout="vertical" margin={{ left: 10, right: 16 }}>
                  <CartesianGrid horizontal={false} />
                  <XAxis type="number" unit=" h" />
                  <YAxis type="category" dataKey="name" width={130} />
                  <Tooltip formatter={(value) => [`${value} h`, "Active per day"]} />
                  <Bar dataKey="hours" fill="#6252df" radius={[0, 4, 4, 0]} barSize={18} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

        </div>
      )}


      {/* Behaviour groups */}

      {behaviorGroups.length > 0 && (
        <div className="session-table-card">
          <div className="table-header">
            <h3>Team behaviour groups</h3>
            <p>
              {groupsCohort === "demo" ? "Sessions from the demo members" : "Sessions from real members"}, grouped by
              K-Means on idle share, switching, apps used, CPU and memory. Demo and real members are grouped separately.
            </p>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Sessions</th>
                  <th>Members</th>
                  <th>Idle share</th>
                  <th>Switches / min</th>
                  <th>Apps / session</th>
                  <th>Avg length</th>
                </tr>
              </thead>
              <tbody>
                {behaviorGroups.map((group) => (
                  <tr key={group.label}>
                    <td>
                      <span className="swatch" style={{ background: BEHAVIOR_COLORS[group.label] }} />
                      <strong>{group.label}</strong>
                    </td>
                    <td>{group.sessions}</td>
                    <td>{group.members}</td>
                    <td>{(group.avg_idle_ratio * 100).toFixed(0)}%</td>
                    <td>{group.avg_switch_frequency}</td>
                    <td>{group.avg_unique_apps}</td>
                    <td>{group.avg_duration_minutes} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

    </>
  );
});


// --------------------------------------------------
// Page
// --------------------------------------------------

function displayStatus(member) {
  return isInviteExpired(member) ? "expired" : member.status;
}


function Team() {
  const [searchParams, setSearchParams] = useSearchParams();
  const days = normalizeTeamRange(searchParams.get("days"));
  const rangeSearch = teamRangeSearch(days);

  const [overview, setOverview] = useState(null);
  const [overviewDays, setOverviewDays] = useState(null);
  const [error, setError] = useState(null);

  const [live, setLive] = useState(null);
  const [liveCheckedAt, setLiveCheckedAt] = useState(null);
  const [liveError, setLiveError] = useState(null);
  const [liveSupported, setLiveSupported] = useState(true);

  const [needsKey, setNeedsKey] = useState(() => !getAdminKey());
  const [keyRejected, setKeyRejected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const [invite, setInvite] = useState(null);
  const [rowBusy, setRowBusy] = useState(null);
  const [actionError, setActionError] = useState("");
  const [announcement, setAnnouncement] = useState("");

  const liveEtagRef = useRef(null);
  const liveStatusRef = useRef(new Map());
  const inviteResultRef = useRef(null);
  const revealInviteRef = useRef(false);


  // A 401 means the stored key was rejected (request() already forgot it),
  // or there was no key to send (locked in another tab).
  function handleAuthError(err) {
    if (err?.status !== 401) {
      return false;
    }

    setNeedsKey(true);
    setKeyRejected(!err.missingKey);
    return true;
  }


  function announceStatusChanges(members) {
    const previous = liveStatusRef.current;

    if (previous.size > 0) {
      const changes = members
        .filter((m) => previous.has(m.id) && previous.get(m.id) !== m.status)
        .filter((m) => m.status === "online" || m.status === "offline")
        .map((m) => `${m.name} is now ${m.status}.`);

      if (changes.length > 0) {
        setAnnouncement(changes.join(" "));
      }
    }

    liveStatusRef.current = new Map(members.map((m) => [m.id, m.status]));
  }


  const runOverview = usePolling(async ({ signal, isCurrent }) => {
    if (!getAdminKey()) {
      setNeedsKey(true);
      return;
    }

    try {
      const data = await loadTeamOverview({ days, signal });

      if (!isCurrent()) return;

      setOverview(data);
      setOverviewDays(days);
      setError(null);
      setNeedsKey(false);
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;
      if (handleAuthError(err)) return;

      // The API answered past its key check, so leave the key form and
      // show the error state (with Try again) instead.
      if (keyWasAccepted(err)) {
        setNeedsKey(false);
      }

      setError(err.message);
    }
  }, OVERVIEW_REFRESH_MS, { enabled: !needsKey, paused, key: days });


  const runLive = usePolling(async ({ signal, isCurrent }) => {
    if (!getAdminKey()) {
      return;
    }

    try {
      const result = await loadTeamLive({ etag: liveEtagRef.current, signal });

      if (!isCurrent()) return;

      if (!result.notModified && result.data) {
        liveEtagRef.current = result.etag;
        announceStatusChanges(result.data.members || []);
        setLive(result.data);
      }

      setLiveCheckedAt(Date.now());
      setLiveError(null);
    } catch (err) {
      if (isAbortError(err) || !isCurrent()) return;
      if (handleAuthError(err)) return;

      if (err.status === 404) {
        // An API server from before the live endpoint existed.
        setLiveSupported(false);
        setLiveError("This API server has no live view. Restart it with the current code.");
        return;
      }

      setLiveError(err.message);
    }
  }, LIVE_REFRESH_MS, { enabled: !needsKey && liveSupported, paused, key: "live" });


  // Bring a freshly re-issued code into view (the panel is at the bottom).
  useEffect(() => {
    if (invite && revealInviteRef.current) {
      revealInviteRef.current = false;
      inviteResultRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      inviteResultRef.current?.focus({ preventScroll: true });
    }
  }, [invite]);


  const liveById = useMemo(
    () => new Map((live?.members || []).map((m) => [m.id, m])),
    [live]
  );

  // Overview rows with the fresher status, last upload and current
  // activity from the live view.
  const members = useMemo(() => (overview?.members || []).map((m) => {
    const now = liveById.get(m.id);

    return {
      ...m,
      status: now?.status ?? m.status,
      last_seen: now?.last_seen ?? m.last_seen,
      simulated: Boolean(m.simulated ?? now?.simulated),
      current: now?.current ?? null,
    };
  }), [overview, liveById]);


  // --------------------------------------------------
  // Actions
  // --------------------------------------------------

  // Reload both views now (after Refresh, unlocking or a change to the
  // member list).
  function reloadAll() {
    return Promise.all([runOverview(true), liveSupported ? runLive(true) : null]);
  }


  // Reload live as well as the overview: lock() cleared it, and while
  // paused nothing else would fetch it again until Resume.
  function unlock() {
    setKeyRejected(false);
    setError(null);
    return reloadAll();
  }


  // Hide everything shown with the key and go back to the key form.
  // Only touches state setters and refs, so any render's copy will do.
  function clearTeamView() {
    liveEtagRef.current = null;
    liveStatusRef.current = new Map();
    setOverview(null);
    setOverviewDays(null);
    setLive(null);
    setLiveCheckedAt(null);
    setLiveError(null);
    setInvite(null);
    setError(null);
    setActionError("");
    setKeyRejected(false);
    setNeedsKey(true);
  }


  function lock() {
    setAdminKey("");
    clearTeamView();
  }


  // Locked (or the key forgotten) in another tab: hide the team data here
  // straight away instead of on the next 30 s overview poll.
  useEffect(() => subscribeAdminKey((key) => {
    if (!key) {
      clearTeamView();
    }
  }), []);


  async function refresh() {
    if (refreshing) {
      return;
    }

    setRefreshing(true);

    try {
      await reloadAll();
      setAnnouncement("Team data refreshed.");
    } finally {
      setRefreshing(false);
    }
  }


  function togglePause() {
    setPaused((value) => !value);
    setAnnouncement(paused ? "Live updates resumed." : "Live updates paused.");
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


  async function revokeInvite(member) {
    const confirmed = window.confirm(
      `Revoke the invite for ${member.name}?\n\n` +
      "Their invite code stops working straight away. They never joined, " +
      "so no shared activity is deleted."
    );

    if (!confirmed) {
      return;
    }

    setRowBusy(member.id);
    setActionError("");

    const forgetShownCode = () => setInvite(
      (current) => (current?.member_id === member.id ? null : current)
    );

    try {
      // Never a plain DELETE: this row can be out of date (paused, or the
      // member joined while the dialog was open), and deleting a member who
      // joined would remove everything they shared.
      await revokePendingInvite(member.id);

      forgetShownCode();
      setAnnouncement(`Invite for ${member.name} revoked.`);
      await reloadAll();
    } catch (err) {
      if (handleAuthError(err)) return;

      if (err.alreadyJoined) {
        setActionError(
          `${member.name} has already joined, so their invite was not revoked and ` +
          "nothing was deleted. Open their page to remove them."
        );
      } else if (err.inviteRotated) {
        forgetShownCode();
        setActionError(
          `Could not revoke the invite for ${member.name}: ${err.message} ` +
          "Their old invite code no longer works."
        );
      } else {
        setActionError(`Could not revoke the invite for ${member.name}: ${err.message}`);
      }

      // The row was out of date (joined, or removed elsewhere): show the truth.
      if (err.alreadyJoined || err.status === 404) {
        await reloadAll();
      }
    } finally {
      setRowBusy(null);
    }
  }


  async function issueNewCode(member) {
    if (displayStatus(member) === "invited") {
      const confirmed = window.confirm(
        `Issue a new invite code for ${member.name}?\n\n` +
        "The code you sent before stops working."
      );

      if (!confirmed) {
        return;
      }
    }

    setRowBusy(member.id);
    setActionError("");

    try {
      const result = await reissueInvite(member.id);

      revealInviteRef.current = true;
      setInvite(result);
      setAnnouncement(`New invite code ready for ${member.name}.`);
      await reloadAll();
    } catch (err) {
      if (handleAuthError(err)) return;

      setActionError(`Could not issue a new code for ${member.name}: ${err.message}`);

      // 409: they joined since this row was drawn; 404: removed elsewhere.
      if (err.status === 409 || err.status === 404) {
        await reloadAll();
      }
    } finally {
      setRowBusy(null);
    }
  }


  function inviteCreated(result) {
    setInvite(result);
    reloadAll();
  }


  // --------------------------------------------------
  // Render
  // --------------------------------------------------

  const header = (
    <div>
      <span className="page-kicker">Team</span>
      <h1>Team Analytics</h1>
      <p>Collect and compare activity from every member of your team.</p>
    </div>
  );

  if (needsKey) {
    return (
      <DashboardLayout>
        <div className="page-header">{header}</div>
        <AdminKeyForm rejected={keyRejected} onSaved={unlock} />
        {error && <p className="form-error" role="alert">{error}</p>}
      </DashboardLayout>
    );
  }

  if (error && !overview) {
    return (
      <DashboardLayout>
        <div className="page-header">{header}</div>
        <div className="error-state" role="alert">
          <h2>Unable to load team analytics</h2>
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

  if (!overview) {
    return (
      <DashboardLayout>
        <div className="page-header">{header}</div>
        <div className="loading-state" role="status">Loading team analytics...</div>
      </DashboardLayout>
    );
  }


  const { team, behavior_groups: behaviorGroups } = overview;

  // Like the server's team totals: once any real member exists, the
  // KPIs describe real members only and demo members are noted apart.
  const demoMembers = members.filter((m) => m.simulated);
  const realOnly = (team.real_members ?? 0) > 0 && demoMembers.length > 0;
  const kpiMembers = realOnly ? members.filter((m) => !m.simulated) : members;
  const demoOnline = demoMembers.filter((m) => m.status === "online").length;

  const withData = kpiMembers.filter((m) => m.summary.observations > 0);
  const sharing = team.sharing ?? withData.length;
  const joined = kpiMembers.filter((m) => !isPendingInvite(displayStatus(m))).length;
  const online = kpiMembers.filter((m) => m.status === "online").length;
  const onlineWindow = formatWindow(live?.online_window_seconds);
  const rangeLoading = overviewDays !== days;
  const rangeLabel = TEAM_RANGES.find((range) => range.value === days)?.label;


  return (
    <DashboardLayout>

      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      <div className="page-header">
        <div>
          <span className="page-kicker">Team</span>
          <h1>Team Analytics</h1>
          <p>
            Activity shared by {sharing} of {kpiMembers.length} members {describeTeamRange(overviewDays || days)}.
            {realOnly && <> {demoMembers.length} demo members are listed below but kept out of the team figures.</>}
            {" "}Overview updated <RelativeTime value={overview.generated_at} />
            {overview.data_as_of && <>, newest sample {overview.data_as_of}</>}.
            {rangeLoading && <span className="range-loading"> Loading {rangeLabel}...</span>}
          </p>
        </div>
        <div className="dashboard-actions team-actions">
          <RangeSelector value={days} onChange={changeRange} />
          <LiveIndicator
            paused={paused}
            checkedAt={liveCheckedAt}
            error={liveError}
            onToggle={togglePause}
          />
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
        </div>
      </div>

      {error && <p className="form-error" role="alert">Last refresh failed: {error}</p>}
      {actionError && <p className="form-error" role="alert">{actionError}</p>}


      {/* KPIs */}

      <div className="stats-grid">
        <div className="stat-card">
          <span>Members</span>
          <h2>{joined}<small> / {kpiMembers.length}</small></h2>
          <p>Joined / invited{realOnly ? ` (plus ${demoMembers.length} demo)` : ""}</p>
        </div>
        <div className="stat-card">
          <span>Online Now</span>
          <h2>{online}</h2>
          <p>
            {onlineWindow ? `Uploaded in the last ${onlineWindow}` : "Uploaded recently"}
            {realOnly && demoOnline > 0 ? ` (plus ${demoOnline} demo)` : ""}
          </p>
        </div>
        <div className="stat-card">
          <span>Team Active Time</span>
          <h2>{formatDuration(team.active_seconds)}</h2>
          <p>Across {team.sessions} sessions</p>
        </div>
        <div className="stat-card">
          <span>Team Focus Score</span>
          <h2>{team.focus_score === null ? "--" : `${team.focus_score}%`}</h2>
          <p>Active share of tracked time</p>
        </div>
      </div>


      {/* Members */}

      <div className="session-table-card">
        <div className="table-header">
          <h3>Members</h3>
          <p>
            "Now" shows what online members are doing, refreshed every few seconds.
            Open a member for their hourly pattern, apps, window titles and sessions.
          </p>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Status</th>
                <th>Now</th>
                <th>Last upload</th>
                <th>Active time</th>
                <th>Per day</th>
                <th>Focus</th>
                <th>Sessions</th>
                <th>Top app</th>
                <th>Main behaviour</th>
                <th>Vs team</th>
                <th className="sr-only-heading"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {members.length === 0 && (
                <tr>
                  <td colSpan="12" className="empty-table">
                    No members yet. Create an invite below.
                  </td>
                </tr>
              )}
              {members.map((m) => {
                const status = displayStatus(m);
                const pending = isPendingInvite(status);
                const busy = rowBusy === m.id;

                return (
                  <tr key={m.id}>
                    <td>
                      <Link to={`/team/${m.id}${rangeSearch}`} className="member-link">
                        <strong>{m.name}</strong>
                      </Link>
                      {m.simulated && <DemoBadge />}
                    </td>
                    <td><StatusPill status={status} /></td>
                    <td>{pending ? <span className="muted-text">--</span> : <NowCell current={m.current} />}</td>
                    <td>
                      {pending
                        ? <ExpiryText value={m.invite_expires} />
                        : <RelativeTime value={m.last_seen} />}
                    </td>
                    <td>{m.summary.observations ? formatDuration(m.summary.active_seconds) : "--"}</td>
                    <td>{m.summary.observations ? `${m.summary.active_hours_per_day} h` : "--"}</td>
                    <td>{m.summary.focus_score === null ? "--" : `${m.summary.focus_score}%`}</td>
                    <td>{m.summary.sessions}</td>
                    <td>{m.summary.top_application || "--"}</td>
                    <td>
                      {m.dominant_behavior
                        ? <span className="behavior-badge">{m.dominant_behavior}</span>
                        : "--"}
                    </td>
                    <td><ComparisonBadge comparison={m.comparison} /></td>
                    <td>
                      {pending && (
                        <div className="row-actions">
                          <button
                            type="button"
                            className="row-action"
                            onClick={() => issueNewCode(m)}
                            disabled={busy}
                            aria-label={`New code for ${m.name}`}
                          >
                            <RotateCcw size={11} aria-hidden="true" /> New code
                          </button>
                          <button
                            type="button"
                            className="row-action is-danger"
                            onClick={() => revokeInvite(m)}
                            disabled={busy}
                            aria-label={`Revoke invite for ${m.name}`}
                          >
                            <X size={11} aria-hidden="true" /> Revoke
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>


      <TeamInsights
        members={overview.members}
        behaviorGroups={behaviorGroups}
        groupsCohort={overview.behavior_groups_cohort}
        rangeSearch={rangeSearch}
      />


      <InvitePanel
        invite={invite}
        onInvite={inviteCreated}
        onAuthError={handleAuthError}
        resultRef={inviteResultRef}
      />

    </DashboardLayout>
  );
}


export default Team;
