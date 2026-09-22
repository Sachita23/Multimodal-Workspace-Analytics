import { useState } from "react";
import { Activity, AlertTriangle, AppWindow, Bell, Brain, LayoutDashboard, Menu, Search, Settings, Sparkles, TrendingUp, X } from "lucide-react";
import { NavLink } from "react-router-dom";

const navigation = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/sessions", label: "Sessions", icon: Activity },
  { to: "/applications", label: "Applications", icon: AppWindow },
  { to: "/behavior", label: "Behavior", icon: Brain },
  { to: "/trends", label: "Trends", icon: TrendingUp },
  { to: "/anomalies", label: "Anomalies", icon: AlertTriangle },
  { to: "/insights", label: "Insights", icon: Sparkles },
  { to: "/settings", label: "Settings", icon: Settings },
];

function Navbar() {
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const linkClass = ({ isActive }) => `top-nav-link ${isActive ? "active" : ""}`;

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
          <label className="global-search"><Search size={16} /><input type="search" placeholder="Search analytics" aria-label="Search analytics" /></label>
          <button className="nav-icon-button" aria-label="Notifications"><Bell size={17} /><span /></button>
          <NavLink to="/settings" className="user-menu" aria-label="Open settings"><span className="user-avatar">V</span><span className="user-name">Workspace User</span></NavLink>
          <button className="menu-toggle" aria-label="Toggle navigation" aria-expanded={isMenuOpen} onClick={() => setIsMenuOpen((open) => !open)}>{isMenuOpen ? <X size={20} /> : <Menu size={20} />}</button>
        </div>
      </div>

      {isMenuOpen && (
        <nav className="mobile-navigation" aria-label="Mobile navigation">
          {navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} className={linkClass} onClick={() => setIsMenuOpen(false)}><Icon size={17} />{label}</NavLink>)}
        </nav>
      )}
    </header>
  );
}

export default Navbar;
