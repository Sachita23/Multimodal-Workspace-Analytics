import { useEffect, useState } from "react";

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
import { loadAnalyticsData } from "../data/analytics";


function formatMinutes(seconds) {

  const minutes = Math.round(seconds / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;

  return `${hours}h ${remainingMinutes}m`;
}


function formatDuration(seconds) {

  const minutes = Math.round(seconds / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;

  return `${hours}h ${remainingMinutes}m`;
}


function Dashboard() {

  const [analytics, setAnalytics] = useState(null);

  const [loading, setLoading] = useState(true);

  const [error, setError] = useState(null);


  useEffect(() => {

    loadAnalyticsData()

      .then((data) => {

        setAnalytics(data);

        setLoading(false);

      })

      .catch((err) => {

        console.error(err);

        setError(err.message);

        setLoading(false);

      });

  }, []);


  if (loading) {

    return (

      <DashboardLayout>

        <div className="loading-state">
          Loading workspace analytics...
        </div>

      </DashboardLayout>

    );

  }


  if (error) {

    return (

      <DashboardLayout>

        <div className="error-state">

          <h2>Unable to load dataset</h2>

          <p>{error}</p>

        </div>

      </DashboardLayout>

    );

  }


  const {
    metrics,
    hourlyActivity,
    behaviorDistribution,
    applicationUsage,
    resourceByHour
  } = analytics;


  /*
   * Only show the top 8 applications.
   * This keeps the chart readable.
   */

  const topApplications =
    applicationUsage
      .slice(0, 8)
      .map((item) => ({
        application:
          item.application.length > 18
            ? item.application.substring(0, 18) + "..."
            : item.application,

        duration:
          Math.round(item.duration / 60)
      }));

  const topApplication = applicationUsage[0];
  const dominantBehavior = behaviorDistribution[0];


  return (

    <DashboardLayout>

      {/* =========================
          HEADER
      ========================= */}

      <div className="page-header dashboard-header">

        <div>

          <span className="page-kicker">Workspace overview</span>

          <h1>Workspace Overview</h1>

          <p>
            Monitor activity, attention patterns, and resource usage across your workspace.
          </p>

        </div>

        <div className="dashboard-actions">
          <button className="range-selector">Last 24 hours ▾</button>
          <button className="date-selector">Dataset overview</button>
        </div>

      </div>


      {/* =========================
          PRIMARY KPI CARDS
      ========================= */}

      <div className="stats-grid">


        <div className="stat-card">

          <span>Active Time</span>

          <h2>
            {formatMinutes(metrics.totalActiveSeconds)}
          </h2>

          <p>
            Total recorded active time
          </p>

        </div>


        <div className="stat-card">

          <span>Focus Score</span>

          <h2>
            {metrics.focusScore}%
          </h2>

          <p>
            Derived from active vs idle time
          </p>

        </div>


        <div className="stat-card">

          <span>Sessions</span>

          <h2>
            {metrics.totalSessions}
          </h2>

          <p>
            Unique workspace sessions
          </p>

        </div>


        <div className="stat-card">

          <span>Anomalies</span>

          <h2>
            {metrics.anomalyCount}
          </h2>

          <p>
            Detected anomalous records
          </p>

        </div>

      </div>


      {/* =========================
          SECONDARY KPI CARDS
      ========================= */}

      <div className="stats-grid secondary-stats">


        <div className="stat-card">

          <span>Idle Time</span>

          <h2>
            {formatMinutes(metrics.totalIdleSeconds)}
          </h2>

          <p>
            Total recorded idle time
          </p>

        </div>


        <div className="stat-card">

          <span>App Switches</span>

          <h2>
            {metrics.applicationSwitches}
          </h2>

          <p>
            Application context switches
          </p>

        </div>


        <div className="stat-card">

          <span>Window Switches</span>

          <h2>
            {metrics.windowSwitches}
          </h2>

          <p>
            Window context switches
          </p>

        </div>


        <div className="stat-card">

          <span>Avg CPU</span>

          <h2>
            {metrics.avgCPU}%
          </h2>

          <p>
            Average CPU utilization
          </p>

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

              <h3>
                Activity Timeline
              </h3>

              <p>
                Active and idle time by hour
              </p>

            </div>

          </div>


          <div className="chart-container">

            <ResponsiveContainer
              width="100%"
              height="100%"
            >

              <LineChart
                data={hourlyActivity}
                margin={{
                  top: 10,
                  right: 20,
                  left: 0,
                  bottom: 5
                }}
              >

                <CartesianGrid
                  strokeDasharray="3 3"
                />

                <XAxis
                  dataKey="hour"
                  tickFormatter={(hour) => `${hour}:00`}
                />

                <YAxis />

                <Tooltip
                  formatter={(value) => [
                    `${value} min`
                  ]}
                  labelFormatter={(hour) =>
                    `${hour}:00`
                  }
                />

                <Legend />

                <Line
                  type="monotone"
                  dataKey="active"
                  name="Active Time"
                  stroke="#2563eb"
                  strokeWidth={3}
                  dot={false}
                />

                <Line
                  type="monotone"
                  dataKey="idle"
                  name="Idle Time"
                  stroke="#9ca3af"
                  strokeWidth={2}
                  dot={false}
                />

              </LineChart>

            </ResponsiveContainer>

          </div>

        </div>


        {/* BEHAVIOR */}

        <div className="chart-card">

          <div className="chart-header">

            <div>

              <h3>
                Behavior Distribution
              </h3>

              <p>
                Workspace behavior classification
              </p>

            </div>

          </div>


          <div className="chart-container pie-container">

            <ResponsiveContainer
              width="100%"
              height="100%"
            >

              <PieChart>

                <Pie
                  data={behaviorDistribution}
                  dataKey="count"
                  nameKey="behavior"
                  cx="50%"
                  cy="45%"
                  outerRadius={90}
                  innerRadius={50}
                  paddingAngle={3}
                  label={({ behavior, percent }) =>
                    `${behavior} ${(percent * 100).toFixed(0)}%`
                  }
                >

                  {behaviorDistribution.map(
                    (entry, index) => (

                      <Cell
                        key={`cell-${index}`}
                        fill={[
                          "#2563eb",
                          "#10b981",
                          "#f59e0b",
                          "#8b5cf6",
                          "#ef4444",
                          "#06b6d4"
                        ][index % 6]}
                      />

                    )
                  )}

                </Pie>

                <Tooltip />

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

              <h3>
                Application Usage
              </h3>

              <p>
                Top applications by active duration
              </p>

            </div>

          </div>


          <div className="chart-container">

            <ResponsiveContainer
              width="100%"
              height="100%"
            >

              <BarChart
                data={topApplications}
                layout="vertical"
                margin={{
                  top: 5,
                  right: 20,
                  left: 30,
                  bottom: 5
                }}
              >

                <CartesianGrid
                  strokeDasharray="3 3"
                />

                <XAxis
                  type="number"
                  unit=" min"
                />

                <YAxis
                  type="category"
                  dataKey="application"
                  width={130}
                />

                <Tooltip
                  formatter={(value) => [
                    `${value} min`,
                    "Active Time"
                  ]}
                />

                <Bar
                  dataKey="duration"
                  name="Active Time"
                  fill="#2563eb"
                  radius={[0, 5, 5, 0]}
                />

              </BarChart>

            </ResponsiveContainer>

          </div>

        </div>


        {/* RESOURCE MONITORING */}

        <div className="chart-card">

          <div className="chart-header">

            <div>

              <h3>
                Resource Monitoring
              </h3>

              <p>
                CPU and memory utilization
              </p>

            </div>

          </div>


          <div className="chart-container">

            <ResponsiveContainer
              width="100%"
              height="100%"
            >

              <LineChart
                data={resourceByHour}
                margin={{
                  top: 10,
                  right: 10,
                  left: 0,
                  bottom: 5
                }}
              >

                <CartesianGrid
                  strokeDasharray="3 3"
                />

                <XAxis
                  dataKey="hour"
                  tickFormatter={(hour) =>
                    `${hour}:00`
                  }
                />

                <YAxis
                  domain={[0, 100]}
                  unit="%"
                />

                <Tooltip
                  formatter={(value) => [
                    `${value}%`
                  ]}
                  labelFormatter={(hour) =>
                    `${hour}:00`
                  }
                />

                <Legend />

                <Line
                  type="monotone"
                  dataKey="cpu"
                  name="CPU"
                  stroke="#2563eb"
                  strokeWidth={2}
                  dot={false}
                />

                <Line
                  type="monotone"
                  dataKey="memory"
                  name="Memory"
                  stroke="#10b981"
                  strokeWidth={2}
                  dot={false}
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

          <h3>
            Application Activity Summary
          </h3>

          <p>
            {applicationUsage.length} applications detected
            across {metrics.totalSessions} sessions.
          </p>

        </div>

        <div className="summary-value">

          {formatDuration(
            applicationUsage.reduce(
              (sum, app) =>
                sum + app.duration,
              0
            )
          )}

          <span>
            tracked application time
          </span>

        </div>

      </div>

      <section className="dashboard-insights">
        <div className="section-heading">
          <div>
            <span className="section-kicker">AI Insights</span>
            <h2>What your workspace activity is saying</h2>
          </div>
          <span className="insight-status">Updated from current dataset</span>
        </div>

        <div className="dashboard-insight-grid">
          <article className="dashboard-insight-card">
            <span>Productivity observation</span>
            <h3>{metrics.focusScore}% focus score</h3>
            <p>{metrics.focusScore >= 60 ? "Your active time is leading idle time, indicating a productive work rhythm." : "Idle time is significant; short focus blocks may help improve consistency."}</p>
          </article>
          <article className="dashboard-insight-card">
            <span>Behavioral pattern</span>
            <h3>{dominantBehavior?.behavior || "No pattern detected"}</h3>
            <p>{dominantBehavior ? `${dominantBehavior.count.toLocaleString()} activity records match this dominant workspace behavior.` : "Behavior classification will appear when data is available."}</p>
          </article>
          <article className="dashboard-insight-card">
            <span>Detected anomalies</span>
            <h3>{metrics.anomalyCount} flagged events</h3>
            <p>{metrics.anomalyCount ? "Review unusual records to understand interruptions or unexpected resource use." : "No unusual activity was detected in this dataset."}</p>
          </article>
          <article className="dashboard-insight-card recommendation-card">
            <span>Recommendation</span>
            <h3>Protect your primary tool time</h3>
            <p>{topApplication ? `${topApplication.application} is your most-used application. Reserve uninterrupted blocks for focused work there.` : "Continue tracking application activity to unlock recommendations."}</p>
          </article>
        </div>
      </section>


    </DashboardLayout>

  );

}


export default Dashboard;
