import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { X } from "lucide-react";

import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";

import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate, EmptyState } from "../components/PageState";
import { searchActivity } from "../data/analytics";
import { BRAND_COLOR, formatDateTime, formatDuration, formatMinutes, truncate } from "../utils/format";


function ApplicationsContent({ data }) {

  const [searchParams, setSearchParams] = useSearchParams();
  const query = (searchParams.get("q") || "").trim();

  const result = useMemo(
    () => (query ? searchActivity(data.rows, query) : null),
    [data.rows, query]
  );

  const applications = result ? result.applications : data.applicationUsage;
  const windows = result ? result.windows : [];


  const totalApplications = applications.length;

  const totalUsage = applications.reduce((sum, app) => sum + app.duration, 0);

  const totalSwitches = applications.reduce((sum, app) => sum + app.switches, 0);

  const topApplication = applications.length > 0 ? applications[0] : null;


  const chartData =
    applications
      .slice(0, 10)
      .map((app) => ({
        application: truncate(app.application, 20),
        duration: Math.round((app.duration / 60) * 10) / 10
      }));


  return (

    <>

      {/* =========================
          HEADER
      ========================= */}

      <div className="page-header">

        <div>
          <h1>Applications</h1>
          <p>Analyze application usage and context switching.</p>
        </div>

      </div>


      {query && (
        <div className="filter-bar" role="status">
          <span>
            Showing activity whose application or window title contains <strong>“{query}”</strong>
            {" "}({result.rows.length.toLocaleString()} samples)
          </span>
          <button type="button" className="range-selector" onClick={() => setSearchParams({})}>
            <X size={13} aria-hidden="true" /> Clear search
          </button>
        </div>
      )}


      {query && applications.length === 0 ? (

        <EmptyState title={`Nothing matches “${query}”`}>
          No application or window title in the {data.rangeLabel.toLowerCase()} contains this text.
          Try a shorter search or a longer time range.
        </EmptyState>

      ) : (

        <>

          {/* =========================
              SUMMARY CARDS
          ========================= */}

          <div className="stats-grid">

            <div className="stat-card">
              <span>Applications</span>
              <h2>{totalApplications}</h2>
              <p>Unique applications detected</p>
            </div>

            <div className="stat-card">
              <span>Most Used</span>
              <h2>{topApplication ? topApplication.application : "--"}</h2>
              <p>Highest active duration</p>
            </div>

            <div className="stat-card">
              <span>Tracked Time</span>
              <h2>{formatDuration(totalUsage)}</h2>
              <p>Total active application time</p>
            </div>

            <div className="stat-card">
              <span>App Switches</span>
              <h2>{totalSwitches.toLocaleString()}</h2>
              <p>Switches into these applications</p>
            </div>

          </div>


          {/* =========================
              CHART
          ========================= */}

          <div className="chart-card application-chart">

            <div className="chart-header">
              <div>
                <h3>Application Usage</h3>
                <p>Top applications by active time</p>
              </div>
            </div>

            <div className="application-chart-container">

              <ResponsiveContainer width="100%" height="100%">

                <BarChart
                  data={chartData}
                  margin={{ top: 10, right: 30, left: 10, bottom: 60 }}
                >

                  <CartesianGrid strokeDasharray="3 3" />

                  <XAxis
                    dataKey="application"
                    angle={-35}
                    textAnchor="end"
                    interval={0}
                  />

                  <YAxis unit=" min" />

                  <Tooltip formatter={(value) => [formatMinutes(value), "Active time"]} />

                  <Bar
                    dataKey="duration"
                    name="Active time"
                    fill={BRAND_COLOR}
                    radius={[4, 4, 0, 0]}
                    isAnimationActive={false}
                  />

                </BarChart>

              </ResponsiveContainer>

            </div>

          </div>


          {/* =========================
              APPLICATION TABLE
          ========================= */}

          <div className="session-table-card application-table">

            <div className="table-header">
              <div>
                <h3>Application Details</h3>
                <p>Detailed application-level analytics</p>
              </div>
            </div>

            <div className="table-wrapper">

              <table>

                <thead>
                  <tr>
                    <th>Rank</th>
                    <th>Application</th>
                    <th>Active Time</th>
                    <th>Samples</th>
                    <th>Switches</th>
                    <th>Usage %</th>
                    <th>Last Used</th>
                  </tr>
                </thead>

                <tbody>

                  {applications.map((app, index) => {

                    const percentage = totalUsage > 0 ? (app.duration / totalUsage) * 100 : 0;

                    return (

                      <tr key={app.application}>
                        <td><strong>#{index + 1}</strong></td>
                        <td><strong>{app.application}</strong></td>
                        <td>{formatDuration(app.duration)}</td>
                        <td>{app.records.toLocaleString()}</td>
                        <td>{app.switches}</td>
                        <td>{percentage.toFixed(1)}%</td>
                        <td>{formatDateTime(new Date(app.lastSeen))}</td>
                      </tr>

                    );

                  })}

                </tbody>

              </table>

            </div>

          </div>


          {/* =========================
              MATCHING WINDOWS (search only)
          ========================= */}

          {query && (

            <div className="session-table-card">

              <div className="table-header">
                <div>
                  <h3>Matching Windows</h3>
                  <p>Window titles that match the search, by active time (top 100)</p>
                </div>
              </div>

              <div className="table-wrapper">

                <table>

                  <thead>
                    <tr>
                      <th>Window</th>
                      <th>Application</th>
                      <th>Active Time</th>
                      <th>Samples</th>
                      <th>Last Seen</th>
                    </tr>
                  </thead>

                  <tbody>

                    {windows.map((item) => (
                      <tr key={item.key}>
                        <td className="window-title" title={item.window_title}><strong>{item.window_title}</strong></td>
                        <td>{item.application}</td>
                        <td>{formatDuration(item.duration)}</td>
                        <td>{item.records.toLocaleString()}</td>
                        <td>{formatDateTime(new Date(item.lastSeen))}</td>
                      </tr>
                    ))}

                  </tbody>

                </table>

              </div>

            </div>

          )}

        </>

      )}

    </>

  );

}


function Applications() {

  return (

    <DashboardLayout>

      <AnalyticsGate
        loadingMessage="Loading application analytics..."
        errorTitle="Unable to load applications"
      >
        {(data) => <ApplicationsContent data={data} />}
      </AnalyticsGate>

    </DashboardLayout>

  );

}


export default Applications;
