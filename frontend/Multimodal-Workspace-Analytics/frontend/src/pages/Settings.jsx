import { useState } from "react";
import { Check, RefreshCw, Save } from "lucide-react";
import DashboardLayout from "../layouts/DashboardLayout";
import { useAnalytics } from "../context/AnalyticsProvider";
import { REFRESH_SECONDS } from "../data/analytics";
import { usePreferences } from "../hooks/usePreferences";
import { formatDateTime } from "../utils/format";

function Settings() {
  const [savedPreferences, savePreferences] = usePreferences();
  const { rows, data, dataAsOf, lastUpdated, status, refresh, refreshSeconds, error } = useAnalytics();
  const [draft, setDraft] = useState(savedPreferences);
  const [saveState, setSaveState] = useState("idle");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");

  function updatePreference(key, value) {
    setDraft((current) => ({ ...current, [key]: value }));
    setSaveState("idle");
  }

  function onSave() {
    setSaveState(savePreferences(draft) ? "saved" : "session");
  }

  async function refreshData() {
    setRefreshing(true);
    setRefreshError("");
    try {
      await refresh();
    } catch (err) {
      setRefreshError(err?.message || "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  }

  const sessionCount = new Set(rows.map((row) => row.session_id)).size;
  const pipeline = status?.pipeline;

  return (
    <DashboardLayout>
      <div className="page-header settings-header">
        <div>
          <h1>Settings</h1>
          <p>Personalize how Workspace Analytics looks and what it highlights.</p>
        </div>
        <button className="settings-save-button" onClick={onSave}>
          {saveState === "idle" ? <Save size={15} /> : <Check size={15} />}
          {saveState === "idle" ? "Save preferences" : "Saved"}
        </button>
      </div>
      {saveState === "session" && (
        <p className="form-error" role="alert">
          Browser storage is blocked, so these preferences apply until you close this tab.
        </p>
      )}

      <div className="settings-layout">
        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Workspace profile</h2><p>Your personal workspace details.</p></div>
          <label className="settings-field">
            <span>Workspace name</span>
            <input value={draft.workspaceName} onChange={(event) => updatePreference("workspaceName", event.target.value)} placeholder="Enter workspace name" maxLength={60} />
          </label>
          <div className="settings-note">Shown in the top bar. Stored locally in this browser only.</div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Alerts</h2><p>Choose what the dashboard highlights for you.</p></div>
          <SettingToggle label="Anomaly alerts" description="Show a dot on the bell when a new unusual episode is detected." checked={draft.anomalyAlerts} onChange={(value) => updatePreference("anomalyAlerts", value)} />
        </section>

        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Display</h2><p>Adjust the density of analytic tables.</p></div>
          <SettingToggle label="Compact table rows" description="Show more session and application records at once." checked={draft.compactTables} onChange={(value) => updatePreference("compactTables", value)} />
        </section>

        <section className="settings-panel data-status-panel">
          <div className="settings-panel-heading"><h2>Dataset status</h2><p>Your local analytics source, refreshed every {Math.round(refreshSeconds)} seconds{refreshSeconds > REFRESH_SECONDS ? " (less often as the history grows)" : ""}.</p></div>
          <div className="dataset-stats">
            <div><strong>{data ? rows.length.toLocaleString() : "—"}</strong><span>samples loaded</span></div>
            <div><strong>{data ? sessionCount.toLocaleString() : "—"}</strong><span>sessions found</span></div>
          </div>
          <dl className="status-list">
            <div><dt>Newest sample</dt><dd>{formatDateTime(dataAsOf)}</dd></div>
            <div><dt>Last checked</dt><dd>{lastUpdated ? lastUpdated.toLocaleTimeString() : "--"}</dd></div>
            {pipeline && <div><dt>Pipeline run</dt><dd>{pipeline.last_success ? formatDateTime(new Date(pipeline.last_success)) : "--"}{pipeline.duration_s ? ` (${Number(pipeline.duration_s).toFixed(1)} s)` : ""}</dd></div>}
            {pipeline?.last_error && <div><dt>Pipeline error</dt><dd className="status-error">{pipeline.last_error}</dd></div>}
          </dl>
          {(refreshError || (error && !refreshing)) && <p className="form-error" role="alert">{refreshError || error.message}</p>}
          <button className="refresh-button" onClick={refreshData} disabled={refreshing} aria-busy={refreshing}>
            <RefreshCw size={14} className={refreshing ? "spin" : ""} />
            {refreshing ? "Refreshing…" : "Refresh analytics"}
          </button>
        </section>
      </div>
    </DashboardLayout>
  );
}

function SettingToggle({ label, description, checked, onChange }) {
  return (
    <div className="setting-toggle-row">
      <div><h3>{label}</h3><p>{description}</p></div>
      <button className={`setting-switch ${checked ? "enabled" : ""}`} type="button" role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)}>
        <span />
      </button>
    </div>
  );
}

export default Settings;
