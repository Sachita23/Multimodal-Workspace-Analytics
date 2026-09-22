import {
  LayoutDashboard,
  Activity,
  AppWindow,
  Brain,
  TrendingUp,
  AlertTriangle,
  Sparkles,
  Settings,
} from "lucide-react";

import { NavLink } from "react-router-dom";


function Sidebar() {

  const navClass = ({ isActive }) =>
    `nav-item ${isActive ? "active" : ""}`;


  return (

    <aside className="sidebar">

      {/* Logo */}

      <div className="sidebar-logo">

        <div className="logo-icon">
          W
        </div>

        <div>

          <h2>
            Workspace
          </h2>

          <span>
            Analytics
          </span>

        </div>

      </div>


      {/* Navigation */}

      <nav className="sidebar-nav">


        <NavLink
          to="/dashboard"
          className={navClass}
        >

          <LayoutDashboard size={18} />

          <span>
            Dashboard
          </span>

        </NavLink>


        <NavLink
          to="/sessions"
          className={navClass}
        >

          <Activity size={18} />

          <span>
            Sessions
          </span>

        </NavLink>


        <NavLink
          to="/applications"
          className={navClass}
        >

          <AppWindow size={18} />

          <span>
            Applications
          </span>

        </NavLink>


        <NavLink
          to="/behavior"
          className={navClass}
        >

          <Brain size={18} />

          <span>
            Behavior
          </span>

        </NavLink>


        <NavLink
          to="/trends"
          className={navClass}
        >

          <TrendingUp size={18} />

          <span>
            Trends
          </span>

        </NavLink>


        <NavLink
          to="/anomalies"
          className={navClass}
        >

          <AlertTriangle size={18} />

          <span>
            Anomalies
          </span>

        </NavLink>


        <NavLink
          to="/insights"
          className={navClass}
        >

          <Sparkles size={18} />

          <span>
            Insights
          </span>

        </NavLink>


      </nav>


      {/* Bottom */}

      <div className="sidebar-bottom">

        <NavLink
          to="/settings"
          className={navClass}
        >

          <Settings size={18} />

          <span>
            Settings
          </span>

        </NavLink>

      </div>


    </aside>

  );

}


export default Sidebar;
