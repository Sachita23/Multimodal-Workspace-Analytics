import { useEffect, useState } from "react";

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


function Sessions() {

  const [sessions, setSessions] = useState([]);

  const [loading, setLoading] = useState(true);

  const [error, setError] = useState(null);


  useEffect(() => {

    loadAnalyticsData()

      .then((data) => {

        setSessions(data.sessionAnalytics);

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
          Loading session analytics...
        </div>

      </DashboardLayout>

    );

  }


  if (error) {

    return (

      <DashboardLayout>

        <div className="error-state">

          <h2>
            Unable to load sessions
          </h2>

          <p>
            {error}
          </p>

        </div>

      </DashboardLayout>

    );

  }


  return (

    <DashboardLayout>

      {/* HEADER */}

      <div className="page-header">

        <div>

          <h1>
            Sessions
          </h1>

          <p>
            Analyze workspace sessions and behavioral patterns.
          </p>

        </div>

      </div>


      {/* SUMMARY */}

      <div className="stats-grid">


        <div className="stat-card">

          <span>
            Total Sessions
          </span>

          <h2>
            {sessions.length}
          </h2>

          <p>
            Recorded workspace sessions
          </p>

        </div>


        <div className="stat-card">

          <span>
            Longest Session
          </span>

          <h2>
            {sessions.length > 0
              ? formatDuration(
                  sessions[0].duration
                )
              : "--"
            }
          </h2>

          <p>
            Longest recorded session
          </p>

        </div>


        <div className="stat-card">

          <span>
            Applications
          </span>

          <h2>
            {sessions.length > 0
              ? Math.max(
                  ...sessions.map(
                    s => s.applications
                  )
                )
              : 0
            }
          </h2>

          <p>
            Maximum apps in one session
          </p>

        </div>


        <div className="stat-card">

          <span>
            Avg Idle Ratio
          </span>

          <h2>

            {sessions.length > 0
              ? (
                  sessions.reduce(
                    (sum, s) =>
                      sum + s.idleRatio,
                    0
                  ) / sessions.length
                ).toFixed(1)
              : 0
            }%

          </h2>

          <p>
            Average session idle ratio
          </p>

        </div>

      </div>


      {/* SESSION TABLE */}

      <div className="session-table-card">

        <div className="table-header">

          <div>

            <h3>
              Workspace Sessions
            </h3>

            <p>
              Session-level behavioral analytics
            </p>

          </div>

        </div>


        <div className="table-wrapper">

          <table>

            <thead>

              <tr>

                <th>
                  Session
                </th>

                <th>
                  Duration
                </th>

                <th>
                  Applications
                </th>

                <th>
                  Switches
                </th>

                <th>
                  Idle Ratio
                </th>

                <th>
                  CPU
                </th>

                <th>
                  Memory
                </th>

                <th>
                  Behavior
                </th>

              </tr>

            </thead>


            <tbody>

              {sessions.map(
                (session, index) => (

                  <tr
                    key={session.session_id}
                  >

                    <td>
                      <strong>
                        Session {index + 1}
                      </strong>

                      <small>
                        {session.session_id}
                      </small>
                    </td>


                    <td>
                      {formatDuration(
                        session.duration
                      )}
                    </td>


                    <td>
                      {session.applications}
                    </td>


                    <td>
                      {session.switches}
                    </td>


                    <td>
                      {session.idleRatio}%
                    </td>


                    <td>
                      {session.avgCPU}%
                    </td>


                    <td>
                      {session.avgMemory}%
                    </td>


                    <td>

                      <span className="behavior-badge">

                        {session.behavior}

                      </span>

                    </td>

                  </tr>

                )
              )}

            </tbody>

          </table>

        </div>

      </div>

    </DashboardLayout>

  );

}


export default Sessions;
