import {
  BrowserRouter,
  Routes,
  Route,
  Navigate
} from "react-router-dom";

import Dashboard from "./pages/Dashboard";

import Sessions from "./pages/Sessions";
import Applications from "./pages/Applications";
import Behavior from "./pages/Behavior";
import Trends from "./pages/Trends";
import Anomalies from "./pages/Anomalies";
import Insights from "./pages/Insights";
import Settings from "./pages/Settings";
import Team from "./pages/Team";
import TeamMember from "./pages/TeamMember";


function App() {

  return (

    <BrowserRouter>

      <Routes>

        <Route
          path="/dashboard"
          element={<Dashboard />}
        />

        <Route
          path="/sessions"
          element={<Sessions />}
        />

        <Route
          path="/applications"
          element={<Applications />}
        />

        <Route
          path="/behavior"
          element={<Behavior />}
        />

        <Route path="/trends" element={<Trends />} />

        <Route path="/anomalies" element={<Anomalies />} />

        <Route path="/insights" element={<Insights />} />

        <Route path="/team" element={<Team />} />

        <Route path="/team/:memberId" element={<TeamMember />} />

        <Route path="/settings" element={<Settings />} />

        <Route
          path="*"
          element={
            <Navigate
              to="/dashboard"
              replace
            />
          }
        />

      </Routes>

    </BrowserRouter>

  );

}


export default App;
