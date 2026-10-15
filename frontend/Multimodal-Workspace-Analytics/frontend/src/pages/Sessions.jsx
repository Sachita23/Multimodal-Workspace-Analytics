import { useState } from "react";

import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate } from "../components/PageState";
import { useAnalytics } from "../context/AnalyticsProvider";
import { STALE_DATA_SECONDS } from "../data/analytics";
import { formatDuration, formatTimeRange } from "../utils/format";


const PAGE_SIZE = 50;


function SessionsContent({ data }) {

  const { status, dataAsOf, lastUpdated } = useAnalytics();
  const [visible, setVisible] = useState(PAGE_SIZE);

  const sessions = data.sessionAnalytics;
  const { metrics } = data;

  // The newest session is "live" while the collector is still writing.
  const collectorRunning = status && typeof status.collector_stale === "boolean"
    ? !status.collector_stale
    : Boolean(dataAsOf && lastUpdated)
      && lastUpdated.getTime() - dataAsOf.getTime() < STALE_DATA_SECONDS * 1000;

  const longest = sessions.reduce(
    (best, session) => (!best || session.duration > best.duration ? session : best),
    null
  );

  const maxApplications = sessions.reduce(
    (best, session) => Math.max(best, session.applications),
    0
  );


  return (

    <>

      {/* HEADER */}

      <div className="page-header">
        <div>
          <h1>Sessions</h1>
          <p>
            Work sessions, newest first. A new session starts after a break of 5 minutes or more.
          </p>
        </div>
      </div>


      {/* SUMMARY */}

      <div className="stats-grid">

        <div className="stat-card">
          <span>Total Sessions</span>
          <h2>{sessions.length}</h2>
          <p>{data.rangeLabel}</p>
        </div>

        <div className="stat-card">
          <span>Longest Session</span>
          <h2>{longest ? formatDuration(longest.duration) : "--"}</h2>
          <p>{longest ? formatTimeRange(longest.start, longest.end) : "No sessions yet"}</p>
        </div>

        <div className="stat-card">
          <span>Applications</span>
          <h2>{maxApplications}</h2>
          <p>Most apps used in one session</p>
        </div>

        <div className="stat-card">
          <span>Avg Idle Ratio</span>
          <h2>{metrics.avgIdleRatio.toFixed(1)}%</h2>
          <p>Idle share of all tracked session time</p>
        </div>

      </div>


      {/* SESSION TABLE */}

      <div className="session-table-card">

        <div className="table-header">
          <div>
            <h3>Workspace Sessions</h3>
            <p>Session-level behavioral analytics</p>
          </div>
        </div>

        <div className="table-wrapper">

          <table>

            <thead>
              <tr>
                <th>Session</th>
                <th>Duration</th>
                <th>Applications</th>
                <th>Switches</th>
                <th>Idle Ratio</th>
                <th>CPU</th>
                <th>Memory</th>
                <th>Behavior</th>
              </tr>
            </thead>

            <tbody>

              {sessions.slice(0, visible).map((session) => (

                <tr key={session.session_id}>

                  <td>
                    <strong>
                      {formatTimeRange(session.start, session.end)}
                    </strong>
                    {session.isLatest && (
                      <span className={`session-badge ${collectorRunning ? "is-live" : ""}`}>
                        {collectorRunning ? "Live" : "Latest"}
                      </span>
                    )}
                    <small>Session {session.session_id}</small>
                  </td>

                  <td>{formatDuration(session.duration)}</td>

                  <td>{session.applications}</td>

                  <td>{session.switches}</td>

                  <td>{session.idleRatio.toFixed(1)}%</td>

                  <td>{session.avgCPU}%</td>

                  <td>{session.avgMemory}%</td>

                  <td>
                    <span className="behavior-badge">{session.behavior}</span>
                  </td>

                </tr>

              ))}

            </tbody>

          </table>

        </div>

        {sessions.length > visible && (
          <div className="table-footer">
            <button type="button" className="range-selector" onClick={() => setVisible((count) => count + PAGE_SIZE)}>
              Show {Math.min(PAGE_SIZE, sessions.length - visible)} more of {sessions.length - visible}
            </button>
          </div>
        )}

      </div>

    </>

  );

}


function Sessions() {

  return (

    <DashboardLayout>

      <AnalyticsGate
        loadingMessage="Loading session analytics..."
        errorTitle="Unable to load sessions"
      >
        {(data) => <SessionsContent data={data} />}
      </AnalyticsGate>

    </DashboardLayout>

  );

}


export default Sessions;
