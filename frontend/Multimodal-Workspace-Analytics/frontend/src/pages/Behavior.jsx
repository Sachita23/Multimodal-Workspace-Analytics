import { useEffect, useState } from "react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  PieChart,
  Pie,
  Cell,
} from "recharts";

import DashboardLayout from "../layouts/DashboardLayout";
import { loadAnalyticsData } from "../data/analytics";

function formatDuration(seconds) {
  if (!seconds || seconds < 0) return "0m";

  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }

  return `${minutes}m`;
}

function Behavior() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    loadAnalyticsData()
      .then((result) => {
        setData(result);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Failed to load behavior analytics.");
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <DashboardLayout>
        <div className="loading">Loading behavior analytics...</div>
      </DashboardLayout>
    );
  }

  if (error) {
    return (
      <DashboardLayout>
        <div className="error">{error}</div>
      </DashboardLayout>
    );
  }

  const {
    behaviorAnalytics = [],
    behaviorDistribution = [],
    metrics = {},
    sessionAnalytics = [],
  } = data;

  /*
   * Convert idle ratio into a clean percentage value.
   *
   * Example:
   * 0.25 -> 25
   * 0.63 -> 63
   *
   * This makes the Recharts bar chart easier to display.
   */
  const idleRatioData = behaviorAnalytics
    .map((item) => {
      const ratio = Number(item.avgIdleRatio);

      return {
        behavior: item.behavior || "Unknown",
        idleRatio: Number.isFinite(ratio)
          ? ratio <= 1
            ? ratio * 100
            : ratio
          : 0,
      };
    })
    .filter((item) => item.behavior);

  const avgSwitchFrequency =
    sessionAnalytics.length > 0
      ? sessionAnalytics.reduce(
          (sum, session) =>
            sum + (Number(session.switchFrequency) || 0),
          0
        ) / sessionAnalytics.length
      : 0;

  const clusterCount = new Set(
    sessionAnalytics
      .map((session) => session.cluster)
      .filter((cluster) => cluster !== undefined && cluster !== null)
  ).size;

  return (
    <DashboardLayout>
      <div className="page-header">
        <div>
          <h1>Behavior Analysis</h1>
          <p>
            Understand workspace behavior patterns and activity characteristics.
          </p>
        </div>
      </div>

      {/* SUMMARY CARDS */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">Behavior Types</div>
          <div className="stat-value">{behaviorAnalytics.length}</div>
          <div className="stat-sub">Detected patterns</div>
        </div>

        <div className="stat-card">
          <div className="stat-label">Focus Score</div>
          <div className="stat-value">
            {Number(metrics.focusScore || 0).toFixed(2)}%
          </div>
          <div className="stat-sub">Active vs idle time</div>
        </div>

        <div className="stat-card">
          <div className="stat-label">Avg Switch Frequency</div>
          <div className="stat-value">
            {avgSwitchFrequency.toFixed(2)}
          </div>
          <div className="stat-sub">Per session</div>
        </div>

        <div className="stat-card">
          <div className="stat-label">Behavioral Clusters</div>
          <div className="stat-value">{clusterCount}</div>
          <div className="stat-sub">Detected clusters</div>
        </div>
      </div>

      {/* CHARTS */}
      <div className="dashboard-grid">

        {/* BEHAVIOR DISTRIBUTION */}
        <div className="chart-card">
          <div className="chart-header">
            <div>
              <h3>Behavior Distribution</h3>
              <p>Distribution of detected workspace behaviors</p>
            </div>
          </div>

          <div className="chart-container">
            {behaviorDistribution.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={behaviorDistribution}
                    dataKey="count"
                    nameKey="behavior"
                    cx="50%"
                    cy="50%"
                    outerRadius={110}
                    label
                  >
                    {behaviorDistribution.map((entry, index) => (
                      <Cell key={`cell-${index}`} />
                    ))}
                  </Pie>

                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <div className="loading">
                No behavior distribution data available.
              </div>
            )}
          </div>
        </div>

        {/* IDLE RATIO */}
        <div className="chart-card">
          <div className="chart-header">
            <div>
              <h3>Idle Ratio by Behavior</h3>
              <p>Average idle percentage for each behavior type</p>
            </div>
          </div>

          <div className="chart-container">
            {idleRatioData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={idleRatioData}
                  margin={{
                    top: 10,
                    right: 20,
                    left: 10,
                    bottom: 20,
                  }}
                >
                  <CartesianGrid strokeDasharray="3 3" />

                  <XAxis
                    dataKey="behavior"
                    tick={{ fontSize: 12 }}
                    interval={0}
                    angle={-20}
                    textAnchor="end"
                    height={60}
                  />

                  <YAxis
                    domain={[0, 100]}
                    tickFormatter={(value) => `${value}%`}
                  />

                  <Tooltip
                    formatter={(value) => [
                      `${Number(value).toFixed(2)}%`,
                      "Idle Ratio",
                    ]}
                  />

                  <Legend />

                  <Bar
                    dataKey="idleRatio"
                    name="Idle Ratio"
                    radius={[6, 6, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="loading">
                No idle-ratio data available.
              </div>
            )}
          </div>
        </div>
      </div>

      {/* BEHAVIOR TABLE */}
      <div className="data-summary-card behavior-table">
        <div className="chart-header">
          <div>
            <h3>Behavior Details</h3>
            <p>Detailed statistics for each detected behavior</p>
          </div>
        </div>

        <div className="table-wrapper">
          <table className="session-table">
            <thead>
              <tr>
                <th>Behavior</th>
                <th>Records</th>
                <th>Active Time</th>
                <th>Idle Time</th>
                <th>Avg Idle Ratio</th>
                <th>App Switches</th>
                <th>Avg Apps</th>
              </tr>
            </thead>

            <tbody>
              {behaviorAnalytics.map((item, index) => {
                const ratio = Number(item.avgIdleRatio) || 0;

                const ratioPercent =
                  ratio <= 1 ? ratio * 100 : ratio;

                return (
                  <tr key={item.behavior || index}>
                    <td>
                      <strong>{item.behavior || "Unknown"}</strong>
                    </td>

                    <td>
                      {(Number(item.records) || 0).toLocaleString()}
                    </td>

                    <td>
                      {formatDuration(Number(item.active) || 0)}
                    </td>

                    <td>
                      {formatDuration(Number(item.idle) || 0)}
                    </td>

                    <td>
                      {ratioPercent.toFixed(2)}%
                    </td>

                    <td>
                      {(Number(item.switches) || 0).toLocaleString()}
                    </td>

                    <td>
                      {Number(item.avgApplications || 0).toFixed(2)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </DashboardLayout>
  );
}

export default Behavior;
