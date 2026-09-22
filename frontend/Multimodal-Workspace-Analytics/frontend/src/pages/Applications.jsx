import { useEffect, useState } from "react";

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
import { loadAnalyticsData } from "../data/analytics";


function formatDuration(seconds) {

  const minutes = Math.round(seconds / 60);

  if (minutes < 60) {
    return `${minutes} min`;
  }

  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;

  return `${hours}h ${remaining}m`;
}


function Applications() {

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
          Loading application analytics...
        </div>

      </DashboardLayout>

    );

  }


  if (error) {

    return (

      <DashboardLayout>

        <div className="error-state">

          <h2>
            Unable to load applications
          </h2>

          <p>
            {error}
          </p>

        </div>

      </DashboardLayout>

    );

  }


  const applications = analytics.applicationUsage;


  const totalApplications =
    applications.length;


  const totalUsage =
    applications.reduce(
      (sum, app) =>
        sum + app.duration,
      0
    );


  const totalSwitches =
    applications.reduce(
      (sum, app) =>
        sum + app.switches,
      0
    );


  const topApplication =
    applications.length > 0
      ? applications[0]
      : null;


  const chartData =
    applications
      .slice(0, 10)
      .map((app) => ({

        application:
          app.application.length > 20
            ? app.application.substring(0, 20) + "..."
            : app.application,

        duration:
          Math.round(
            app.duration / 60
          )

      }));


  return (

    <DashboardLayout>


      {/* =========================
          HEADER
      ========================= */}

      <div className="page-header">

        <div>

          <h1>
            Applications
          </h1>

          <p>
            Analyze application usage and context switching.
          </p>

        </div>

      </div>


      {/* =========================
          SUMMARY CARDS
      ========================= */}

      <div className="stats-grid">


        <div className="stat-card">

          <span>
            Applications
          </span>

          <h2>
            {totalApplications}
          </h2>

          <p>
            Unique applications detected
          </p>

        </div>


        <div className="stat-card">

          <span>
            Most Used
          </span>

          <h2>
            {topApplication
              ? topApplication.application
              : "--"
            }
          </h2>

          <p>
            Highest active duration
          </p>

        </div>


        <div className="stat-card">

          <span>
            Tracked Time
          </span>

          <h2>
            {formatDuration(totalUsage)}
          </h2>

          <p>
            Total application activity
          </p>

        </div>


        <div className="stat-card">

          <span>
            App Switches
          </span>

          <h2>
            {totalSwitches}
          </h2>

          <p>
            Recorded context switches
          </p>

        </div>


      </div>


      {/* =========================
          CHART
      ========================= */}

      <div className="chart-card application-chart">

        <div className="chart-header">

          <div>

            <h3>
              Application Usage
            </h3>

            <p>
              Top applications by active time
            </p>

          </div>

        </div>


        <div className="application-chart-container">

          <ResponsiveContainer
            width="100%"
            height="100%"
          >

            <BarChart
              data={chartData}
              margin={{
                top: 10,
                right: 30,
                left: 20,
                bottom: 60
              }}
            >

              <CartesianGrid
                strokeDasharray="3 3"
              />

              <XAxis
                dataKey="application"
                angle={-35}
                textAnchor="end"
                interval={0}
              />

              <YAxis
                unit=" min"
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
                radius={[5, 5, 0, 0]}
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

            <h3>
              Application Details
            </h3>

            <p>
              Detailed application-level analytics
            </p>

          </div>

        </div>


        <div className="table-wrapper">

          <table>

            <thead>

              <tr>

                <th>
                  Rank
                </th>

                <th>
                  Application
                </th>

                <th>
                  Active Time
                </th>

                <th>
                  Records
                </th>

                <th>
                  Switches
                </th>

                <th>
                  Usage %
                </th>

              </tr>

            </thead>


            <tbody>

              {applications.map(
                (app, index) => {

                  const percentage =
                    totalUsage > 0
                      ? (
                          app.duration /
                          totalUsage
                        ) * 100
                      : 0;


                  return (

                    <tr
                      key={app.application}
                    >

                      <td>
                        <strong>
                          #{index + 1}
                        </strong>
                      </td>


                      <td>
                        <strong>
                          {app.application}
                        </strong>
                      </td>


                      <td>
                        {formatDuration(
                          app.duration
                        )}
                      </td>


                      <td>
                        {app.records}
                      </td>


                      <td>
                        {app.switches}
                      </td>


                      <td>

                        {percentage.toFixed(1)}%

                      </td>

                    </tr>

                  );

                }
              )}

            </tbody>

          </table>

        </div>

      </div>


    </DashboardLayout>

  );

}


export default Applications;
