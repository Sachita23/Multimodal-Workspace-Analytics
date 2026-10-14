"""
Team collector: run on each member's machine.

    python collector/team_sync.py join --server http://HOST:5000 --invite CODE
    python collector/team_sync.py run
    python collector/team_sync.py status
    python collector/team_sync.py leave

`join` shows exactly what will be shared and only contacts the
server after the member types "yes". `run` collects every few
seconds into a local queue and uploads it in batches, so nothing
is lost while the server is unreachable. `leave` removes the
member from the team and deletes everything they shared.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    COLLECTION_INTERVAL,
    TEAM_CONSENT_VERSION,
    TEAM_MAX_BATCH,
    TEAM_SHARED_FIELDS,
    TEAM_SYNC_INTERVAL,
)
from collector.collector import collect_data, display_data, save_data


STATE_DIR = Path(
    os.environ.get("WORKSPACE_TEAM_HOME", Path.home() / ".workspace-analytics")
)
CONFIG_FILE = STATE_DIR / "team.json"
QUEUE_FILE = STATE_DIR / "team_queue.db"

FIELD_DESCRIPTIONS = {
    "timestamp": "Time of each sample (every few seconds)",
    "application": "Name of the app in the foreground",
    "window_title": "Title of the foreground window (can include "
                    "document names, web page titles, email subjects)",
    "activity_status": "Whether you are Active or Idle",
    "idle_seconds": "Seconds since your last keyboard/mouse input",
    "cpu_percent": "CPU usage",
    "memory_percent": "Memory usage",
    "battery_percent": "Battery level",
    "charging": "Whether the laptop is charging",
}

NOT_COLLECTED = (
    "keystrokes, screen contents, screenshots, file contents, "
    "browser history, microphone or camera"
)


# --------------------------------------------------
# Local state
# --------------------------------------------------

def load_config():
    if not CONFIG_FILE.exists():
        return None
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(config):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)


def open_queue():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(QUEUE_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS queue "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
    )
    return conn


def enqueue(conn, observation):
    conn.execute(
        "INSERT INTO queue (payload) VALUES (?)",
        (json.dumps({k: observation[k] for k in TEAM_SHARED_FIELDS}),),
    )
    conn.commit()


def queued_count(conn):
    return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]


# --------------------------------------------------
# HTTP
# --------------------------------------------------

class ServerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def request_json(method, url, body=None, token=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("error", error.reason)
        except (ValueError, AttributeError):
            message = error.reason
        raise ServerError(error.code, message) from None


def warn_if_insecure(server):
    parsed = urlparse(server)

    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost", "127.0.0.1", "::1"
    ):
        print(
            "Note: this server uses plain HTTP, so your activity is not "
            "encrypted in transit. Only use it on a network you trust."
        )


# --------------------------------------------------
# Commands
# --------------------------------------------------

def show_consent(server):
    print()
    print("=" * 70)
    print("JOIN TEAM WORKSPACE ANALYTICS")
    print("=" * 70)
    print(f"Server: {server}")
    print()
    print("While the collector runs, it sends the following to the")
    print("team's main user every few seconds:")
    print()
    for field in TEAM_SHARED_FIELDS:
        print(f"  - {FIELD_DESCRIPTIONS[field]}")
    print()
    print(f"It does NOT collect {NOT_COLLECTED}.")
    print()
    print("The main user can see all of this, including individual")
    print("window titles, and compare you with the rest of the team.")
    print()
    print("Collection only happens while `team_sync.py run` is running.")
    print("Stop it at any time with Ctrl+C. Run `team_sync.py leave` to")
    print("leave the team and delete everything you have shared.")
    print("=" * 70)
    warn_if_insecure(server)


def cmd_join(args):
    if load_config():
        print("This machine has already joined a team. Run `leave` first.")
        return 1

    server = args.server.rstrip("/")
    show_consent(server)

    answer = input('\nType "yes" to agree and join: ').strip().lower()

    if answer != "yes":
        print("Not joined. Nothing was sent.")
        return 1

    try:
        result = request_json("POST", f"{server}/api/team/join", {
            "invite_code": args.invite,
            "consent": {
                "accepted": True,
                "version": TEAM_CONSENT_VERSION,
                "fields": TEAM_SHARED_FIELDS,
            },
        })
    except (ServerError, urllib.error.URLError, OSError) as error:
        print(f"Could not join: {error}")
        return 1

    save_config({
        "server": server,
        "token": result["token"],
        "member_id": result["member_id"],
        "name": result["name"],
        "consent_version": result["consent_version"],
    })

    print(f"\nJoined as {result['name']}. Start sharing with:")
    print("  python collector/team_sync.py run")
    return 0


def flush(conn, config):
    """
    Upload queued observations in batches. Returns False if the
    server has revoked this member.
    """

    while True:
        rows = conn.execute(
            "SELECT id, payload FROM queue ORDER BY id LIMIT ?",
            (TEAM_MAX_BATCH,),
        ).fetchall()

        if not rows:
            return True

        try:
            result = request_json(
                "POST",
                f"{config['server']}/api/team/ingest",
                {"observations": [json.loads(payload) for _, payload in rows]},
                token=config["token"],
            )

        except ServerError as error:
            if error.status == 401:
                return False
            print(f"[sync] Server rejected upload: {error}. Will retry.")
            return True

        except (urllib.error.URLError, OSError) as error:
            print(f"[sync] Server unreachable ({error}). "
                  f"{queued_count(conn)} observations queued.")
            return True

        conn.execute("DELETE FROM queue WHERE id <= ?", (rows[-1][0],))
        conn.commit()

        print(f"[sync] Uploaded {result['accepted']} observations "
              f"({result['rejected']} rejected).")


def cmd_run(args):
    config = load_config()

    if not config:
        print("Not joined yet. Run `team_sync.py join` first.")
        return 1

    if config.get("consent_version") != TEAM_CONSENT_VERSION:
        print("What this collector shares has changed since you agreed.")
        print("Run `leave`, then `join` again with a new invite.")
        return 1

    warn_if_insecure(config["server"])
    conn = open_queue()

    print(f"Sharing activity with {config['server']} as {config['name']}.")
    print("Press Ctrl+C to stop.")

    last_sync = 0.0

    try:
        while True:
            observation = collect_data()
            enqueue(conn, observation)

            if args.keep_local_copy:
                save_data(observation)

            if not args.quiet:
                display_data(observation)

            if time.monotonic() - last_sync >= TEAM_SYNC_INTERVAL:
                if not flush(conn, config):
                    print("The main user removed you from the team. "
                          "Stopping; local queue cleared.")
                    conn.execute("DELETE FROM queue")
                    conn.commit()
                    CONFIG_FILE.unlink(missing_ok=True)
                    return 1
                last_sync = time.monotonic()

            time.sleep(COLLECTION_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping. Uploading what is queued...")
        flush(conn, config)
        print("Stopped.")
        return 0

    finally:
        conn.close()


def cmd_status(_args):
    config = load_config()

    if not config:
        print("Not joined to a team.")
        return 0

    conn = open_queue()
    print(f"Server:  {config['server']}")
    print(f"Member:  {config['name']}")
    print(f"Queued locally (not yet uploaded): {queued_count(conn)}")
    conn.close()

    try:
        me = request_json(
            "GET", f"{config['server']}/api/team/me", token=config["token"]
        )
    except (ServerError, urllib.error.URLError, OSError) as error:
        print(f"Could not reach server: {error}")
        return 1

    print(f"Stored on server: {me['observations_stored']} observations "
          f"({me['first_observation']} to {me['last_observation']})")
    print(f"Fields shared: {', '.join(me['shared_fields'])}")
    return 0


def cmd_leave(_args):
    config = load_config()

    if not config:
        print("Not joined to a team.")
        return 0

    answer = input(
        "Leave the team and delete everything you shared from the "
        'server? Type "yes": '
    ).strip().lower()

    if answer != "yes":
        print("Nothing changed.")
        return 1

    try:
        request_json(
            "DELETE", f"{config['server']}/api/team/me", token=config["token"]
        )
    except ServerError as error:
        if error.status != 401:
            print(f"Server refused: {error}")
            return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not reach server, so your data was NOT deleted: {error}")
        return 1

    QUEUE_FILE.unlink(missing_ok=True)
    CONFIG_FILE.unlink(missing_ok=True)
    print("Left the team. Your data was deleted from the server.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    commands = parser.add_subparsers(dest="command", required=True)

    join = commands.add_parser("join", help="join a team with an invite code")
    join.add_argument("--server", required=True, help="e.g. http://192.168.1.20:5000")
    join.add_argument("--invite", required=True, help="invite code from the main user")
    join.set_defaults(func=cmd_join)

    run = commands.add_parser("run", help="collect and share activity")
    run.add_argument("--keep-local-copy", action="store_true",
                     help="also append to data/raw for your own dashboard")
    run.add_argument("--quiet", action="store_true", help="don't print each sample")
    run.set_defaults(func=cmd_run)

    commands.add_parser("status", help="show what is shared").set_defaults(func=cmd_status)
    commands.add_parser("leave", help="leave and delete your data").set_defaults(func=cmd_leave)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
