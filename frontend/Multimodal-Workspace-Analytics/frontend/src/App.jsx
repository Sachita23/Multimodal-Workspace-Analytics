import { lazy, Suspense } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation
} from "react-router-dom";

import { AnalyticsProvider } from "./context/AnalyticsProvider";
import ErrorBoundary from "./components/ErrorBoundary";
import { LoadingState } from "./components/PageState";
import DashboardLayout from "./layouts/DashboardLayout";
import { isBackgroundRoute } from "./utils/routes";


// Pages are split into their own chunks and loaded on first visit.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Sessions = lazy(() => import("./pages/Sessions"));
const Applications = lazy(() => import("./pages/Applications"));
const Behavior = lazy(() => import("./pages/Behavior"));
const Trends = lazy(() => import("./pages/Trends"));
const Insights = lazy(() => import("./pages/Insights"));
const Focus = lazy(() => import("./pages/Focus"));
const Settings = lazy(() => import("./pages/Settings"));
const Team = lazy(() => import("./pages/Team"));
const TeamMember = lazy(() => import("./pages/TeamMember"));


function PageFallback() {
  return (
    <DashboardLayout>
      <LoadingState message="Loading page…" />
    </DashboardLayout>
  );
}


function AppRoutes() {

  const location = useLocation();

  return (

    // Keyed on the path so a crashed page recovers when you navigate away.
    <ErrorBoundary key={location.pathname}>

      <Suspense fallback={<PageFallback />}>

        <Routes>

          <Route path="/dashboard" element={<Dashboard />} />

          <Route path="/sessions" element={<Sessions />} />

          <Route path="/applications" element={<Applications />} />

          <Route path="/behavior" element={<Behavior />} />

          <Route path="/trends" element={<Trends />} />


          <Route path="/insights" element={<Insights />} />

          <Route path="/focus" element={<Focus />} />

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

      </Suspense>

    </ErrorBoundary>

  );

}


// The single-user analytics, polled less often on pages that do not show
// them (Team, Focus), where they only feed the navbar's anomaly alert.
function LiveAnalytics({ children }) {

  const { pathname } = useLocation();

  return (
    <AnalyticsProvider background={isBackgroundRoute(pathname)}>
      {children}
    </AnalyticsProvider>
  );

}


function App() {

  return (

    <BrowserRouter>

      <LiveAnalytics>

        <AppRoutes />

      </LiveAnalytics>

    </BrowserRouter>

  );

}


export default App;
