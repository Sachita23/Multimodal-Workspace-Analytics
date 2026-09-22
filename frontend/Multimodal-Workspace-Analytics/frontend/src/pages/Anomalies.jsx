import { useEffect, useState } from "react";
import DashboardLayout from "../layouts/DashboardLayout";
import { loadAnalyticsData } from "../data/analytics";

function Anomalies() {
  const [analytics, setAnalytics] = useState(null);

  useEffect(() => { loadAnalyticsData().then(setAnalytics).catch(console.error); }, []);

  if (!analytics) return <DashboardLayout><div className="loading-state">Loading anomaly detection...</div></DashboardLayout>;

  const { anomalies, metrics, rows } = analytics;
  const highestScore = anomalies[0]?.anomaly_score ?? 0;
  const rate = rows.length ? (anomalies.length / rows.length) * 100 : 0;

  return (
    <DashboardLayout>
      <div className="page-header"><div><h1>Anomalies</h1><p>Review activity records flagged for unusual workspace behavior.</p></div></div>
      <div className="stats-grid">
        <div className="stat-card"><span>Flagged Records</span><h2>{metrics.anomalyCount}</h2><p>Potentially unusual events</p></div>
        <div className="stat-card"><span>Anomaly Rate</span><h2>{rate.toFixed(2)}%</h2><p>Of all activity records</p></div>
        <div className="stat-card"><span>Highest Score</span><h2>{Number(highestScore).toFixed(3)}</h2><p>Most unusual detected record</p></div>
        <div className="stat-card"><span>Reviewed Dataset</span><h2>{metrics.totalRecords.toLocaleString()}</h2><p>Total records analyzed</p></div>
      </div>
      <div className="session-table-card"><div className="table-header"><h3>Flagged Activity</h3><p>Records are ordered by anomaly score, highest first.</p></div><div className="table-wrapper"><table><thead><tr><th>Timestamp</th><th>Application</th><th>Behavior</th><th>Score</th><th>CPU</th><th>Memory</th><th>Idle Ratio</th></tr></thead><tbody>{anomalies.length ? anomalies.map((item, index) => <tr key={`${item.timestamp}-${index}`}><td>{item.timestamp}</td><td><strong>{item.application}</strong></td><td><span className="behavior-badge">{item.behavior_label}</span></td><td><span className="anomaly-score">{Number(item.anomaly_score).toFixed(3)}</span></td><td>{item.cpu_percent}%</td><td>{item.memory_percent}%</td><td>{(Number(item.idle_ratio) * 100).toFixed(1)}%</td></tr>) : <tr><td colSpan="7" className="empty-table">No anomalies were detected in this dataset.</td></tr>}</tbody></table></div></div>
    </DashboardLayout>
  );
}

export default Anomalies;
