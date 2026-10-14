import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Trash2 } from "lucide-react";
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
  STATUS_LABELS,
  formatDuration,
  formatFeatureValue,
  formatRelative,
  loadMemberActivity,
  loadMemberDetail,
  removeMember,
} from "../data/team";


const PAGE_SIZE = 100;


function ActivityLog({ memberId }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadMore = useCallback(async (offset) => {
    setLoading(true);

    try {
      const page = await loadMemberActivity(memberId, offset, PAGE_SIZE);
      setRows((current) => (offset === 0 ? page.rows : [...current, ...page.rows]));
      setTotal(page.total);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [memberId]);

  useEffect(() => {
    loadMore(0);
  }, [loadMore]);

  return (
    <div className="session-table-card">
      <div className="table-header">
        <h3>Activity log</h3>
        <p>Every sample this member shared, newest first ({total.toLocaleString()} total).</p>
      </div>
      {error && <p className="form-error table-error">{error}</p>}
      <div className="table-wrapper activity-log">
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
            {rows.length === 0 && !loading && (
              <tr><td colSpan="7" className="empty-table">No activity shared yet.</td></tr>
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
      {rows.length < total && (
        <div className="table-footer">
          <button className="range-selector" disabled={loading} onClick={() => loadMore(rows.length)}>
            {loading ? "Loading..." : `Load ${Math.min(PAGE_SIZE, total - rows.length)} more`}
          </button>
        </div>
      )}
    </div>
  );
}


function TeamMember() {
  const { memberId } = useParams();
  const navigate = useNavigate();

  const [member, setMember] = useState(null);
  const [error, setError] = useState(null);
  const [needsKey, setNeedsKey] = useState(false);

  const load = useCallback(async () => {
    try {
      setMember(await loadMemberDetail(memberId));
      setError(null);
      setNeedsKey(false);
    } catch (err) {
      if (err.status === 401) {
        setNeedsKey(true);
      } else {
        setError(err.message);
      }
    }
  }, [memberId]);

  useEffect(() => {
    load();
  }, [load]);

  async function remove() {
    const confirmed = window.confirm(
      `Remove ${member.name} from the team?\n\n` +
      "This permanently deletes everything they have shared and revokes their collector."
    );

    if (!confirmed) {
      return;
    }

    try {
      await removeMember(member.id);
      navigate("/team");
    } catch (err) {
      setError(err.message);
    }
  }


  if (needsKey) {
    return (
      <DashboardLayout>
        <AdminKeyForm rejected onSaved={load} />
      </DashboardLayout>
    );
  }

  if (error && !member) {
    return (
      <DashboardLayout>
        <div className="error-state">
          <h2>Unable to load member</h2>
          <p>{error}</p>
          <p><Link to="/team" className="member-link">Back to team</Link></p>
        </div>
      </DashboardLayout>
    );
  }

  if (!member) {
    return (
      <DashboardLayout>
        <div className="loading-state">Loading member...</div>
      </DashboardLayout>
    );
  }


  const { summary, comparison } = member;
  const mix = Object.entries(member.behavior_mix).sort((a, b) => b[1] - a[1]);
  const topApps = member.top_applications.slice(0, 8);


  return (
    <DashboardLayout>

      <Link to="/team" className="back-link"><ArrowLeft size={14} /> Team</Link>

      <div className="page-header">
        <div>
          <span className="page-kicker">{STATUS_LABELS[member.status]}</span>
          <h1>{member.name}</h1>
          <p>
            {member.consent_at
              ? `Agreed to share on ${new Date(member.consent_at).toLocaleDateString()}. Last upload ${formatRelative(member.last_seen)}.`
              : `Invite pending until ${new Date(member.invite_expires).toLocaleDateString()}.`}
          </p>
        </div>
        <div className="dashboard-actions">
          <button className="range-selector danger-button" onClick={remove}>
            <Trash2 size={13} /> Remove and delete data
          </button>
        </div>
      </div>

      {error && <p className="form-error">{error}</p>}


      <div className="stats-grid">
        <div className="stat-card">
          <span>Active Time</span>
          <h2>{formatDuration(summary.active_seconds)}</h2>
          <p>{summary.active_hours_per_day} h per active day ({summary.days_active} days)</p>
        </div>
        <div className="stat-card">
          <span>Focus Score</span>
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
                  <th>{member.name}</th>
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

      {summary.observations > 0 && (
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
      )}


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

      {member.top_windows.length > 0 && (
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
                {member.top_windows.map((w) => (
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

      {member.sessions.length > 0 && (
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
                  <th />
                </tr>
              </thead>
              <tbody>
                {member.sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td>{s.start}</td>
                    <td>{s.duration_minutes} min</td>
                    <td>{s.active_minutes} min</td>
                    <td>{(s.idle_ratio * 100).toFixed(0)}%</td>
                    <td>{s.switches}</td>
                    <td>{s.unique_apps}</td>
                    <td>{s.top_application}</td>
                    <td>{s.behavior_group ? <span className="behavior-badge">{s.behavior_group}</span> : <small>Too short</small>}</td>
                    <td>{s.unusual && <span className="anomaly-score">Unusual</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}


      {member.status !== "invited" && <ActivityLog memberId={member.id} />}

    </DashboardLayout>
  );
}


export default TeamMember;
