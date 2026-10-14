import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Copy, RefreshCw, UserPlus } from "lucide-react";
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
  STATUS_LABELS,
  createInvite,
  formatDuration,
  formatFeatureValue,
  formatRelative,
  getAdminKey,
  loadTeamOverview,
} from "../data/team";


const REFRESH_MS = 30000;


function StatusPill({ status }) {
  return (
    <span className={`status-pill status-${status}`}>
      <span aria-hidden="true" />
      {STATUS_LABELS[status] || status}
    </span>
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


function InvitePanel({ onCreated }) {
  const [name, setName] = useState("");
  const [invite, setInvite] = useState(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");

    try {
      const result = await createInvite(name.trim());
      setInvite(result);
      setName("");
      setCopied(false);
      onCreated();
    } catch (err) {
      setError(err.message);
    }
  }

  const command = invite
    ? `python collector/team_sync.py join --server http://YOUR-IP:5000 --invite ${invite.invite_code}`
    : "";

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="settings-panel invite-panel">
      <div className="settings-panel-heading">
        <h2>Invite a member</h2>
        <p>
          Each invite works once and expires in 7 days. The member sees exactly
          what is shared, including window titles, and must agree before any data is sent.
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
        <button type="submit" className="settings-save-button" disabled={!name.trim()}>
          <UserPlus size={14} /> Create invite
        </button>
      </form>

      {error && <p className="form-error">{error}</p>}

      {invite && (
        <div className="invite-result">
          <p>
            Send <strong>{invite.name}</strong> this command to run on their machine
            from the project folder. Replace <code>YOUR-IP</code> with this computer's
            address. This code is shown only once.
          </p>
          <div className="invite-command">
            <code>{command}</code>
            <button type="button" className="range-selector" onClick={copy}>
              <Copy size={13} /> {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


function Team() {
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState(null);
  const [needsKey, setNeedsKey] = useState(!getAdminKey());
  const [keyRejected, setKeyRejected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    if (!getAdminKey()) {
      setNeedsKey(true);
      return;
    }

    setRefreshing(true);

    try {
      setOverview(await loadTeamOverview());
      setError(null);
      setNeedsKey(false);
    } catch (err) {
      if (err.status === 401) {
        setNeedsKey(true);
        setKeyRejected(true);
      } else {
        setError(err.message);
      }
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);


  if (needsKey) {
    return (
      <DashboardLayout>
        <div className="page-header">
          <div>
            <span className="page-kicker">Team</span>
            <h1>Team Analytics</h1>
            <p>Collect and compare activity from every member of your team.</p>
          </div>
        </div>
        <AdminKeyForm rejected={keyRejected} onSaved={refresh} />
      </DashboardLayout>
    );
  }

  if (error && !overview) {
    return (
      <DashboardLayout>
        <div className="error-state">
          <h2>Unable to load team analytics</h2>
          <p>{error}</p>
        </div>
      </DashboardLayout>
    );
  }

  if (!overview) {
    return (
      <DashboardLayout>
        <div className="loading-state">Loading team analytics...</div>
      </DashboardLayout>
    );
  }


  const { team, members, behavior_groups: behaviorGroups } = overview;
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
    <DashboardLayout>

      <div className="page-header">
        <div>
          <span className="page-kicker">Team</span>
          <h1>Team Analytics</h1>
          <p>
            Activity shared by {team.joined} of {team.members} members.
            Updated {formatRelative(overview.generated_at)}.
          </p>
        </div>
        <div className="dashboard-actions">
          <button className="range-selector" onClick={refresh} disabled={refreshing}>
            <RefreshCw size={13} className={refreshing ? "spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      {error && <p className="form-error">Last refresh failed: {error}</p>}


      {/* KPIs */}

      <div className="stats-grid">
        <div className="stat-card">
          <span>Members</span>
          <h2>{team.joined}<small> / {team.members}</small></h2>
          <p>Joined / invited</p>
        </div>
        <div className="stat-card">
          <span>Online Now</span>
          <h2>{team.online}</h2>
          <p>Uploaded in the last 2 minutes</p>
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
          <p>Open a member for their hourly pattern, apps, window titles and sessions.</p>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Status</th>
                <th>Last upload</th>
                <th>Active time</th>
                <th>Per day</th>
                <th>Focus</th>
                <th>Sessions</th>
                <th>Top app</th>
                <th>Main behaviour</th>
                <th>Vs team</th>
              </tr>
            </thead>
            <tbody>
              {members.length === 0 && (
                <tr>
                  <td colSpan="10" className="empty-table">
                    No members yet. Create an invite below.
                  </td>
                </tr>
              )}
              {members.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link to={`/team/${m.id}`} className="member-link">
                      <strong>{m.name}</strong>
                    </Link>
                  </td>
                  <td><StatusPill status={m.status} /></td>
                  <td>{m.status === "invited" ? "--" : formatRelative(m.last_seen)}</td>
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>


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
              <Link key={m.id} to={`/team/${m.id}`} className="recommendation differs-card">
                <span>{m.comparison.score}</span>
                <div>
                  <h4>{m.name}</h4>
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
            <p>Sessions from all members, grouped by K-Means on idle share, switching, apps used, CPU and memory.</p>
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


      <InvitePanel onCreated={refresh} />

    </DashboardLayout>
  );
}


export default Team;
