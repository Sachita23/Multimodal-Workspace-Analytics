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
import { AnalyticsGate, CollectingState } from "../components/PageState";
import { MIN_ROWS_FOR_INSIGHTS } from "../data/analytics";
import { BRAND_COLOR, behaviorColor, formatDuration, isBehaviorLabel } from "../utils/format";


function BehaviorContent({ data }) {
  const { behaviorAnalytics, behaviorDistribution, metrics } = data;

  const behaviorTotal = behaviorDistribution.reduce((total, item) => total + item.seconds, 0);

  // avgIdleRatio is already a 0-100 percentage (duration-weighted).
  const idleRatioData = behaviorAnalytics.map((item) => ({
    behavior: item.behavior,
    idleRatio: item.avgIdleRatio,
  }));

  return (
    <>
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
          <span>Behavior Types</span>
          <h2>{behaviorAnalytics.filter((item) => isBehaviorLabel(item.behavior)).length}</h2>
          <p>Detected patterns</p>
        </div>

        <div className="stat-card">
          <span>Focus Score</span>
          <h2>{metrics.focusScore.toFixed(1)}%</h2>
          <p>Active share of tracked time</p>
        </div>

        <div className="stat-card">
          <span>Avg Switch Frequency</span>
          <h2>{metrics.avgSwitchFrequency.toFixed(2)}</h2>
          <p>App switches / min, weighted by session length</p>
        </div>

        <div className="stat-card">
          <span>Behavioral Clusters</span>
          <h2>{metrics.clusterCount}</h2>
          <p>Clusters among labelled sessions</p>
        </div>
      </div>

      {metrics.totalRecords < MIN_ROWS_FOR_INSIGHTS && (
        <CollectingState records={metrics.totalRecords} />
      )}

      {/* CHARTS */}
      <div className="dashboard-grid">

        {/* BEHAVIOR DISTRIBUTION */}
        <div className="chart-card">
          <div className="chart-header">
            <div>
              <h3>Behavior Distribution</h3>
              <p>Share of tracked time by behavior pattern</p>
            </div>
          </div>

          <div className="chart-container pie-container">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={behaviorDistribution}
                  dataKey="seconds"
                  nameKey="behavior"
                  cx="50%"
                  cy="45%"
                  outerRadius={90}
                  innerRadius={48}
                  paddingAngle={2}
                  label={false}
                  isAnimationActive={false}
                >
                  {behaviorDistribution.map((entry) => (
                    <Cell key={entry.behavior} fill={behaviorColor(entry.behavior)} />
                  ))}
                </Pie>

                <Tooltip
                  formatter={(value, name) => [
                    `${formatDuration(value)} (${behaviorTotal ? ((value / behaviorTotal) * 100).toFixed(0) : 0}%)`,
                    name,
                  ]}
                />
                <Legend verticalAlign="bottom" />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* IDLE RATIO */}
        <div className="chart-card">
          <div className="chart-header">
            <div>
              <h3>Idle Ratio by Behavior</h3>
              <p>Idle share of tracked time for each behavior type</p>
            </div>
          </div>

          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={idleRatioData}
                margin={{ top: 10, right: 20, left: 0, bottom: 20 }}
              >
                <CartesianGrid strokeDasharray="3 3" />

                <XAxis
                  dataKey="behavior"
                  tick={{ fontSize: 11 }}
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
                  formatter={(value) => [`${Number(value).toFixed(1)}%`, "Idle ratio"]}
                />

                <Bar
                  dataKey="idleRatio"
                  name="Idle ratio"
                  fill={BRAND_COLOR}
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* BEHAVIOR TABLE */}
      <div className="session-table-card behavior-table">
        <div className="table-header">
          <div>
            <h3>Behavior Details</h3>
            <p>Detailed statistics for each detected behavior</p>
          </div>
        </div>

        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Behavior</th>
                <th>Sessions</th>
                <th>Samples</th>
                <th>Active Time</th>
                <th>Idle Time</th>
                <th>Idle Ratio</th>
                <th>App Switches</th>
                <th>Avg Apps</th>
              </tr>
            </thead>

            <tbody>
              {behaviorAnalytics.map((item) => (
                <tr key={item.behavior}>
                  <td>
                    <span className="swatch" style={{ background: behaviorColor(item.behavior) }} aria-hidden="true" />
                    <strong>{item.behavior}</strong>
                  </td>
                  <td>{item.sessions}</td>
                  <td>{item.records.toLocaleString()}</td>
                  <td>{formatDuration(item.active)}</td>
                  <td>{formatDuration(item.idle)}</td>
                  <td>{item.avgIdleRatio.toFixed(1)}%</td>
                  <td>{item.switches.toLocaleString()}</td>
                  <td>{item.avgApplications.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}


function Behavior() {
  return (
    <DashboardLayout>
      <AnalyticsGate
        loadingMessage="Loading behavior analytics..."
        errorTitle="Unable to load behavior analytics"
      >
        {(data) => <BehaviorContent data={data} />}
      </AnalyticsGate>
    </DashboardLayout>
  );
}

export default Behavior;
