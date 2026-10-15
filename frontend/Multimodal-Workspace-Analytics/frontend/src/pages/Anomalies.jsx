import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate } from "../components/PageState";
import { useAnalytics } from "../context/AnalyticsProvider";
import { useAnomaliesSeen, usePreferences } from "../hooks/usePreferences";
import { episodesInRange, idsToMarkSeen, rangeShowing, unseenEpisodes } from "../utils/anomalyAlerts";
import { formatDuration, formatTimeRange } from "../utils/format";

const PAGE_SIZE = 50;
const DETAIL_ROWS = 100;

// Viewing this page counts as seeing the episodes it lists for the selected
// range (they leave the bell). Older ones stay new until a range shows them.
function useMarkAnomaliesSeen() {
  const { data, anomalyEpisodes } = useAnalytics();
  const [, markSeen, seeded] = useAnomaliesSeen();
  const loaded = data !== null;
  const rangeStart = data?.rangeStart ?? null;

  useEffect(() => {
    if (loaded) {
      markSeen(idsToMarkSeen(anomalyEpisodes, rangeStart, seeded));
    }
  }, [loaded, anomalyEpisodes, rangeStart, seeded, markSeen]);
}

// New episodes that the selected range leaves out, with a way to show them.
function OlderNewEpisodes({ data }) {
  const { anomalyEpisodes, range, setRange } = useAnalytics();
  const [preferences] = usePreferences();
  const [seen, , seeded] = useAnomaliesSeen();

  if (!preferences.anomalyAlerts || !seeded || data.rangeStart === null) return null;

  const shown = new Set(episodesInRange(anomalyEpisodes, data.rangeStart).map((episode) => episode.id));
  const older = unseenEpisodes(anomalyEpisodes, seen).filter((episode) => !shown.has(episode.id));

  if (!older.length) return null;

  const one = older.length === 1;

  return (
    <div className="anomaly-notice" role="status">
      <span>{older.length} new unusual {one ? "episode is" : "episodes are"} older than the {data.rangeLabel.toLowerCase()}.</span>
      <button type="button" className="range-selector" onClick={() => setRange(rangeShowing(older, range))}>
        Show {one ? "it" : "them"}
      </button>
    </div>
  );
}

function EpisodeDetail({ episode }) {
  const rows = episode.rows.slice(-DETAIL_ROWS).reverse();

  return (
    <tr className="episode-detail">
      <td colSpan="8">
        <table className="mini-table">
          <thead><tr><th>Time</th><th>Application</th><th>Window</th><th>Status</th><th>CPU</th><th>Memory</th><th>Score</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.timestamp}>
                <td>{row.timestamp}</td>
                <td>{row.application}</td>
                <td className="window-title" title={row.window_title}>{row.window_title || "--"}</td>
                <td>{row.activity_status}</td>
                <td>{row.cpu_percent}%</td>
                <td>{row.memory_percent}%</td>
                <td>{row.anomaly_score.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {episode.rows.length > DETAIL_ROWS && <p className="section-sub">Showing the newest {DETAIL_ROWS} of {episode.rows.length.toLocaleString()} samples.</p>}
      </td>
    </tr>
  );
}

function AnomaliesContent({ data }) {
  const [visible, setVisible] = useState(PAGE_SIZE);
  const [expanded, setExpanded] = useState(null);

  const { anomalyEpisodes: episodes, metrics } = data;
  const highestScore = episodes.reduce((best, episode) => Math.max(best, episode.maxScore), 0);
  const rate = metrics.totalRecords ? (metrics.anomalousRecords / metrics.totalRecords) * 100 : 0;

  return (
    <>
      <div className="page-header"><div><h1>Anomalies</h1><p>Unusual stretches of activity, grouped into episodes and listed newest first.</p></div></div>
      <OlderNewEpisodes data={data} />
      <div className="stats-grid">
        <div className="stat-card"><span>Unusual Episodes</span><h2>{metrics.anomalyCount}</h2><p>{metrics.anomalousRecords.toLocaleString()} flagged samples</p></div>
        <div className="stat-card"><span>Anomaly Rate</span><h2>{rate.toFixed(1)}%</h2><p>Of samples in this range</p></div>
        <div className="stat-card"><span>Highest Score</span><h2>{episodes.length ? highestScore.toFixed(3) : "--"}</h2><p>Most unusual episode</p></div>
        <div className="stat-card"><span>Reviewed Samples</span><h2>{metrics.totalRecords.toLocaleString()}</h2><p>{data.rangeLabel}</p></div>
      </div>
      <div className="session-table-card">
        <div className="table-header"><h3>Unusual Episodes</h3><p>Select an episode to see its samples.</p></div>
        <div className="table-wrapper">
          <table>
            <thead><tr><th><span className="sr-only">Details</span></th><th>When</th><th>Duration</th><th>Top Applications</th><th>Behavior</th><th>Max Score</th><th>CPU</th><th>Idle Ratio</th></tr></thead>
            <tbody>
              {episodes.length ? episodes.slice(0, visible).map((episode) => {
                const open = expanded === episode.id;
                return (
                  <Fragment key={episode.id}>
                    <tr>
                      <td>
                        <button type="button" className="row-toggle" aria-expanded={open} aria-label={open ? "Hide samples" : "Show samples"} onClick={() => setExpanded(open ? null : episode.id)}>
                          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        </button>
                      </td>
                      <td><strong>{formatTimeRange(episode.start, episode.end)}</strong><small>Session {episode.session_id} · {episode.records.toLocaleString()} samples</small></td>
                      <td>{formatDuration(episode.duration)}</td>
                      <td>{episode.topApps.join(", ")}</td>
                      <td><span className="behavior-badge">{episode.behavior}</span></td>
                      <td><span className="anomaly-score">{episode.maxScore.toFixed(3)}</span></td>
                      <td>{episode.avgCPU}%</td>
                      <td>{episode.idleRatio.toFixed(1)}%</td>
                    </tr>
                    {open && <EpisodeDetail episode={episode} />}
                  </Fragment>
                );
              }) : <tr><td colSpan="8" className="empty-table">No unusual activity was detected in this range.</td></tr>}
            </tbody>
          </table>
        </div>
        {episodes.length > visible && (
          <div className="table-footer">
            <button type="button" className="range-selector" onClick={() => setVisible((count) => count + PAGE_SIZE)}>
              Show {Math.min(PAGE_SIZE, episodes.length - visible)} more of {episodes.length - visible}
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function Anomalies() {
  useMarkAnomaliesSeen();

  return (
    <DashboardLayout>
      <AnalyticsGate loadingMessage="Loading anomaly detection..." errorTitle="Unable to load anomalies">
        {(data) => <AnomaliesContent data={data} />}
      </AnalyticsGate>
    </DashboardLayout>
  );
}

export default Anomalies;
