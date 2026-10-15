import { useEffect, useState } from "react";
import { Activity, AlertTriangle, AppWindow, Bell, Brain, LayoutDashboard, Menu, Search, Settings, Sparkles, Target, TrendingUp, Users, X } from "lucide-react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { useAnalytics } from "../context/AnalyticsProvider";
import { useAnomaliesSeen, usePreferences } from "../hooks/usePreferences";
import { rangeShowing, unseenEpisodes } from "../utils/anomalyAlerts";
import { initials } from "../utils/format";

const navigation = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/sessions", label: "Sessions", icon: Activity },
  { to: "/applications", label: "Applications", icon: AppWindow },
  { to: "/behavior", label: "Behavior", icon: Brain },
  { to: "/trends", label: "Trends", icon: TrendingUp },
  { to: "/anomalies", label: "Anomalies", icon: AlertTriangle },
  { to: "/insights", label: "Insights", icon: Sparkles },
  { to: "/focus", label: "Focus", icon: Target },
  { to: "/team", label: "Team", icon: Users },
  { to: "/settings", label: "Settings", icon: Settings },
];


function SearchBox({ onSearch }) {
  const navigate = useNavigate();
  const location = useLocation();
  const currentQuery = location.pathname === "/applications"
    ? new URLSearchParams(location.search).get("q") || ""
    : "";

  function onSubmit(event) {
    event.preventDefault();
    const query = new FormData(event.currentTarget).get("q")?.toString().trim() || "";

    navigate(query ? `/applications?q=${encodeURIComponent(query)}` : "/applications");
    onSearch?.();
  }

  return (
    <form className="global-search" role="search" onSubmit={onSubmit}>
      <Search size={16} aria-hidden="true" />
      {/* Keyed on the active query so it resets when the filter is cleared. */}
      <input
        key={currentQuery}
        name="q"
        type="search"
        defaultValue={currentQuery}
        placeholder="Search apps or windows"
        aria-label="Search applications and window titles"
      />
    </form>
  );
}


function AnomalyBell() {
  const navigate = useNavigate();
  const { data, anomalyEpisodes, range, setRange } = useAnalytics();
  const [preferences] = usePreferences();
  const [seen, markSeen, seeded] = useAnomaliesSeen();
  const loaded = data !== null;

  // First visit in this browser: what happened before is not "new".
  useEffect(() => {
    if (loaded && !seeded) {
      markSeen(anomalyEpisodes.map((episode) => episode.id));
    }
  }, [loaded, seeded, anomalyEpisodes, markSeen]);

  const unseen = preferences.anomalyAlerts && seeded
    ? unseenEpisodes(anomalyEpisodes, seen)
    : [];
  const newCount = unseen.length;

  // Open the Anomalies page on a range that lists every new episode (a
  // range the user picked is only ever widened).
  function openAnomalies() {
    const target = rangeShowing(unseen, range);

    if (target !== range) setRange(target);

    navigate("/anomalies");
  }

  const label = newCount > 0
    ? `${newCount} new unusual ${newCount === 1 ? "episode" : "episodes"}`
    : "No new unusual activity";

  return (
    <button
      type="button"
      className="nav-icon-button"
      aria-label={label}
      title={label}
      onClick={openAnomalies}
    >
      <Bell size={17} aria-hidden="true" />
      {newCount > 0 && <span className="nav-badge-dot" />}
    </button>
  );
}


function Navbar() {
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [preferences] = usePreferences();
  const linkClass = ({ isActive }) => `top-nav-link ${isActive ? "active" : ""}`;
  const name = preferences.workspaceName.trim() || "Workspace User";

  return (
    <header className="top-navigation">
      <div className="top-nav-inner">
        <NavLink to="/dashboard" className="brand" onClick={() => setIsMenuOpen(false)}>
          <span className="brand-mark">W</span>
          <span>Workspace <strong>Analytics</strong></span>
        </NavLink>

        <nav className="desktop-navigation" aria-label="Primary navigation">
          {navigation.map(({ to, label }) => <NavLink key={to} to={to} className={linkClass}>{label}</NavLink>)}
        </nav>

        <div className="top-nav-actions">
          <SearchBox />
          <AnomalyBell />
          <NavLink to="/settings" className="user-menu" aria-label={`${name} – open settings`}><span className="user-avatar" aria-hidden="true">{initials(name)}</span><span className="user-name">{name}</span></NavLink>
          <button className="menu-toggle" aria-label="Toggle navigation" aria-expanded={isMenuOpen} onClick={() => setIsMenuOpen((open) => !open)}>{isMenuOpen ? <X size={20} /> : <Menu size={20} />}</button>
        </div>
      </div>

      {isMenuOpen && (
        <nav className="mobile-navigation" aria-label="Mobile navigation">
          <SearchBox onSearch={() => setIsMenuOpen(false)} />
          {navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} className={linkClass} onClick={() => setIsMenuOpen(false)}><Icon size={17} />{label}</NavLink>)}
        </nav>
      )}
    </header>
  );
}

export default Navbar;
