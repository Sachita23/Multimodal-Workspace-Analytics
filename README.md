# Multimodal Workspace Analytics

Workspace Analytics records which application is in front, the window
title, whether you are active or idle, and CPU, memory and battery
readings, every 5 seconds. A live pipeline turns those samples into work
sessions, clusters the sessions into behaviour types and flags unusual
ones. A React dashboard shows the results and refreshes on its own every
10 seconds. Two optional parts build on this: a desktop **Focus
Assistant** for timed focus sessions, and **Team analytics**, where one
main user collects and compares activity from teammates who opt in.

Everything runs on your own machine. Your activity data stays in local
CSV and SQLite files and is never uploaded to a cloud service. The
dashboard uses the fonts already on your computer and loads nothing from
the internet.

## Features

- **Collector** for Windows, macOS and Linux (X11). Records the
  foreground app and window title, active/idle status, CPU, memory and
  battery. The lock screen is recorded as `Locked`.
- **Live pipeline**: every 10 s it rebuilds sessions (a new session
  starts after a 5-minute break or a gap in collection), counts app and
  window switches, clusters sessions into behaviour types (Idle-heavy,
  High-switching, Multitasking, Stable-activity, Mixed-activity) with
  K-Means, and flags unusual finished sessions with a robust (median/MAD)
  distance. The session in progress is never flagged.
- **Dashboard** with ten pages, a "Live · updated N s ago" indicator that
  turns amber when the collector stops, and a range picker (last 24 hours,
  7 days, 30 days, all time).
- **Focus Assistant** (Tk desktop app): pick the apps that count as focus,
  start a timed session, get a reminder when you drift away. Its sessions
  appear live on the dashboard's Focus page.
- **Team analytics**: members join with a one-time invite and explicit
  consent, their collectors upload over HTTP, and the Team page shows who
  is online and what they are working on right now, behaviour groups
  across the team, how each person differs from the rest, and unusual
  sessions.
- **Demo data**: a sandbox for the single-user dashboard that never
  touches your own recordings, and live demo members for the Team page,
  marked **Demo** and analysed apart from real members.

## How it fits together

```
collector/collector.py --> data/raw/workspace_activity.csv
                                 |  every 10 s
live_pipeline.py (ETL + ML) --> data/processed/workspace_activity_processed.csv
                                data/processed/status.json
run_focus_assistant.py -----> data/focus/*.csv, data/focus/live.json
                                 |
api/server.py (Flask, :5050) --> /api/analytics.csv  /api/status  /api/focus/*  /api/team/*
                                 |  proxied by Vite
dashboard (React, :5173) <-------+

team member:  collector/team_sync.py --HTTP--> /api/team/ingest --> data/team/team.db
```

| Part | Built with |
| --- | --- |
| Collection | Python, psutil; pywin32 (Windows), AppleScript via `osascript` (macOS), `xdotool`/`xprintidle` (Linux) |
| Processing and ML | pandas, NumPy (K-Means and the anomaly score are implemented directly in NumPy) |
| Storage | CSV files (single user, Focus Assistant), SQLite (team server, member upload queue) |
| API | Flask; waitress when listening on the network |
| Dashboard | React 19, Vite 8, React Router 7, Recharts 3, PapaParse, lucide-react |
| Focus Assistant | Tkinter |
| Tooling | pytest, oxlint, `node --test`, GitHub Actions |

## Requirements

- **Python 3.12 or newer** on the main machine (NumPy 2.5 needs 3.12+).
  The `python3` that ships with macOS is 3.9, which is too old here;
  install Python from python.org or Homebrew. Team members who only run
  the collector can use Python 3.9 or newer, including that built-in
  `python3`.
