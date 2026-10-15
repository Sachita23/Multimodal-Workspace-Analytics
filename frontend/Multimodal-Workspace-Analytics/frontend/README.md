# Workspace Analytics dashboard

The React + Vite front end for Workspace Analytics. It shows your own
activity (Dashboard, Sessions, Applications, Behavior, Trends, Anomalies,
Insights), your Focus Assistant sessions (Focus) and, for team admins, the
Team pages. All data comes from the Flask API in `api/server.py`.

## Requirements

- Node `^20.19.0 || >=22.12.0` (`.nvmrc` pins 22; run `nvm use`)
- The Flask API running (normally started for you by `python start_project.py`
  from the repository root)

## Development

```bash
npm ci          # install exact versions from package-lock.json
npm run dev     # http://localhost:5173
```

`python start_project.py` starts the collector, the live pipeline, the API and
this dev server together, so you rarely need to run `npm run dev` yourself.

The dev server is pinned to port 5173 (`strictPort`): if something else holds
it, Vite exits instead of quietly moving to another port.

### API proxy

Requests to `/api/*` are proxied to `http://127.0.0.1:$WORKSPACE_API_PORT`
(default **5050**, the same default as `config.API_PORT`; macOS AirPlay holds
port 5000). When you run the API on another port, start Vite with the same
value:

```bash
WORKSPACE_API_PORT=5099 npm run dev
```

## How live updates work

`src/context/AnalyticsProvider.jsx` is the single source of data for the
single-user pages:

- fetches `/api/analytics.csv` once, then every 10 s with `If-None-Match`, so
  an unchanged file costs a `304`;
- reads `/api/status` on the same cycle to tell whether the collector is still
  writing (the "Live · updated 6 s ago" pill turns amber when it stops);
- pauses while the tab is hidden and keeps the last good data when a poll fails.

`src/data/analytics.js` holds the parsing (`fetchAnalyticsRows`) and a pure
`computeAnalytics(rows, range)` that every page reads from. The range picker
(Last 24 hours / 7 days / 30 days / All time) filters rows before aggregation.

The Focus page polls `/api/focus/live` every 5 s and `/api/focus/summary` and
`/api/focus/sessions` every 30 s.

## Build and lint

```bash
npm run build   # production build into dist/
npm run lint    # oxlint
npm test        # unit tests for src/data/analytics.js (node --test)
npm run preview # serve dist/ locally (the /api proxy is dev-server only)
```

Pages are lazy-loaded, so each route ships as its own chunk.

## Layout

```
src/
  App.jsx                    routes (lazy), error boundary, analytics provider
  context/AnalyticsProvider  shared live data + range
  data/analytics.js          CSV parsing and all single-user aggregation
  data/focus.js              Focus API client
  data/team.js               Team API client
  hooks/usePreferences.js    preferences and "seen anomalies" (localStorage)
  utils/format.js            durations, dates, shared chart colours
  components/                Navbar, LiveIndicator, PageState, ErrorBoundary, AdminKeyForm
  layouts/DashboardLayout    navbar + live status toolbar
  pages/                     one file per route
```
