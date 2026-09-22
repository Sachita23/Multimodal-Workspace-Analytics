import { useEffect, useState } from "react";
import { Check, RefreshCw, Save } from "lucide-react";
import DashboardLayout from "../layouts/DashboardLayout";
import { loadAnalyticsData } from "../data/analytics";

const defaultPreferences = {
  workspaceName: "Workspace User",
  weeklySummary: true,
  anomalyAlerts: true,
  compactTables: false,
};

function Settings() {
  const [preferences, setPreferences] = useState(() => {
    const stored = window.localStorage.getItem("workspace-analytics-preferences");

    if (!stored) {
      return defaultPreferences;
    }

    try {
      return { ...defaultPreferences, ...JSON.parse(stored) };
    } catch {
      return defaultPreferences;
    }
  });
  const [analytics, setAnalytics] = useState(null);
  const [saved, setSaved] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    loadAnalyticsData().then(setAnalytics).catch(console.error);
  }, []);

  function updatePreference(key, value) {
    setPreferences((current) => ({ ...current, [key]: value }));
    setSaved(false);
  }

  function savePreferences() {
    window.localStorage.setItem(
      "workspace-analytics-preferences",
      JSON.stringify(preferences)
    );
    setSaved(true);
  }

  async function refreshData() {
    setRefreshing(true);
    try {
      setAnalytics(await loadAnalyticsData());
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <DashboardLayout>
      <div className="page-header settings-header">
        <div>
          <h1>Settings</h1>
          <p>Personalize how Workspace Analytics looks, feels, and notifies you.</p>
        </div>
        <button className="settings-save-button" onClick={savePreferences}>
          {saved ? <Check size={15} /> : <Save size={15} />}
          {saved ? "Saved" : "Save preferences"}
        </button>
      </div>

      <div className="settings-layout">
        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Workspace profile</h2><p>Your personal workspace details.</p></div>
          <label className="settings-field">
            <span>Workspace name</span>
            <input value={preferences.workspaceName} onChange={(event) => updatePreference("workspaceName", event.target.value)} placeholder="Enter workspace name" />
          </label>
          <div className="settings-note">This name is stored locally in your browser.</div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Notifications</h2><p>Choose the insights that deserve your attention.</p></div>
          <SettingToggle label="Weekly workspace summary" description="Receive a roundup of focus and activity patterns." checked={preferences.weeklySummary} onChange={(value) => updatePreference("weeklySummary", value)} />
          <SettingToggle label="Anomaly alerts" description="Highlight unusual activity when it is detected." checked={preferences.anomalyAlerts} onChange={(value) => updatePreference("anomalyAlerts", value)} />
        </section>

        <section className="settings-panel">
          <div className="settings-panel-heading"><h2>Display</h2><p>Adjust the density of analytic tables.</p></div>
          <SettingToggle label="Compact table rows" description="Show more session and application records at once." checked={preferences.compactTables} onChange={(value) => updatePreference("compactTables", value)} />
        </section>

        <section className="settings-panel data-status-panel">
          <div className="settings-panel-heading"><h2>Dataset status</h2><p>Your current local analytics source.</p></div>
          <div className="dataset-stats">
            <div><strong>{analytics?.metrics.totalRecords?.toLocaleString() ?? "—"}</strong><span>records loaded</span></div>
            <div><strong>{analytics?.metrics.totalSessions ?? "—"}</strong><span>sessions found</span></div>
          </div>
          <button className="refresh-button" onClick={refreshData} disabled={refreshing}>
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
      <button className={`setting-switch ${checked ? "enabled" : ""}`} type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}>
        <span />
      </button>
    </div>
  );
}

export default Settings;
