import { useLocation } from "react-router-dom";

import Navbar from "../components/Navbar";
import LiveIndicator, { RangeSelect } from "../components/LiveIndicator";
import { usePreferences } from "../hooks/usePreferences";
import { toolbarFor } from "../utils/routes";


function DashboardLayout({ children }) {
  const { pathname } = useLocation();
  const [preferences] = usePreferences();
  const toolbar = toolbarFor(pathname);

  return (
    <div className="app-layout">

      <Navbar />

      <div className="main-area">

        <main className={`page-content${preferences.compactTables ? " compact-tables" : ""}`}>

          {toolbar.live && (
            <div className="page-toolbar">
              <LiveIndicator />
              {toolbar.range && <RangeSelect />}
            </div>
          )}

          {children}

        </main>

      </div>

    </div>
  );
}

export default DashboardLayout;