- **Node.js 20.19+ or 22.12+** for the dashboard (`.nvmrc` pins 22).
- **Tk** for the Focus Assistant only (see [Focus Assistant](#focus-assistant)).
- **Linux:** `xdotool` and `xprintidle` (X11; Wayland sessions report
  the app as `Unknown`).
- **macOS:** window titles need permissions for the terminal that runs
  the collector (see [Permissions on macOS](#permissions-on-macos)).

## Quick start

### macOS and Linux

```bash
git clone <this repository> && cd Multimodal-Workspace-Analytics
python3.12 -m venv .venv         # or python3.13 / python3.14: any Python 3.12+
source .venv/bin/activate        # Homebrew Python refuses a global pip install (PEP 668)
python --version                 # must say 3.12 or newer
pip install -r requirements.txt
python start_project.py
```

Name the Python version explicitly: on a Mac with only the Xcode
command line tools, plain `python3` is 3.9 (`python3 --version` shows
which one you have). If `python3.12` is not found, install Python from
python.org or with Homebrew (`brew install python@3.12`), or use the
3.12+ version you have. With 3.9 the `pip install` stops at
`numpy==2.5.4`, and `start_project.py` refuses to start with the same
advice.

### Windows (PowerShell)

```powershell
git clone <this repository>; cd Multimodal-Workspace-Analytics
py -3.12 -m venv .venv          # or py -3.14
.venv\Scripts\Activate.ps1      # if scripts are blocked, use .venv\Scripts\python.exe instead of python
python -m pip install -r requirements.txt
python start_project.py
```

`start_project.py` starts the collector, the live pipeline, the API on
http://127.0.0.1:5050 and the dashboard on http://localhost:5173, and
opens the dashboard in your browser. The first run also installs the
dashboard's packages (`npm ci`); if that install was interrupted, or
`package-lock.json` has changed since, the next start runs `npm ci`
again. Press **Ctrl+C** to stop everything. The first sessions and
insights appear after a few minutes of collection.

## Using it

### Launcher options

| Command | What it does |
| --- | --- |
| `python start_project.py` | Single user: everything listens on this machine only. |
| `python start_project.py --team` | Also accepts team members: the API listens on the network (port 5050) and uses waitress. Other computers can use only the member endpoints (see [Main user](#main-user)). |
| `python start_project.py --behind-proxy` | For members connecting through an HTTPS reverse proxy on this machine (see [HTTPS for team mode](#https-for-team-mode)). The API stays on 127.0.0.1, without debug mode. Can't be combined with `--team`. |
| `python start_project.py --demo` | Also streams live demo team members into the team server. They are marked **Demo** and never affect real members' comparisons or totals (see [Demo data](#demo-data)). |
| `python start_project.py --sandbox` | The single-user demo: generated activity from `data/demo` instead of running the collector (see [Demo data](#demo-data)). With `--focus`, the Focus Assistant still watches this computer. |
| `python start_project.py --focus` | Also opens the Focus Assistant. |
| `python start_project.py --no-browser` | Doesn't open the browser. |

Options combine, e.g. `python start_project.py --sandbox --focus` or
`python start_project.py --team --demo`. `--help` lists them; `--help`
also works for `api/server.py`, `live_pipeline.py` and `run_pipeline.py`
and never starts them.

The launcher keeps the API on this machine unless you pass `--team` or
`--behind-proxy`. If `WORKSPACE_API_HOST=0.0.0.0` (every interface) is
left in your environment, it starts the API as `--team` does, so other
computers still reach only the member endpoints; any other address that
is not this machine is refused.

Only one launcher can run at a time. It keeps a lock file,
`data/.start_project.pid`, with its own PID and those of the services it
started. Stop it with **Ctrl+C**; on macOS and Linux `kill <launcher PID>`
(the PID is printed at startup) stops everything just as cleanly. If a
launcher was killed outright (`kill -9`, a crash), the next start stops
the services it left behind and takes over the lock. A lock whose PID now
belongs to some other program is ignored; if the launcher still says it
is already running when you know it isn't, delete
`data/.start_project.pid`. Vite always uses port 5173 and exits instead of
moving to another port when 5173 is taken.

### Running the parts yourself

From the project root, each in its own terminal:

```bash
python collector/collector.py    # a sample every 5 s into data/raw
python live_pipeline.py          # reprocesses every 10 s, writes data/processed/status.json
python api/server.py             # http://127.0.0.1:5050
cd frontend/Multimodal-Workspace-Analytics/frontend && npm run dev   # http://localhost:5173
```

`python run_pipeline.py` processes the raw file once and exits. If you
run the API on another port, start Vite with the same
`WORKSPACE_API_PORT` so its `/api` proxy follows. `api/server.py` runs
without Flask's debug mode; `WORKSPACE_API_DEBUG=1 python api/server.py`
turns it on, and only while the API listens on 127.0.0.1.

For team members, use `start_project.py --team` rather than
`WORKSPACE_API_HOST=0.0.0.0 python api/server.py`. Run by hand on the
network, the API answers your own data (`/api/analytics.csv`,
`/api/status`, `/api/focus/*`) only to this machine or to a request with
the admin key in the `X-Admin-Key` header, but the team admin endpoints
then accept the admin key from any computer. `--team` keeps everything
except the member endpoints on this machine. See the
[dashboard README](frontend/Multimodal-Workspace-Analytics/frontend/README.md)
for front-end development notes.

### Dashboard pages

| Page | Shows |
| --- | --- |
| Dashboard | Active and idle time, focus score, app and window switches, activity timeline, top apps, behaviour mix, CPU and memory, unusual episodes, rule-based insights |
| Sessions | Every session with start and end time, newest first; the current one has a **Live** badge |
| Applications | Time per app; the navbar search filters by app name or window title and lists the matching windows |
| Behavior | Behaviour clusters, their share of tracked time and their averages |
| Trends | Activity by hour (zero-filled for the last 24 hours, average per day for longer ranges) |
| Anomalies | Unusual finished sessions grouped into episodes, expandable to their samples |
| Insights | Peak and quiet hours, switching and idle patterns |
| Focus | The running Focus Assistant session, adherence per session, where deviations come from, session history |
| Team | Team analytics (needs the admin key; see [Team analytics](#team-analytics)) |
| Settings | Workspace name, compact tables, the anomaly alert dot, dataset and pipeline status |

### Demo data

None of the demo modes touch your own recorded activity, and demo team
members never change what the Team page says about your real members.

**Single-user sandbox (the single-user demo).**
`python start_project.py --sandbox` runs the whole platform on generated
activity instead of running the collector. A demo feed writes 8 hours of
history and then a realistic sample every 5 s (work blocks with 5 to 30
minute breaks) into `data/demo/`, and the pipeline, the API and the
Focus Assistant all use that folder. Your own `data/raw` is not read or
written. Delete `data/demo/` to throw the sandbox away.

The sandbox combines with `--focus`
(`python start_project.py --sandbox --focus`), but the Focus Assistant
is not simulated: it still watches this computer's foreground app and
window title, so the sandbox's Focus page shows your real apps and
windows, and they are logged to `data/demo/focus/`. Deleting `data/demo/`
deletes those Focus sessions too. Leave out `--focus` when you show the
sandbox to others.

**Live team demo.** `python start_project.py --demo` streams five demo
members into the running server's team database, the same one your
real members use, so members who already joined keep uploading. Demo
members go through the real invite, consent and upload flow, get three
days of history, then send a sample every 5 s and take breaks, so the
Team page shows people going offline and coming back. They are named
`Demo · ...`, carry a **Demo** badge and are analysed as their own
group: they never affect your real members' behaviour groups,
comparisons or team totals. They stay between runs, and the next
`--demo` resumes them. Remove them all with
`python -m team.live_demo --remove` while the server runs, or one at a
time with **Remove and delete data** on a demo member's page. Combine
with `--team` (`python start_project.py --team --demo`) to show them
next to real members.

**Offline team database.** `python -m team.simulate` fills a separate
database, `data/team/demo.db` (never your real `team.db`), with five
simulated members and three days of data each (`--days N`, `--reset` to
start over). View it with
`WORKSPACE_TEAM_DB=data/team/demo.db python start_project.py`
(PowerShell: `$env:WORKSPACE_TEAM_DB = "data\team\demo.db"`, then
`python start_project.py`, and `Remove-Item Env:WORKSPACE_TEAM_DB`
afterwards). Don't add `--team` while viewing it: your real members'
collectors would reach a server that doesn't know them and stop with
"token rejected". Their collectors keep the queue: once you restart
without `WORKSPACE_TEAM_DB`, `team_sync.py run` uploads it. They must
not run `leave`, which deletes it.

**Running the sandbox parts by hand.** Give each command the same
`WORKSPACE_DATA_DIR`, each in its own terminal, and stop
`start_project.py` first (it uses the same ports). Set it per command,
as below, rather than with `export`: a variable left in the terminal
would send a later real `python start_project.py` into the demo folder.

```bash
WORKSPACE_DATA_DIR=data/demo python -m collector.demo_feed --backfill-hours 6
WORKSPACE_DATA_DIR=data/demo python live_pipeline.py
WORKSPACE_DATA_DIR=data/demo python api/server.py
cd frontend/Multimodal-Workspace-Analytics/frontend && npm run dev
```

Relative paths are taken from the project folder. In PowerShell, set
`$env:WORKSPACE_DATA_DIR = "data\demo"` in each terminal and remove it
afterwards with `Remove-Item Env:WORKSPACE_DATA_DIR`. Feed options:
`--backfill-only` writes the history and exits, `--reset` starts from an
empty demo file, `--seed N` makes a run repeatable. The feed refuses to
write into the real `data/`.

## Team analytics

One main user runs the server; each member runs a small collector that
uploads to it.

```
member laptop                              main user's machine
-------------                              -------------------
collector/team_sync.py  --HTTP (or HTTPS)-->  api/server.py --> data/team/team.db (SQLite)
  local offline queue                         per-member ETL --> cross-member ML --> Team page
```

- Each member's collector samples every 5 s and uploads every 10 s with
  a personal token. While the server is unreachable, samples wait in a
  local queue.
- Each member's data goes through the ETL **separately**, so sessions
  and switches never mix people. Analysis covers the last 30 days by
  default.
- Cross-member ML on session-level features: **behaviour groups**
  (K-Means over everyone's sessions), **differs from team** (each
  member's typical session against everyone else's, with the features
  that drive the difference) and **unusual sessions** (far from that
  member's own norm).

### Main user

1. Start the server on the network: `python start_project.py --team`.
   Allow incoming connections when Windows Defender Firewall or macOS
   asks. Other computers can then use only what a member's collector
   needs: `/api/health`, `/api/team/join`, `/api/team/ingest` and
   `/api/team/me`. Everything else (your own activity log and window
   titles in `/api/analytics.csv`, `/api/status`, the Focus Assistant
   data and the admin endpoints) answers `403` to them, even with the
   admin key, and works only from this machine, where your dashboard
   reaches it through Vite.
2. Copy the **admin key** the API prints at startup (also saved in
   `data/team/admin_key`). Open the **Team** page and paste it.
3. Create an invite for each member. The invite shows a ready-to-run
   `join` command with this machine's address and port, and when the
   code expires (7 days). It is shown once; use **New code** in the
   Members table if it gets lost.
4. Before inviting people, have one member open
   `http://YOUR-IP:5050/api/health` in a browser. Campus and office Wi-Fi
   often stop laptops from reaching each other; if the page doesn't load,
   use a shared hotspot or a VPN such as Tailscale.

The Team page updates itself: the members list every 5 s, the analysis
every 30 s, and both pause while the tab is hidden or when you press
**Pause**. It has a **Now** column (each online member's current app,
window title and Active/Idle state), range buttons (Today means the last
24 hours, then 7 days, 30 days, All), **New code** and **Revoke** for
pending or expired invites, and **Lock**, which forgets the admin key in
this browser. A member's page adds a **Right now** card (current app and
the last 10 minutes) and an activity log that adds new samples as they
arrive.

### Each member

Members need a copy of this repository and Python 3.9 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements-collector.txt            # psutil (and pywin32 on Windows) only
python collector/team_sync.py join --server http://MAIN-USER-IP:5050 --invite CODE
python collector/team_sync.py run                    # Ctrl+C to stop
```

| Command | What it does |
| --- | --- |
| `join --server URL --invite CODE` | Lists exactly what will be shared and joins only after you type `yes`. |
| `run` | Collects and uploads. `--keep-local-copy` also writes `data/raw` for your own dashboard; `--quiet` stops printing each sample. |
| `status` | What the server holds about you, how many samples are queued, the oldest queued one, and any set aside. |
| `set-server URL` | Follows the server to a new address (a missing `http://` is added). It checks the address first; a running collector switches within a few seconds. |
| `leave` | Leaves the team and deletes everything you shared. |
| `leave --local-only` | Only forgets the team on this machine, for when the server is gone. Shared data stays until the main user removes you. |

If something goes wrong:

- **Server unreachable:** sampling continues and the queue grows; uploads
  are retried after 30 s, then 60 s, up to every 5 minutes.
- **Token rejected (401):** `run` stops but keeps the queue and settings.
  Ask the main user before cleaning up: if the server only runs another
  team database for a while, `run` works again once it is back. If the
  server moved, run `set-server`; if you were removed, run `leave` to
  clean up (it deletes the queue).
- **Batches the server keeps refusing** are set aside in the
  `dead_letter` table of `~/.workspace-analytics/team_queue.db` and
  counted by `status`.

### Consent and privacy

Members share timestamps, the foreground app, **window titles**,
active/idle status and CPU, memory and battery readings. `join` lists
exactly these fields and contacts the server only after the member types
`yes`, and the server issues a token only for that list. Tokens and
invite codes are stored only as hashes. The main user can remove a
member, which deletes their data and revokes their collector. Plain HTTP
is not encrypted; see [HTTPS for team mode](#https-for-team-mode).

The main user's own data is not shared: with `--team` or
`--behind-proxy`, other computers reach only the four member endpoints
listed under [Main user](#main-user). Whenever the API listens beyond
this machine, even when `python api/server.py` is started by hand with
`WORKSPACE_API_HOST=0.0.0.0`, your activity log, status and Focus data
answer other computers only with the admin key (`X-Admin-Key`); without
it they get `403`. `/api/health` answers everyone but holds no personal
data.

## Focus Assistant

```bash
python run_focus_assistant.py      # or: python start_project.py --focus
```

Choose the apps that count as focus, give the session a title, goal and
length, and start it. The assistant checks the foreground app every
second:

- Leaving your focus apps shows a reminder with **Allow for this
  session** and **Snooze 5 min**.
- After 60 s without input it shows **Paused (idle)**; idle time counts
  as neither focus nor deviation.
- Alt+Tab, the desktop, Spotlight, the lock screen and the assistant's
  own windows never count as deviations.
- A session left open by a crash is closed as *Interrupted* the next time
  the assistant starts.

Sessions are logged to `data/focus/` and the running session is written
to `data/focus/live.json` every second, which the dashboard's **Focus**
page reads (it shows the assistant as not running when that file is
older than 5 s). Run the assistant with the same `WORKSPACE_DATA_DIR` as
the API.

The assistant needs Tk. Without it, `run_focus_assistant.py` prints how
to install it for your system and exits:

| System | Install Tk |
| --- | --- |
| macOS, Homebrew Python | `brew install python-tk@3.14` (match your Python version) |
| macOS or Windows, python.org installer | Run the installer again, choose **Modify**, tick **tcl/tk and IDLE** |
| Debian / Ubuntu | `sudo apt install python3-tk` |
| Fedora | `sudo dnf install python3-tkinter` |

### Permissions on macOS

The collector and the Focus Assistant read the frontmost app through
System Events. Allow the app that runs them (for example Terminal):

- **System Settings > Privacy & Security > Automation**: allow it to
  control System Events, or every app shows as `Unknown`.
- **System Settings > Privacy & Security > Accessibility**: needed for
  window titles.

The collector prints a one-time hint when either is missing.

## Configuration

Set these environment variables before starting a process. `start_project.py`
passes them on to everything it starts. An empty value counts as unset.
Relative paths in `WORKSPACE_DATA_DIR` and `WORKSPACE_TEAM_DB` are taken
from the project folder.

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKSPACE_API_HOST` | `127.0.0.1` | Address the API listens on. `--team` sets `0.0.0.0`, `--behind-proxy` sets `127.0.0.1`. For `start_project.py` without either, `0.0.0.0` means `--team` and other non-local addresses are refused. Beyond this machine, your own data needs the admin key (see [API](#api)). |
| `WORKSPACE_API_PORT` | `5050` | API port, also used by the Vite proxy and the demo. Not 5000, which macOS AirPlay holds. |
| `WORKSPACE_API_DEBUG` | off | `1` turns on Flask's debug mode for `python api/server.py`, only while it listens on 127.0.0.1. Never with `--team` or `--behind-proxy`. |
| `WORKSPACE_DATA_DIR` | `data` | Folder for raw, processed and Focus Assistant data. `--sandbox` sets `data/demo`. |
| `WORKSPACE_DASHBOARD_URL` | `http://localhost:5173` | Dashboard address the launcher opens in your browser and the Focus Assistant links to. The launcher still waits for Vite on `http://localhost:5173`. |
| `WORKSPACE_TEAM_DB` | `data/team/team.db` | Team database used by the server, and so by `--demo`. |
| `WORKSPACE_TEAM_ADMIN_KEY` | generated, saved in `data/team/admin_key` | Admin key for the Team page. At least 16 characters; shorter values are ignored. |
| `WORKSPACE_TEAM_HOME` | `~/.workspace-analytics` | A member's token, settings and upload queue. |
| `WORKSPACE_TEAM_DEMO_STATE` | `data/team/live_demo.json` | Tokens of the live demo members. |

Fixed settings (collection every 5 s, idle after 60 s, sessions split
after 5 minutes, refresh every 10 s, members online for 120 s after an
upload, invites valid for 7 days) live in `config.py`.

## API

| Endpoint | Returns |
| --- | --- |
| `GET /api/health` | Server status |
| `GET /api/status` | Pipeline freshness: last run or error, rows, sessions, newest sample, file times, whether the collector has stopped |
| `GET /api/analytics.csv` | The processed dashboard data (ETag, so unchanged polls get `304`) |
| `GET /api/focus/live`, `/summary`, `/sessions` | Focus Assistant data |
| `/api/team/...` | Team endpoints: `join`, `ingest`, `me` for members; `invites`, `overview`, `live`, `members/<id>`, `members/<id>/activity`, `members/<id>/invite` for the main user (with the `X-Admin-Key` header) |

`/api/status`, `/api/analytics.csv` and `/api/focus/*` hold your own
data. They always answer requests from this machine (the dashboard goes
through Vite's proxy on 127.0.0.1). When the API listens beyond this
machine, other computers get them only with the admin key in the
`X-Admin-Key` header, and `403` otherwise. With `--team` or
`--behind-proxy` the launcher is stricter: other computers get `403`
from every endpoint except the four member endpoints, whatever key they
send. `/api/health` is open and reports only that the server is up and
whether processed data exists.

## Data and privacy

| Where | What | To delete |
| --- | --- | --- |
| `data/raw/` | Collector samples (`workspace_activity.csv`; older files renamed `workspace_activity.<date>.csv` when the columns change) | Stop the platform, delete the files |
| `data/processed/` | Pipeline output and `status.json` | Delete; rebuilt from `data/raw` |
| `data/focus/` | Focus sessions, events and `live.json` | Delete the folder |
| `data/team/` | Team database (with any live demo members), admin key, `demo.db` from `team.simulate`, live demo state | Remove members on the Team page, or delete the files |
| `data/demo/` | Single-user sandbox data (`--sandbox`) | Delete the folder |
| `~/.workspace-analytics/` (member) | Token, server address, upload queue | `team_sync.py leave` (or `leave --local-only`) |

Everything under `data/` except `data/sample/` is ignored by git, and so
are SQLite files and the collector, pipeline and Focus files anywhere
else in the project (for a relative `WORKSPACE_DATA_DIR` or
`WORKSPACE_TEAM_DB` outside `data/`). On Windows, the member token file
is protected by your user profile's permissions rather than file mode
bits.

## HTTPS for team mode

The API serves plain HTTP. To protect tokens and window titles in
transit:

- **Tailscale** (simplest): install it on the main user's and the
  members' machines and join with the main user's Tailscale address,
  e.g. `--server http://100.101.102.103:5050`. Tailscale encrypts the
  traffic between devices even though the address says `http://`.
- **Caddy**, when the main user's machine has a public DNS name with
  ports 80 and 443 reachable. Start the platform with
  `python start_project.py --behind-proxy`: the API stays on 127.0.0.1,
  runs on waitress without debug mode, and reads each visitor's address
  from Caddy's `X-Forwarded-For` header, so visitors still reach only the
  member endpoints. Then save this as `Caddyfile` and run `caddy run` in
  that folder (Caddy gets the certificate itself):

  ```caddyfile
  team.example.com {
      @members path /api/health /api/team/join /api/team/ingest /api/team/me
      handle @members {
          reverse_proxy 127.0.0.1:5050
      }
      handle {
          respond 404
      }
  }
  ```

  Only the member endpoints are forwarded; everything else gets `404`.
  Don't use the one-line `caddy reverse-proxy --to 127.0.0.1:5050`: it
  forwards every path, and an API started without `--behind-proxy`
  (`python start_project.py` or `python api/server.py`) sees every
  proxied visitor as this machine, so it would hand them your activity
  log, status and Focus data. Don't set `WORKSPACE_API_DEBUG=1` while a
  proxy forwards to the API either: Flask's debug pages show source code
  and file paths. Members join with `--server https://team.example.com`
  (replace the address in the invite's `join` command).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest                      # Python tests (only tests/ is collected; see pyproject.toml)

cd frontend/Multimodal-Workspace-Analytics/frontend
npm ci
npm run lint                          # oxlint
npm test                              # analytics unit tests (node --test)
npm run build
```

GitHub Actions (`.github/workflows/ci.yml`) runs on pushes to `main` and
on pull requests: the Python tests on Ubuntu, Windows and macOS with
Python 3.12 and 3.14, the member collector with only
`requirements-collector.txt` installed on Ubuntu, Windows and macOS with
Python 3.9 and 3.12 (import and `--help`), and the dashboard's lint,
tests and build on Node 22. The Python job also checks that `--help`
answers without starting `api/server.py`, `run_pipeline.py`,
`live_pipeline.py` or `start_project.py`. Every step runs in bash (Git
Bash on Windows), so a failing line fails the step. `tests/test_start_project.py`
and `tests/test_docs_devops_review.py` cover the launcher: its options,
what each one hands the services, the lock file, stopping on SIGTERM,
and the member-only filter of `--team`. `tests/test_server_access.py`
covers who may read your own data when the API listens on the network,
and when debug mode is allowed.

| File | Installs |
| --- | --- |
| `requirements.txt` | Everything the main machine needs to run the platform |
| `requirements-dev.txt` | The above plus pytest |
| `requirements-collector.txt` | What team members need for `team_sync.py` |

### Project structure

```
api/                 Flask app: server.py (analytics, status), focus.py (Focus API)
collector/           collector.py (sampling), team_sync.py (member collector), demo_feed.py
etl/                 session_processor.py: cleaning, switches, sessions, features
ml/                  clustering.py (K-Means, behaviour labels), anomaly_detection.py
focus/               Focus Assistant: focus_gui.py (Tk), focus_manager.py, analytics.py
team/                Team server: api.py, db.py, analysis.py, simulate.py, live_demo.py
frontend/Multimodal-Workspace-Analytics/frontend/   React dashboard
tests/               pytest suite
data/                Local data (ignored by git); data/sample holds an old synthetic
                     processed dataset that predates the window_title column
config.py            Paths, intervals, thresholds, ports
run_pipeline.py      One-off pipeline run; live_pipeline.py runs it every 10 s
start_project.py     Launcher
```

## Limitations

- No HTTPS inside the app; see [HTTPS for team mode](#https-for-team-mode).
- Team timestamps are each member's local time without a time zone, so
  members in other time zones or the hour when daylight saving time ends
  can be misread.
- The dashboard downloads and parses the whole processed CSV whenever it
  changes. That is fine for weeks of data but grows with history.
- The Linux collector needs X11.

## License

No license has been chosen yet. Until one is added, all rights are
reserved by the authors.
