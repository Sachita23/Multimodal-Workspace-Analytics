import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
} from "recharts";

import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate, CollectingState } from "../components/PageState";
import { useAnalytics } from "../context/AnalyticsProvider";
import { MIN_ROWS_FOR_INSIGHTS } from "../data/analytics";
import { BRAND_COLOR, CHART_COLORS, NEUTRAL_COLOR, behaviorColor, formatDateTime, formatDuration, formatMinutes, truncate, isBehaviorLabel } from "../utils/format";


function hourlySubtitle(data) {
  if (data.hourlyMode === "timeline") {
    return "Active and idle minutes in each of the last 24 hours";
  }

  return `Average minutes per hour of day (avg per day, ${data.hourlyDays} ${data.hourlyDays === 1 ? "day" : "days"} with data)`;
}


function DashboardContent({ data }) {

  const { dataAsOf } = useAnalytics();

  const {
    metrics,
    hourlyActivity,
    behaviorDistribution,
    applicationUsage,
    resourceByHour
  } = data;


  /*
   * Only show the top 8 applications.
   * This keeps the chart readable.
   */

  const topApplications =
    applicationUsage
      .slice(0, 8)
      .map((item) => ({
        application: truncate(item.application, 18),
        duration: Math.round((item.duration / 60) * 10) / 10
      }));

  const topApplication = applicationUsage[0];
  const dominantBehavior = behaviorDistribution.find((item) => isBehaviorLabel(item.behavior));
  const behaviorTotal = behaviorDistribution.reduce((total, item) => total + item.seconds, 0);
  const trackedApplicationSeconds = applicationUsage.reduce((total, app) => total + app.duration, 0);
  const enoughData = metrics.totalRecords >= MIN_ROWS_FOR_INSIGHTS;


  return (

    <>

      {/* =========================
          HEADER
      ========================= */}

      <div className="page-header dashboard-header">

        <div>

          <span className="page-kicker">Workspace overview · {data.rangeLabel}</span>

          <h1>Workspace Overview</h1>

          <p>
            Monitor activity, attention patterns, and resource usage across your workspace.
          </p>

        </div>

      </div>


      {/* =========================
          PRIMARY KPI CARDS
      ========================= */}

      <div className="stats-grid">

        <div className="stat-card">
          <span>Active Time</span>
          <h2>{formatDuration(metrics.totalActiveSeconds)}</h2>
          <p>Recorded active time</p>
        </div>

        <div className="stat-card">
          <span>Focus Score</span>
          <h2>{metrics.focusScore}%</h2>
          <p>Share of tracked time that was active</p>
        </div>

        <div className="stat-card">
          <span>Sessions</span>
          <h2>{metrics.totalSessions}</h2>
          <p>Work sessions, split at breaks of 5+ min</p>
        </div>

        <div className="stat-card">
          <span>Anomalies</span>
          <h2>{metrics.anomalyCount}</h2>
          <p>
            Unusual episodes ({metrics.anomalousRecords.toLocaleString()} samples)
          </p>
        </div>

      </div>


      {/* =========================
          SECONDARY KPI CARDS
      ========================= */}

      <div className="stats-grid secondary-stats">

        <div className="stat-card">
          <span>Idle Time</span>
          <h2>{formatDuration(metrics.totalIdleSeconds)}</h2>
          <p>Recorded idle time</p>
        </div>

        <div className="stat-card">
          <span>App Switches</span>
          <h2>{metrics.applicationSwitches.toLocaleString()}</h2>
          <p>Application context switches</p>
        </div>

        <div className="stat-card">
          <span>Window Switches</span>
          <h2>{metrics.windowSwitches.toLocaleString()}</h2>
          <p>Window context switches</p>
        </div>

        <div className="stat-card">
          <span>Avg CPU</span>
          <h2>{metrics.avgCPU}%</h2>
          <p>Average CPU utilization</p>
        </div>

      </div>


      {/* =========================
          ROW 1 — ACTIVITY + BEHAVIOR
      ========================= */}

      <div className="dashboard-grid">


        {/* ACTIVITY TIMELINE */}

        <div className="chart-card large">

          <div className="chart-header">
            <div>
              <h3>Activity Timeline</h3>
              <p>{hourlySubtitle(data)}</p>
            </div>
          </div>

          <div className="chart-container">

            <ResponsiveContainer width="100%" height="100%">

              <LineChart
                data={hourlyActivity}
                margin={{ top: 10, right: 20, left: 0, bottom: 5 }}
              >

                <CartesianGrid strokeDasharray="3 3" />

                <XAxis dataKey="label" minTickGap={16} />

                <YAxis unit=" min" width={56} />

                <Tooltip
                  formatter={(value, name) => [formatMinutes(value), name]}
                />

                <Legend />

                <Line
                  type="monotone"
                  dataKey="active"
                  name="Active time"
                  stroke={CHART_COLORS[0]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />

                <Line
                  type="monotone"
                  dataKey="idle"
                  name="Idle time"
                  stroke={NEUTRAL_COLOR}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />

              </LineChart>

            </ResponsiveContainer>

          </div>

        </div>


        {/* BEHAVIOR */}

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
                  outerRadius={85}
                  innerRadius={50}
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
                    name
                  ]}
                />

                <Legend verticalAlign="bottom" />

              </PieChart>

            </ResponsiveContainer>

          </div>

        </div>

      </div>


      {/* =========================
          ROW 2 — APPLICATIONS + RESOURCES
      ========================= */}

      <div className="dashboard-grid">


        {/* APPLICATION USAGE */}

        <div className="chart-card large">

          <div className="chart-header">
            <div>
              <h3>Application Usage</h3>
              <p>Top applications by active time</p>
            </div>
          </div>

          <div className="chart-container">

            <ResponsiveContainer width="100%" height="100%">

              <BarChart
                data={topApplications}
                layout="vertical"
                margin={{ top: 5, right: 20, left: 10, bottom: 5 }}
              >

                <CartesianGrid strokeDasharray="3 3" />

                <XAxis type="number" unit=" min" />

                <YAxis type="category" dataKey="application" width={130} />

                <Tooltip formatter={(value) => [formatMinutes(value), "Active time"]} />

                <Bar
                  dataKey="duration"
                  name="Active time"
                  fill={BRAND_COLOR}
                  radius={[0, 4, 4, 0]}
                  isAnimationActive={false}
                />

              </BarChart>

            </ResponsiveContainer>

          </div>

        </div>


        {/* RESOURCE MONITORING */}

        <div className="chart-card">

          <div className="chart-header">
            <div>
              <h3>Resource Monitoring</h3>
              <p>Average CPU and memory utilization by hour</p>
            </div>
          </div>

          <div className="chart-container">

            <ResponsiveContainer width="100%" height="100%">

              <LineChart
                data={resourceByHour}
                margin={{ top: 10, right: 10, left: 0, bottom: 5 }}
              >

                <CartesianGrid strokeDasharray="3 3" />

                <XAxis dataKey="label" minTickGap={16} />

                <YAxis domain={[0, 100]} unit="%" />

                <Tooltip formatter={(value, name) => [`${value}%`, name]} />

                <Legend />

                <Line
                  type="monotone"
                  dataKey="cpu"
                  name="CPU"
                  stroke={CHART_COLORS[0]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />

                <Line
                  type="monotone"
                  dataKey="memory"
                  name="Memory"
                  stroke={CHART_COLORS[1]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />

              </LineChart>

            </ResponsiveContainer>

          </div>

        </div>


      </div>


      {/* =========================
          APPLICATION SUMMARY
      ========================= */}

      <div className="data-summary-card">

        <div>
          <h3>Application Activity Summary</h3>
          <p>
            {applicationUsage.length} applications detected
            across {metrics.totalSessions} sessions.
          </p>
        </div>

        <div className="summary-value">
          {formatDuration(trackedApplicationSeconds)}
          <span>tracked application time</span>
        </div>

      </div>


      {/* =========================
          INSIGHTS
      ========================= */}

      <section className="dashboard-insights">

        <div className="section-heading">
          <div>
            <span className="section-kicker">Insights</span>
            <h2>What your workspace activity is saying</h2>
          </div>
          <span className="insight-status">
            Data as of {formatDateTime(dataAsOf)}
          </span>
        </div>

        {enoughData ? (

          <div className="dashboard-insight-grid">
            <article className="dashboard-insight-card">
              <span>Productivity observation</span>
              <h3>{metrics.focusScore}% focus score</h3>
              <p>{metrics.focusScore >= 60 ? "Your active time is leading idle time, indicating a productive work rhythm." : "Idle time is significant; short focus blocks may help improve consistency."}</p>
            </article>
            <article className="dashboard-insight-card">
              <span>Behavioral pattern</span>
              <h3>{dominantBehavior?.behavior || "No pattern detected"}</h3>
              <p>{dominantBehavior ? `${formatDuration(dominantBehavior.seconds)} of tracked time matches this dominant workspace behavior.` : "Behavior classification will appear when data is available."}</p>
            </article>
            <article className="dashboard-insight-card">
              <span>Detected anomalies</span>
              <h3>{metrics.anomalyCount} unusual {metrics.anomalyCount === 1 ? "episode" : "episodes"}</h3>
              <p>{metrics.anomalyCount ? "Review unusual episodes to understand interruptions or unexpected resource use." : "No unusual activity was detected in this range."}</p>
            </article>
            <article className="dashboard-insight-card recommendation-card">
              <span>Recommendation</span>
              <h3>Protect your primary tool time</h3>
              <p>{topApplication ? `${topApplication.application} is your most-used application. Reserve uninterrupted blocks for focused work there.` : "Continue tracking application activity to unlock recommendations."}</p>
            </article>
          </div>

        ) : (

          <CollectingState records={metrics.totalRecords} />

        )}

      </section>

    </>

  );

}


function Dashboard() {

  return (

    <DashboardLayout>

      <AnalyticsGate
        loadingMessage="Loading workspace analytics..."
        errorTitle="Unable to load dataset"
      >
        {(data) => <DashboardContent data={data} />}
      </AnalyticsGate>

    </DashboardLayout>

  );

}


export default Dashboard;
