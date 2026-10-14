# Multimodal Workspace Analytics Platform

A workspace analytics platform that collects non-sensitive laptop activity metadata, processes the data through an ETL pipeline, identifies behavioral patterns using data analytics and machine learning, and presents intelligent insights through an interactive dashboard.

## Project Status

Currently working on:
- Workspace activity data collection
- Data storage
- ETL pipeline
- Data preprocessing

## Technology Stack

- Python
- Pandas
- Machine Learning
- PostgreSQL
- Azure
- GitHub
- GitHub Actions


## Team Analytics (multi-user)

One main user can collect activity from several team members and compare
them on the **Team** page of the dashboard.

### How it works

```
member laptop                          main user's machine
-------------                          -------------------
collector/team_sync.py  --HTTPS/HTTP-->  api/server.py  ->  data/team/team.db (SQLite)
  local offline queue                    per-member ETL  ->  cross-user ML  ->  Team page
```

- Each member's collector uploads batches every 30 seconds with a personal
  token. If the server is unreachable, samples wait in a local queue.
- Each member's data goes through the existing ETL **separately**, so
  sessions and app switches never mix people.
- Cross-user ML on session-level features:
  - **Behaviour groups**: K-Means over every member's sessions.
  - **Differs from team**: each member's typical session compared with
    everyone else's, listing the features that drive the difference.
  - **Unusual sessions**: sessions far from that member's own norm.

### Consent and privacy

Members share timestamps, the foreground app, **window titles**,
active/idle status and CPU/memory/battery. `join` lists exactly this and
only contacts the server after the member types `yes`. The server only
issues a token for that exact list of fields. Members can run
`team_sync.py status` to see what is stored, and `team_sync.py leave` to
leave and delete all their data. The main user can also remove a member,
which deletes their data and revokes their collector.

Tokens and invite codes are stored only as hashes. Plain HTTP is
unencrypted, so use a trusted network or put the server behind HTTPS.

### Main user

```bash
pip install -r requirements.txt

# Listen on the network so members can connect. Debug mode is switched
# off automatically when the host is not localhost.
WORKSPACE_API_HOST=0.0.0.0 WORKSPACE_API_PORT=5050 python start_project.py
```

Invites created on the Team page then include this machine's network
address, so members can copy the join command as-is. Before inviting
people, have one member open `http://YOUR-IP:5050/api/health` in a
browser: campus and office Wi-Fi often block laptops from reaching each
other. If it doesn't load, use a shared hotspot or a VPN such as Tailscale.

The server prints the **admin key**, which is also saved to
`data/team/admin_key`. Start the dashboard (`npm run dev` in
`frontend/Multimodal-Workspace-Analytics/frontend`), open **Team**, paste
the key, and create an invite for each member.

> **macOS:** AirPlay Receiver holds port 5000 on the network, which is why
> the command above uses 5050. The dashboard's proxy follows
> `WORKSPACE_API_PORT` automatically.

### Each member

```bash
pip install -r requirements.txt
python collector/team_sync.py join --server http://MAIN-USER-IP:5050 --invite CODE
python collector/team_sync.py run            # Ctrl+C to stop
python collector/team_sync.py status         # what is stored on the server
python collector/team_sync.py leave          # leave and delete your data
```

The collector works on Windows, macOS and Linux (X11). On macOS, window
titles need the Accessibility permission for the terminal running it
(System Settings > Privacy & Security > Accessibility). On Linux, install
`xdotool` and `xprintidle`.

### Try it with simulated members

```bash
python -m team.simulate --reset   # 5 members with different work styles
python api/server.py
```

### Tests

```bash
python -m pytest
```
