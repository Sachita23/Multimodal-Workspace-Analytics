"""
Team collector: run on each member's machine.

    python collector/team_sync.py join --server http://HOST:5050 --invite CODE
    python collector/team_sync.py run
    python collector/team_sync.py status
    python collector/team_sync.py set-server http://NEW-HOST:5050
    python collector/team_sync.py requeue-dead
    python collector/team_sync.py leave
    python collector/team_sync.py leave --local-only
    python collector/team_sync.py focus --minutes 25 --apps "Code,Terminal"
    python collector/team_sync.py focus --list-apps

`join` shows exactly what will be shared and only contacts the
server after the member types "yes". `run` collects every few
seconds into a local queue and uploads it in batches, so nothing
is lost while the server is unreachable. `set-server` follows the
main user's server to a new address. `requeue-dead` queues again
what the team server refused and `run` set aside. `leave` removes
the member from the team and deletes everything they shared;
`leave --local-only` only forgets the team on this machine (a
`run` still going in another terminal stops by itself).

`focus` runs a timed focus session in this terminal: only the apps
given with --apps are on task, and switching to any other app prints
an ERROR line, shows a desktop notification and lowers the session's
score (apps are never closed). After asking, the session is shared
with the team server every few seconds; --no-share keeps it on this
machine (no team needed). Uploads that fail are kept and sent later.
"""

import argparse
import contextlib
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:                        # one process at a time edits focus_pending.json
    import fcntl
except ImportError:         # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:         # not Windows
    msvcrt = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as project_config
from config import (
    COLLECTION_INTERVAL,
    TEAM_CONSENT_VERSION,
    TEAM_MAX_BATCH,
    TEAM_SHARED_FIELDS,
    TEAM_SYNC_INTERVAL,
)
from collector.collector import (
    clean_text,
    collect_data,
    display_data,
    next_deadline,
    save_data,
    sleep_until,
)


def state_dir_from_environment():
    """
    WORKSPACE_TEAM_HOME, or ~/.workspace-analytics when it is unset,
    empty or only whitespace (e.g. `export WORKSPACE_TEAM_HOME=$UNSET`):
    a blank value must never mean "the current folder".
    """

    configured = os.environ.get("WORKSPACE_TEAM_HOME", "").strip()

    if configured:
        return Path(configured).expanduser()

    return Path.home() / ".workspace-analytics"


STATE_DIR = state_dir_from_environment()
CONFIG_FILE = STATE_DIR / "team.json"
QUEUE_FILE = STATE_DIR / "team_queue.db"

# The team server keeps at most this many characters of each text
# field (MAX_TEXT_LENGTH in team/api.py), so longer window titles
# are cut before they are queued instead of being uploaded in full.
MAX_TEXT_LENGTH = 512

# Give up connecting after this long (server asleep, moved or
# firewalled) so sampling is not held up; a connected server still
# gets the full request timeout to answer.
CONNECT_TIMEOUT_SECONDS = 3

# Uploads per sync while `run` is sampling; a larger backlog is
# sent over the following samples.
MAX_BATCHES_PER_SYNC = 3

# After a failed upload, wait 30 s, then 60 s, ... up to 5 minutes.
BACKOFF_FIRST_SECONDS = 30
BACKOFF_MAX_SECONDS = 300

# A batch the team server refuses as invalid (400/413/422 with its
# own JSON error) is retried this many times, then moved to the
# dead_letter table so it cannot block everything queued after it.
REFUSED_BATCH_ATTEMPTS = 3

# `leave` waits this long for a collector still running in another
# terminal to notice and let go of the queue file (Windows cannot
# delete a file another process has open).
QUEUE_RELEASE_SECONDS = 20

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

# Focus sessions (`focus`).
FOCUS_TICK_SECONDS = getattr(project_config, "FOCUS_TICK_SECONDS", 1)
# A running session is uploaded this often, and once more at the end.
FOCUS_UPLOAD_SECONDS = 5
# Uploads run beside the timer; never wait long for a slow server.
FOCUS_UPLOAD_TIMEOUT_SECONDS = 5
# How long the end of `focus` waits for its last upload.
FOCUS_FINAL_UPLOAD_SECONDS = 20
FOCUS_MAX_MINUTES = 480
FOCUS_MAX_APPS = 30
FOCUS_FINISHED_STATUSES = ("Completed", "Stopped", "Interrupted")
FOCUS_DEFAULT_TITLE = "Focus session"

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


def config_version():
    try:
        return CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return None


def make_state_dir():
    # A new folder is private to this user. An existing one (maybe a
    # WORKSPACE_TEAM_HOME the user chose) is left as it is; the files
    # in it are owner-only either way.
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _make_private_file(path):
    """
    Create `path` readable by this user only, or tighten an existing
    one: the queue holds window titles (email subjects, document
    names). SQLite gives its journal files the same permissions.
    """

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return              # sqlite3 reports the real problem

    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
    except OSError:
        pass                # not our file; leave its permissions
    finally:
        os.close(fd)


def save_config(config):
    make_state_dir()
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)


def open_queue():
    make_state_dir()
    _make_private_file(QUEUE_FILE)
    conn = sqlite3.connect(QUEUE_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS queue "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dead_letter "
        "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
        "error TEXT, failed_at TEXT NOT NULL)"
    )
    return conn


def _upload_value(value):
    """
    Text made valid Unicode (a lone UTF-16 surrogate from a Windows
    title would make the server fail on the whole batch, again and
    again) and cut to what the server keeps. Other values unchanged.
    """

    if isinstance(value, str):
        return clean_text(value).strip()[:MAX_TEXT_LENGTH]

    return value


def enqueue(conn, observation):
    conn.execute(
        "INSERT INTO queue (payload) VALUES (?)",
        (json.dumps({
            field: _upload_value(observation[field])
            for field in TEAM_SHARED_FIELDS
        }),),
    )
    conn.commit()


def queued_count(conn):
    return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]


def oldest_queued_timestamp(conn):
    row = conn.execute(
        "SELECT payload FROM queue ORDER BY id LIMIT 1"
    ).fetchone()

    if not row:
        return None

    try:
        return json.loads(row[0]).get("timestamp")
    except (ValueError, AttributeError):
        return None


def dead_letter_count(conn):
    return conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]


def requeue_dead_letter(conn):
    """
    Put every row set aside in dead_letter back at the end of the
    upload queue. Returns how many were moved.
    """

    count = dead_letter_count(conn)
    conn.execute(
        "INSERT INTO queue (payload) SELECT payload FROM dead_letter ORDER BY id"
    )
    conn.execute("DELETE FROM dead_letter")
    conn.commit()
    return count


def move_to_dead_letter(conn, last_id, error):
    """
    Move every queued row up to last_id (one batch) out of the
    upload queue, keeping it for inspection.
    """

    conn.execute(
        "INSERT OR REPLACE INTO dead_letter (id, payload, error, failed_at) "
        "SELECT id, payload, ?, ? FROM queue WHERE id <= ?",
        (str(error), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), last_id),
    )
    conn.execute("DELETE FROM queue WHERE id <= ?", (last_id,))
    conn.commit()


# --------------------------------------------------
# HTTP
# --------------------------------------------------

class ServerError(Exception):
    """
    An HTTP error status. `from_team_api` is True when the answer
    was the team API's own JSON {"error": ...}, i.e. the team server
    itself refused the request (not a proxy or some other program
    answering at that address).
    """

    def __init__(self, status, message, from_team_api=False):
        super().__init__(message)
        self.status = status
        self.from_team_api = from_team_api


def server_error(status, reason, body):
    """
    The ServerError for an error answer whose body is `body` (bytes,
    or None when it could not be read).
    """

    try:
        answer = json.loads(body) if body else None
    except ValueError:
        answer = None

    if isinstance(answer, dict) and "error" in answer:
        return ServerError(status, answer["error"], from_team_api=True)

    return ServerError(status, reason)


class _ConnectTimeout:
    """
    Connection mixin: connect within CONNECT_TIMEOUT_SECONDS, then
    use the request's own timeout while waiting for the answer.
    """

    def connect(self):
        request_timeout = self.timeout
        has_timeout = isinstance(request_timeout, (int, float))

        self.timeout = (
            min(CONNECT_TIMEOUT_SECONDS, request_timeout)
            if has_timeout else CONNECT_TIMEOUT_SECONDS
        )

        try:
            super().connect()
        finally:
            self.timeout = request_timeout

        self.sock.settimeout(
            request_timeout if has_timeout else socket.getdefaulttimeout()
        )


class _HTTPConnection(_ConnectTimeout, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_ConnectTimeout, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


_opener = None


def _open(req, timeout):
    global _opener

    if _opener is None:
        _opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)

    return _opener.open(req, timeout=timeout)


def request_json(method, url, body=None, token=None, timeout=15):
    """
    Send a JSON request and return the decoded JSON answer.

    Raises ServerError for an HTTP error status (or an answer that
    is not JSON) and OSError (including urllib.error.URLError) when
    the server cannot be reached or the connection breaks. Nothing
    else: callers rely on these two to keep running.
    """

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with _open(req, timeout) as response:
            raw = response.read()
            status = response.status

    except urllib.error.HTTPError as error:
        # The error body can be cut off too (proxy or server dying
        # mid-answer); the status alone is still the answer.
        try:
            error_body = error.read()
        except (AttributeError, OSError, http.client.HTTPException):
            error_body = None
        raise server_error(error.code, error.reason, error_body) from None

    except http.client.HTTPException as error:
        raise ConnectionError(
            f"broken answer from server ({type(error).__name__})"
        ) from None

    except UnicodeError as error:
        # e.g. a host name with an empty label ("192.168.1..20")
        raise ConnectionError(
            f"could not contact {urlparse(url).netloc} ({error})"
        ) from None

    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise ServerError(
            status, "the answer was not JSON; is this the team server?"
        ) from None


def warn_if_insecure(server):
    parsed = urlparse(server)

    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost", "127.0.0.1", "::1"
    ):
        print(
            "Note: this server uses plain HTTP, so your activity is not "
            "encrypted in transit. Only use it on a network you trust."
        )


def normalise_server(url):
    """
    "192.168.1.20:5050/" -> "http://192.168.1.20:5050". Raises
    ValueError for anything that is not an http(s) address.
    """

    url = (url or "").strip()

    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        url = f"http://{url}"

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    try:
        parsed.port
        # Rejects empty or over-long labels ("192.168.1..20"), which
        # would otherwise only fail once a request is sent.
        if parsed.hostname:
            parsed.hostname.encode("idna")
    except (ValueError, UnicodeError):
        parsed = None

    if not parsed or scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"Not a server address: {url!r}. "
            "Use something like http://192.168.1.20:5050"
        )

    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


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

    try:
        server = normalise_server(args.server)
    except ValueError as error:
        print(error)
        return 1

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


class SyncState:
    """
    What `run` keeps between uploads: when to try next, how many
    attempts in a row have failed, the batch size the server takes,
    and how often the batch at the head of the queue was refused.
    """

    def __init__(self):
        self.batch_size = TEAM_MAX_BATCH
        self.failures = 0
        self.next_sync = 0.0
        self.refusals = {}

    def uploaded(self, more_waiting):
        self.failures = 0
        # With a backlog left, carry on at the next sample.
        self.next_sync = time.monotonic() + (
            0 if more_waiting else TEAM_SYNC_INTERVAL
        )

    def failed(self):
        """
        Record a failed attempt; returns the seconds until the next.
        """

        self.failures += 1
        delay = min(
            BACKOFF_MAX_SECONDS,
            BACKOFF_FIRST_SECONDS * 2 ** (self.failures - 1),
        )
        self.next_sync = time.monotonic() + delay
        return delay


def _refused(conn, state, rows, error):
    """
    The server refused a batch as invalid. Try it again at the next
    sync; after REFUSED_BATCH_ATTEMPTS, set it aside.
    """

    head = rows[0][0]
    attempts = state.refusals.get(head, 0) + 1

    if attempts < REFUSED_BATCH_ATTEMPTS:
        state.refusals[head] = attempts
        state.next_sync = time.monotonic() + TEAM_SYNC_INTERVAL
        print(f"[sync] Server rejected upload: {error}. Will retry "
              f"({attempts}/{REFUSED_BATCH_ATTEMPTS}).")
        return

    state.refusals.pop(head, None)
    move_to_dead_letter(conn, rows[-1][0], error)
    state.next_sync = time.monotonic()
    print(f"[sync] Server rejected {len(rows)} observations "
          f"{REFUSED_BATCH_ATTEMPTS} times ({error}). Moved them to the "
          f"dead_letter table in {QUEUE_FILE} and carrying on. To try "
          "them again later, run `team_sync.py requeue-dead`.")


def _batch_payload(rows):
    # Rows queued before text was cleaned at enqueue time are
    # cleaned here, so one of them cannot fail every upload.
    observations = []

    for _, payload in rows:
        observation = json.loads(payload)

        if isinstance(observation, dict):
            observation = {
                field: _upload_value(value)
                for field, value in observation.items()
            }

        observations.append(observation)

    return {"observations": observations}


def flush(conn, config, state=None, max_batches=None):
    """
    Upload queued observations in batches: all of them, or at most
    `max_batches` uploads. Returns False if the server no longer
    accepts this member's token, True otherwise. `state` records
    failures and when the next attempt is due.
    """

    if state is None:
        state = SyncState()

    sent = 0

    while max_batches is None or sent < max_batches:
        rows = conn.execute(
            "SELECT id, payload FROM queue ORDER BY id LIMIT ?",
            (state.batch_size,),
        ).fetchall()

        if not rows:
            state.uploaded(more_waiting=False)
            return True

        try:
            result = request_json(
                "POST",
                f"{config['server']}/api/team/ingest",
                _batch_payload(rows),
                token=config["token"],
            )

        except ServerError as error:
            if error.status == 401:
                return False

            if error.status == 413 and len(rows) > 1:
                state.batch_size = max(1, len(rows) // 2)
                print(f"[sync] Upload too large for the server; sending "
                      f"{state.batch_size} at a time.")
                continue

            # Only the team server's own refusal sets rows aside. A
            # 400 from something else at that address (a TLS proxy,
            # another web app) says nothing about the rows, so it is
            # backed off like any other wrong-address answer.
            if error.status in (400, 413, 422) and error.from_team_api:
                _refused(conn, state, rows, error)
                return True

            delay = state.failed()
            # Not a server-side failure: likely the wrong address.
            hint = (
                " Is this the team server? If it moved, run "
                "`team_sync.py set-server URL`."
                if error.status < 500 else ""
            )
            print(f"[sync] Server refused upload ({error.status}: {error}). "
                  f"{queued_count(conn)} observations queued; retrying in "
                  f"{delay} s.{hint}")
            return True

        except (urllib.error.URLError, OSError) as error:
            delay = state.failed()
            print(f"[sync] Server unreachable ({error}). "
                  f"{queued_count(conn)} observations queued; retrying in "
                  f"{delay} s.")
            return True

        # Only drop rows from the queue once the team server confirms.
        if not isinstance(result, dict) or "accepted" not in result:
            delay = state.failed()
            print(f"[sync] Unexpected answer from {config['server']}; is "
                  "this the team server? If it moved, run `team_sync.py "
                  f"set-server URL`. Retrying in {delay} s.")
            return True

        conn.execute("DELETE FROM queue WHERE id <= ?", (rows[-1][0],))
        conn.commit()
        state.refusals.pop(rows[0][0], None)
        sent += 1

        print(f"[sync] Uploaded {result['accepted']} observations "
              f"({result.get('rejected', 0)} rejected).")

    state.uploaded(more_waiting=queued_count(conn) > 0)
    return True


def print_token_rejected(conn):
    print()
    print("The server no longer accepts this collector's token: the main")
    print("user removed you, or the team server was reset or replaced.")
    print(f"Stopped. Your {queued_count(conn)} queued observations are kept "
          "on this machine.")
    print("  - If the main user's server moved to a new address, run")
    print("      python collector/team_sync.py set-server http://NEW-HOST:5050")
    print("  - To forget this team on this machine, run")
    print("      python collector/team_sync.py leave")


def _same_member(config, fresh):
    return (
        isinstance(fresh, dict)
        and fresh.get("token") == config["token"]
        and fresh.get("member_id") == config.get("member_id")
    )


def follow_server_change(config, state):
    """
    Re-read team.json after it changed on disk. A new server address
    for the same member is used from the next upload on, without
    waiting out the backoff. Returns (config, version seen); config
    is None when the team was forgotten on this machine (`leave` in
    another terminal deleted team.json) or replaced by a new `join`.
    """

    version = config_version()

    if version is None:
        return None, None               # team.json deleted

    try:
        fresh = load_config()
    except (OSError, ValueError):
        return config, None             # half-written; look again next sample

    if fresh is None:
        return None, None

    if not isinstance(fresh, dict):
        return config, None

    if not _same_member(config, fresh):
        return None, version

    if fresh.get("server") and fresh["server"] != config["server"]:
        print(f"[sync] Server address changed to {fresh['server']}.")
        config = {**config, "server": fresh["server"]}
        state.failures = 0
        state.next_sync = 0.0

    return config, version


def still_joined(config):
    """
    Whether team.json still holds this collector's membership.
    """

    try:
        return _same_member(config, load_config())
    except (OSError, ValueError):
        return True                     # being rewritten, not removed


def print_team_forgotten():
    print()
    print("The team settings on this machine were removed (`leave`) or "
          "replaced by a new `join`.")
    print("Stopped; nothing more is shared by this collector.")


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
    retry_focus_uploads(config)

    try:
        return _run_loop(conn, config, args)

    except sqlite3.Error:
        # `leave` in another terminal deleted the queue under us.
        if still_joined(config):
            raise
        print_team_forgotten()
        return 0

    finally:
        conn.close()


def _run_loop(conn, config, args):
    state = SyncState()
    seen_config = config_version()
    next_sample = time.monotonic()

    try:
        while True:
            observation = collect_data()

            # `set-server` or `leave` in another terminal: follow it
            # right away, and never queue or upload for a team that
            # was left.
            if config_version() != seen_config:
                config, seen_config = follow_server_change(config, state)

                if config is None:
                    print_team_forgotten()
                    return 0

            enqueue(conn, observation)

            if args.keep_local_copy:
                save_data(observation)

            if not args.quiet:
                display_data(observation)

            if time.monotonic() >= state.next_sync:
                if not flush(conn, config, state, MAX_BATCHES_PER_SYNC):
                    print_token_rejected(conn)
                    return 1

                # The server answers again: send focus sessions an
                # earlier `focus` could not upload.
                if state.failures == 0 and focus_pending_file().exists():
                    retry_focus_uploads(config)

            next_sample = next_deadline(next_sample, COLLECTION_INTERVAL)
            sleep_until(next_sample)

    except KeyboardInterrupt:
        if not still_joined(config):
            print_team_forgotten()
            return 0

        print("\nStopping. Uploading what is queued...")

        try:
            if not flush(conn, config):
                print_token_rejected(conn)
                return 0
        except KeyboardInterrupt:
            pass

        left = queued_count(conn)
        if left:
            print(f"{left} observations are still queued; they are sent "
                  "the next time you run the collector.")
        print("Stopped.")
        return 0


def cmd_status(_args):
    config = load_config()

    if not config:
        print("Not joined to a team.")
        return 0

    conn = open_queue()
    print(f"Server:  {config['server']}")
    print(f"Member:  {config['name']}")

    oldest = oldest_queued_timestamp(conn)
    print(f"Queued locally (not yet uploaded): {queued_count(conn)}"
          + (f" (oldest from {oldest})" if oldest else ""))

    pending_focus = len(load_focus_pending(config))
    if pending_focus:
        print(f"Focus sessions waiting to be uploaded: {pending_focus}")

    dead = dead_letter_count(conn)
    if dead:
        print(f"Refused by the server and set aside: {dead} "
              f"(dead_letter table in {QUEUE_FILE}; `requeue-dead` "
              "queues them again)")
    conn.close()

    try:
        me = request_json(
            "GET", f"{config['server']}/api/team/me", token=config["token"]
        )
    except ServerError as error:
        if error.status == 401:
            print("The server no longer accepts this collector's token. "
                  "Run `set-server` if the server moved, or `leave`.")
        else:
            print(f"Server refused: {error}")
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not reach server: {error}")
        return 1

    print(f"Stored on server: {me['observations_stored']} observations "
          f"({me['first_observation']} to {me['last_observation']})")
    print(f"Fields shared: {', '.join(me['shared_fields'])}")
    return 0


def cmd_set_server(args):
    config = load_config()

    if not config:
        print("Not joined to a team. Use `join` with an invite instead.")
        return 1

    try:
        server = normalise_server(args.url)
    except ValueError as error:
        print(error)
        return 1

    try:
        me = request_json("GET", f"{server}/api/team/me", token=config["token"])
    except ServerError as error:
        if error.status == 401:
            print(f"{server} does not know this collector's token, so the "
                  "address was not changed. If the main user started a new "
                  "team, run `leave --local-only` and `join` with a new invite.")
        else:
            print(f"{server} refused the check ({error}); the address was "
                  "not changed.")
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not reach {server} ({error}); the address was not "
              "changed.")
        return 1

    if me.get("member_id") != config.get("member_id"):
        print(f"{server} answered for a different member, so the address "
              "was not changed.")
        return 1

    config["server"] = server
    save_config(config)

    print(f"Server set to {server} (sharing as {me.get('name', config['name'])}).")
    print("A collector that is already running switches over within a few "
          "seconds.")
    warn_if_insecure(server)
    return 0


def _remove_file(path):
    path.unlink(missing_ok=True)


def remove_local_state():
    """
    Delete team.json, then the queue. team.json goes first, so a
    `run` still going in another terminal stops at its next sample
    even if the queue cannot be deleted yet: Windows refuses while
    that collector has the file open, so wait a little for it to
    stop, and failing that at least empty the queue.

    Returns True when the queued observations are gone.
    """

    _remove_file(CONFIG_FILE)
    # Focus sessions not yet uploaded belong to the team being left.
    with focus_pending_lock():
        _remove_file(focus_pending_file())

    try:
        _remove_file(focus_pending_lock_file())
    except OSError:
        pass                # still open in a `focus` on Windows

    deadline = time.monotonic() + QUEUE_RELEASE_SECONDS
    waiting = False

    while True:
        try:
            _remove_file(QUEUE_FILE)
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                break
            if not waiting:
                waiting = True
                print("Waiting for a running `team_sync.py run` to stop...")
            time.sleep(0.25)

    try:
        conn = sqlite3.connect(QUEUE_FILE)
        try:
            conn.execute("DELETE FROM queue")
            conn.execute("DELETE FROM dead_letter")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        print(f"Could not delete or empty {QUEUE_FILE} ({error}). Stop any "
              "`team_sync.py run` and delete that file by hand.")
        return False

    print(f"{QUEUE_FILE} is still in use, so it was emptied instead of "
          "deleted. Stop any `team_sync.py run` that is still running.")
    return True


def cmd_requeue_dead(_args):
    if not load_config():
        print("Not joined to a team.")
        return 1

    conn = open_queue()

    try:
        moved = requeue_dead_letter(conn)
    finally:
        conn.close()

    if moved:
        print(f"Queued {moved} set-aside observations again; `run` uploads "
              "them with the rest.")
    else:
        print("Nothing was set aside.")
    return 0


def forget_locally(config):
    conn = open_queue()
    queued = queued_count(conn)
    conn.close()

    answer = input(
        "Forget this team on this machine only? What you already shared "
        "stays on the server until the main user removes you, and "
        f"{queued} queued observations are discarded. Type \"yes\": "
    ).strip().lower()

    if answer != "yes":
        print("Nothing changed.")
        return 1

    queue_removed = remove_local_state()
    print(f"Forgot the team at {config['server']} on this machine. Ask the "
          "main user to remove you to delete what you shared.")
    return 0 if queue_removed else 1


def cmd_leave(args):
    config = load_config()

    if not config:
        print("Not joined to a team.")
        return 0

    if getattr(args, "local_only", False):
        return forget_locally(config)

    answer = input(
        "Leave the team and delete everything you shared from the "
        'server? Type "yes": '
    ).strip().lower()

    if answer != "yes":
        print("Nothing changed.")
        return 1

    already_removed = False

    try:
        request_json(
            "DELETE", f"{config['server']}/api/team/me", token=config["token"]
        )
    except ServerError as error:
        if error.status != 401:
            print(f"Server refused: {error}")
            return 1
        already_removed = True
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not reach server, so your data was NOT deleted: {error}")
        print("If the server moved, run `set-server URL` and try again; to "
              "forget the team on this machine anyway, run "
              "`leave --local-only`.")
        return 1

    queue_removed = remove_local_state()

    if already_removed:
        print("Left the team. The server had already removed you; local "
              "team settings and queue were deleted.")
    else:
        print("Left the team. Your data was deleted from the server.")
    return 0 if queue_removed else 1


# --------------------------------------------------
# Focus sessions
# --------------------------------------------------

def focus_data_dir():
    """
    Where `focus` keeps this machine's focus sessions (the same CSV
    files the dashboard's Focus page reads, separate from data/).
    """

    return STATE_DIR / "focus"


def focus_pending_file():
    return STATE_DIR / "focus_pending.json"


def focus_pending_lock_file():
    return STATE_DIR / "focus_pending.lock"


@contextlib.contextmanager
def focus_pending_lock():
    """
    Hold an exclusive lock on focus_pending.lock while reading and
    rewriting focus_pending.json: `run` and `focus` (two processes)
    both retry the rows in it. Best effort: when the lock cannot be
    taken the edit goes ahead anyway (the team server ignores an
    older row, so a lost race only delays an upload).
    """

    fd = None

    try:
        make_state_dir()
        fd = os.open(focus_pending_lock_file(), os.O_RDWR | os.O_CREAT, 0o600)

        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    except OSError:
        pass

    try:
        yield
    finally:
        if fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)


def create_focus_engine(data_dir, notifier):
    """
    The headless focus engine (focus/engine.py) for this machine.
    """

    from focus.engine import FocusEngine

    return FocusEngine(data_dir=data_dir, notifier=notifier, source="member")


def desktop_notify(title, message):
    """
    Best-effort desktop notification; never raises.
    """

    try:
        from focus.notify import notify

        notify(title, message)
    except Exception:  # a notification must never stop the session
        pass


def load_focus_pending(config=None):
    """
    {session_id: row} of focus uploads that failed earlier. Rows kept
    for a different membership (a `join` since) are dropped.
    """

    try:
        data = json.loads(focus_pending_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print(f"[focus] {focus_pending_file()} could not be read; its "
              "unsent focus sessions are skipped.")
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {}

    if config is not None and data.get("member_id") != config.get("member_id"):
        return {}

    return {
        sid: row for sid, row in data["sessions"].items()
        if isinstance(row, dict)
    }


def save_focus_pending(pending, config):
    """
    Keep unsent rows owner-only (they name apps and goals); delete
    the file once nothing is left.
    """

    path = focus_pending_file()

    if not pending:
        _remove_file(path)
        return

    make_state_dir()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(
            {"member_id": config.get("member_id"), "sessions": pending},
            file, indent=2,
        )

    os.replace(tmp, path)


class FocusUploadResult:
    def __init__(self):
        self.sent = 0
        self.pending = 0
        self.dropped = 0
        self.revoked = False
        self.error = None


def _focus_row_rank(row):
    """
    Which of two rows of one session is newer: a finished row beats a
    running one, then the later updated_at wins.
    """

    return (
        row.get("status") != "running",
        str(row.get("updated_at") or ""),
    )


def keep_focus_rows(config, rows):
    """
    Add `rows` to focus_pending.json (under its lock), keeping the
    newest row of each session, before anything is sent: a row is on
    disk even if this process ends during the upload. Returns the
    pending rows, or None when they could not be saved.
    """

    with focus_pending_lock():
        pending = load_focus_pending(config)
        changed = False

        for row in rows:
            old = pending.get(row["session_id"])

            if old is None or _focus_row_rank(row) >= _focus_row_rank(old):
                pending[row["session_id"]] = row
                changed = True

        if changed:
            try:
                save_focus_pending(pending, config)
            except OSError as error:
                print(f"[focus] Could not save unsent focus sessions ({error}).")
                return None

    return pending


def _forget_focus_row(config, row):
    """
    Remove a row the server answered from focus_pending.json, unless a
    newer row of that session was kept since (by this process or the
    other one). Returns how many rows are still pending.
    """

    with focus_pending_lock():
        pending = load_focus_pending(config)

        if pending.get(row["session_id"]) == row:
            pending.pop(row["session_id"])

            try:
                save_focus_pending(pending, config)
            except OSError as error:
                print(f"[focus] Could not save unsent focus sessions ({error}).")

    return len(pending)


def send_focus_rows(config, rows=()):
    """
    Upload `rows` plus every row kept from earlier failures, oldest
    first; only the newest row of each session is sent. Every row is
    written to focus_pending.json first and removed once the server
    has answered, so what cannot be sent now (or when this process
    ends mid-upload) is sent later. A row the team server refuses as
    invalid is dropped (it would never be accepted).
    """

    result = FocusUploadResult()
    rows = list(rows)
    pending = keep_focus_rows(config, rows)

    if pending is None:
        # Could not save: still try to send what we have in memory.
        pending = load_focus_pending(config)
        for row in rows:
            old = pending.get(row["session_id"])
            if old is None or _focus_row_rank(row) >= _focus_row_rank(old):
                pending[row["session_id"]] = row

    remaining = len(pending)

    for _sid, row in sorted(
        pending.items(), key=lambda item: str(item[1].get("updated_at", ""))
    ):
        try:
            answer = request_json(
                "POST",
                f"{config['server']}/api/team/focus",
                {"session": row},
                token=config["token"],
                timeout=FOCUS_UPLOAD_TIMEOUT_SECONDS,
            )
        except ServerError as error:
            if error.status == 401:
                result.revoked = True
                result.error = str(error)
                break

            if error.status in (400, 413, 422) and error.from_team_api:
                print(f"[focus] The team server refused focus session "
                      f"\"{row.get('title')}\" ({error}); it was not shared.")
                remaining = _forget_focus_row(config, row)
                result.dropped += 1
                continue

            result.error = f"{error.status}: {error}"
            break
        except (urllib.error.URLError, OSError) as error:
            result.error = str(error)
            break

        if not isinstance(answer, dict) or answer.get("ok") is not True:
            result.error = "unexpected answer; is this the team server?"
            break

        remaining = _forget_focus_row(config, row)
        result.sent += 1

    result.pending = remaining
    return result


def retry_focus_uploads(config):
    """
    Send focus sessions an earlier upload could not; quiet when there
    are none.
    """

    if not focus_pending_file().exists():
        return None

    result = send_focus_rows(config)

    if result.sent:
        print(f"[focus] Uploaded {result.sent} focus session update(s) "
              "kept from earlier.")
    if result.pending:
        print(f"[focus] {result.pending} focus session(s) still waiting to "
              f"be uploaded ({result.error}).")

    return result


class FocusUploader:
    """
    Sends the newest row of the running session in a background
    thread, so a slow or unreachable server never delays the
    once-per-second checks that catch a switch to another app.
    """

    def __init__(self, config):
        self.config = config
        self.revoked = False
        self.pending = 0
        self.last_error = None
        self.uploads = 0
        self._latest = None
        self._closed = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._work, name="focus-upload", daemon=True
        )
        self._thread.start()

    def submit(self, row):
        with self._lock:
            self._latest = row
        self._wake.set()

    def _work(self):
        while True:
            self._wake.wait()
            self._wake.clear()

            with self._lock:
                row, self._latest = self._latest, None
                closed = self._closed

            if row is not None and not self.revoked:
                try:
                    result = send_focus_rows(self.config, [row])
                except Exception as error:  # keep the timer running
                    self.last_error = str(error)
                else:
                    self.uploads += 1
                    self.pending = result.pending
                    self.last_error = result.error
                    self.revoked = self.revoked or result.revoked

            with self._lock:
                if closed and self._latest is None:
                    return

    def close(self, timeout=FOCUS_FINAL_UPLOAD_SECONDS):
        """
        Send what was submitted last, waiting at most `timeout`
        seconds. Returns True when the thread finished.
        """

        with self._lock:
            self._closed = True
        self._wake.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()


def parse_focus_apps(raw):
    """
    "Code, Terminal,code" -> ["Code", "Terminal"] (case-insensitive
    duplicates dropped, order kept).
    """

    apps = []
    seen = set()

    for part in (raw or "").split(","):
        app = part.strip()

        if app and app.lower() not in seen:
            apps.append(app[:MAX_TEXT_LENGTH])
            seen.add(app.lower())

    return apps


def _short(value, limit=MAX_TEXT_LENGTH):
    if value is None:
        return None
    return _upload_value(str(value))[:limit]


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _whole(value, default=0, high=None):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        number = default
    number = max(0, number)
    return min(number, high) if high is not None else number


def _seconds(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number, 1) if number > 0 and number != float("inf") else 0.0


def focus_session_score(deviations, deviation_seconds):
    """
    focus.scoring.session_score, or the same formula from config when
    that module is unavailable.
    """

    try:
        from focus.scoring import session_score

        return session_score(deviations, deviation_seconds)
    except Exception:
        start = getattr(project_config, "FOCUS_START_SCORE", 100)
        switch = getattr(project_config, "FOCUS_SWITCH_PENALTY", 5)
        per_minute = getattr(project_config, "FOCUS_OFF_TASK_PENALTY_PER_MINUTE", 6)
        return max(0, round(
            start - switch * deviations - per_minute * deviation_seconds / 60
        ))


def focus_record(data_dir, session_id, running):
    """
    The focus/analytics record of one session in data_dir (in-zone
    and off-task seconds, deviations, score), or None.
    """

    try:
        from focus import analytics

        records = analytics.session_records(
            data_dir, active_session_id=session_id if running else None
        )
    except Exception:
        return None

    return next((r for r in records if r.get("id") == session_id), None)


def focus_row(live, record=None, finished=None, plan=None):
    """
    The POST /api/team/focus "session" for a session: running (from
    the engine's live payload) or, with `finished` (the engine's
    session_json, or {} when the session ended by itself), its final
    row. `record` adds the analytics totals; `plan` holds what this
    command started the session with, for values live lacks.
    """

    plan = plan or {}
    record = record or {}
    live = live or {}
    done = finished is not None
    finished = finished or {}

    # While it runs the engine's live numbers are newest; at the end the
    # recorded session (the same rule, from the saved timeline) wins.
    sources = (record, finished, live) if done else (live, record)

    def pick(key):
        for source in sources:
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    deviations = _whole(pick("deviations"))
    deviation_seconds = _seconds(pick("deviation_seconds"))
    score = pick("score")

    if score is None:
        score = focus_session_score(deviations, deviation_seconds)

    planned_s = live.get("planned_s")
    planned_minutes = (
        _whole(planned_s / 60) if isinstance(planned_s, (int, float))
        else _whole(record.get("planned", plan.get("minutes", 0)))
    )

    apps = live.get("allowed_apps") or record.get("apps") or plan.get("apps") or []
    title = live.get("title") or record.get("title") or plan.get("title")
    goal = live.get("goal") or record.get("goal") or plan.get("goal") or ""
    started_at = live.get("started_at") or finished.get("start") or _now_iso()

    row = {
        "session_id": str(live.get("session_id") or finished.get("id")),
        "title": _short(title or FOCUS_DEFAULT_TITLE) or FOCUS_DEFAULT_TITLE,
        "goal": _short(goal) or "",
        "planned_minutes": planned_minutes,
        "allowed_apps": [
            _short(app) for app in apps if str(app).strip()
        ][:FOCUS_MAX_APPS],
        "started_at": started_at,
        "ended_at": None,
        "status": "running",
        "deviations": deviations,
        "deviation_seconds": deviation_seconds,
        "allowed_seconds": _seconds(record.get("allowed_seconds")),
        "score": _whole(score, high=100),
        "current_app": None,
        "current_allowed": None,
        "remaining_s": None,
        "updated_at": _now_iso(),
    }

    if done:
        status = finished.get("status") or record.get("status")
        row["status"] = status if status in FOCUS_FINISHED_STATUSES else "Stopped"
        row["ended_at"] = finished.get("end") or (
            record["end"].isoformat(timespec="seconds")
            if hasattr(record.get("end"), "isoformat") else None
        ) or _now_iso()
        row["ended_at"] = max(row["ended_at"], row["started_at"])
    else:
        allowed = live.get("allowed")
        row["current_app"] = _short(live.get("current_app"))
        row["current_allowed"] = allowed if isinstance(allowed, bool) else None
        row["remaining_s"] = _whole(live.get("remaining_s"))

    return row


def _red(text):
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[1;31m{text}\033[0m"
    return text


def _clock(seconds):
    seconds = _whole(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


FOCUS_STATE_LABELS = {
    "allowed": "on task",
    "deviation": "OFF TASK",
    "idle": "paused (idle)",
    "neutral": "system window",
    "self": "this window",
    "unknown": "window unknown",
}


def focus_status_line(live, uploader=None):
    score = live.get("score")
    deviations = _whole(live.get("deviations"))
    state = live.get("state")
    current = live.get("current_app") or "--"
    label = FOCUS_STATE_LABELS.get(state, state or "--")

    if state == "neutral" and live.get("status"):
        # e.g. the engine's start grace: "Starting - switch to ..."
        label = live["status"]

    parts = [
        f"{_clock(live.get('remaining_s'))} left",
        f"score {score if score is not None else '--'}",
        f"{deviations} deviation{'s' if deviations != 1 else ''}",
        f"{current}: {label}",
    ]

    if uploader is not None:
        if uploader.revoked:
            parts.append("not shared (token rejected)")
        elif uploader.pending:
            parts.append(f"upload pending ({uploader.pending})")
        elif uploader.uploads:
            parts.append("shared")

    return f"[{datetime.now():%H:%M:%S}] " + " | ".join(parts)


def deviation_key(live):
    last = live.get("last_deviation") if live else None

    if not isinstance(last, dict) or not last.get("at"):
        return None

    return (live.get("session_id"), last.get("at"), last.get("app"))


def deviation_message(live):
    last = live.get("last_deviation") or {}
    apps = ", ".join(live.get("allowed_apps") or []) or "your focus apps"
    penalty = last.get("penalty")
    penalty = getattr(project_config, "FOCUS_SWITCH_PENALTY", 5) if penalty is None else penalty

    # No score here: the engine counts the episode from its next tick,
    # so the score in this payload does not include the penalty yet.
    return (
        f"ERROR: You switched to {last.get('app') or 'another app'}, which is "
        f"not part of this focus session. -{penalty} points. Return to {apps}."
    )


def show_focus_sharing(config, title, goal, apps):
    print()
    print("=" * 70)
    print("SHARE THIS FOCUS SESSION")
    print("=" * 70)
    print(f"Server: {config['server']} (as {config.get('name')})")
    print()
    print(f"  Title:        {title}")
    print(f"  Goal:         {goal}")
    print(f"  Allowed apps: {', '.join(apps)}")
    print()
    print(f"While it runs, every {FOCUS_UPLOAD_SECONDS} seconds and once at the end, "
          "the main user receives:")
    print("  - the title, goal and allowed apps above, and the time left")
    print("  - the app you are in and whether it is allowed (so every")
    print("    deviation is shared with the app's name)")
    print("  - how many deviations, the time off task and your score")
    print("Window titles are not part of it.")
    print("=" * 70)
    warn_if_insecure(config["server"])


def confirm_share(server):
    try:
        answer = input(f"Share this session with {server}? [Y/n] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("", "y", "yes")


def cmd_list_apps():
    try:
        from focus.apps import recent_applications, running_applications
    except ImportError as error:
        print(f"Listing apps is not available ({error}).")
        return 1

    running = running_applications()
    recent = [app for app in recent_applications() if app not in running]

    print("Open now:  " + (", ".join(running) if running else "(none found)"))
    if recent:
        print("Recent:    " + ", ".join(recent))
    example = ",".join(running[:2]) if running else "Visual Studio Code,Terminal"
    print()
    print(f'Start with e.g.: python collector/team_sync.py focus --minutes 25 '
          f'--apps "{example}"')
    return 0


def cmd_focus(args):
    if args.list_apps:
        return cmd_list_apps()

    apps = parse_focus_apps(args.apps)

    if args.minutes is None or not 1 <= args.minutes <= FOCUS_MAX_MINUTES:
        print(f"Give the length with --minutes (1 to {FOCUS_MAX_MINUTES}).")
        return 1

    if not 1 <= len(apps) <= FOCUS_MAX_APPS:
        print(f'Give 1 to {FOCUS_MAX_APPS} allowed apps with --apps "App one,App two" '
              "(see `focus --list-apps`).")
        return 1

    title = (args.title or "").strip()[:MAX_TEXT_LENGTH] or FOCUS_DEFAULT_TITLE
    goal = (args.goal or "").strip()[:MAX_TEXT_LENGTH] or title

    config = None

    if not args.no_share:
        config = load_config()

        if not config:
            print("Not joined to a team, so this session cannot be shared. "
                  "Run `join` first, or add --no-share to keep it on this "
                  "machine.")
            return 1

        show_focus_sharing(config, title, goal, apps)

        try:
            if not args.yes and not confirm_share(config["server"]):
                print("Not shared: the session runs on this machine only.")
                config = None

            if config:
                retry_focus_uploads(config)
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 130

    engine = create_focus_engine(focus_data_dir(), desktop_notify)

    try:
        live = engine.start(title, args.minutes, apps, goal=goal)
    except (RuntimeError, ValueError) as error:
        # FocusBusy (a RuntimeError): a session or Focus Assistant
        # already runs here.
        print(f"Could not start the focus session: {error}")
        return 1

    plan = {"title": title, "goal": goal, "apps": apps, "minutes": args.minutes}
    return _focus_loop(engine, live, config, plan)


class FocusSignal(KeyboardInterrupt):
    """
    SIGTERM or SIGHUP (the terminal was closed) during a session: it
    ends as Interrupted, and its final row is still kept and sent.
    """


def _raise_focus_signal(signum, _frame):
    raise FocusSignal(signum)


@contextlib.contextmanager
def focus_signals():
    """
    Turn SIGTERM / SIGHUP into FocusSignal while a session runs, so
    closing the terminal ends it like Ctrl+C instead of losing the
    final row. Only possible in the main thread.
    """

    previous = {}

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)

        if number is None:
            continue

        try:
            previous[number] = signal.signal(number, _raise_focus_signal)
        except (ValueError, OSError):  # not the main thread
            pass

    try:
        yield
    finally:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):
                pass


def _focus_loop(engine, live, config, plan):
    with focus_signals():
        return _run_focus_loop(engine, live, config, plan)


def _run_focus_loop(engine, live, config, plan):
    uploader = FocusUploader(config) if config else None
    session_id = live.get("session_id")
    seen = deviation_key(live)
    next_upload = next_tick = time.monotonic()
    finished = None

    print()
    print(f"Focus session started: {plan['title']} for {plan['minutes']} min. "
          f"Allowed: {', '.join(plan['apps'])}.")
    print("Switching to any other app is an error and lowers your score. "
          "Press Ctrl+C to stop.")

    try:
        while True:
            print(focus_status_line(live, uploader), flush=True)

            if uploader is not None and time.monotonic() >= next_upload:
                uploader.submit(focus_row(
                    live, focus_record(focus_data_dir(), session_id, True),
                    plan=plan,
                ))
                next_upload = time.monotonic() + FOCUS_UPLOAD_SECONDS

            # Tick on a fixed beat (no drift from slow window queries);
            # after a stall, carry on from now rather than catch up.
            next_tick += FOCUS_TICK_SECONDS
            focus_sleep(max(0.0, next_tick - time.monotonic()))
            next_tick = max(next_tick, time.monotonic())
            latest = engine.tick() or {}

            if not latest.get("running"):
                break

            # After the session ends, tick() returns the idle heartbeat;
            # `live` keeps the last running payload for the final row.
            live = latest
            key = deviation_key(live)

            if key is not None and key != seen:
                print(_red(deviation_message(live)), flush=True)

            seen = key or seen

        # Ended by itself (time is up): the engine kept its session_json.
        finished = getattr(engine, "last_session", None) or {"status": "Completed"}

    except KeyboardInterrupt as stop:
        status = "Interrupted" if isinstance(stop, FocusSignal) else "Stopped"
        print("\nStopping the focus session...")
        finished = _stop_engine(engine, status)

    except BaseException:
        finished = _stop_engine(engine, "Interrupted")
        if uploader is not None:
            uploader.close(timeout=0)
            # Keep the end for the next `focus` or `run` to send.
            try:
                keep_focus_rows(config, [focus_row(
                    live, focus_record(focus_data_dir(), session_id, False),
                    finished=finished or {"status": "Interrupted"}, plan=plan,
                )])
            except Exception:
                pass
        raise

    record = focus_record(focus_data_dir(), session_id, False)
    final = focus_row(live, record, finished=finished, plan=plan)
    # On disk before anything else (printing included: the terminal may
    # be gone), so the end is never lost if this process stops now.
    kept = keep_focus_rows(config, [final]) is not None if config else False
    print_focus_summary(final)

    if uploader is not None:
        uploader.submit(final)
        print("Uploading the session...")

        try:
            done = uploader.close()
        except KeyboardInterrupt:
            done = False

        if uploader.revoked:
            print("The team server no longer accepts this collector's token; "
                  "the session was kept on this machine only.")
        elif not done or uploader.pending:
            if kept:
                print("The session could not be uploaded yet; it is sent the "
                      "next time you run `focus` or `run`.")
            else:
                print("The session could not be uploaded, and it could not "
                      "be saved for later either.")
        else:
            print("Shared with the team.")

    return 0


def focus_sleep(seconds):
    time.sleep(seconds)


def _stop_engine(engine, status):
    try:
        return engine.stop(status) or {}
    except Exception as error:  # report, but keep what was recorded
        print(f"Could not stop the session cleanly ({error}).")
        return {}


def print_focus_summary(row):
    print()
    print(f"Focus session {row['status'].lower()}: {row['title']}")
    print(f"  Score:      {row['score']} / 100")
    print(f"  Deviations: {row['deviations']} "
          f"({_clock(row['deviation_seconds'])} off task)")
    print(f"  On task:    {_clock(row['allowed_seconds'])}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    commands = parser.add_subparsers(dest="command", required=True)

    join = commands.add_parser("join", help="join a team with an invite code")
    join.add_argument("--server", required=True, help="e.g. http://192.168.1.20:5050")
    join.add_argument("--invite", required=True, help="invite code from the main user")
    join.set_defaults(func=cmd_join)

    run = commands.add_parser("run", help="collect and share activity")
    run.add_argument("--keep-local-copy", action="store_true",
                     help="also append to data/raw for your own dashboard")
    run.add_argument("--quiet", action="store_true", help="don't print each sample")
    run.set_defaults(func=cmd_run)

    commands.add_parser("status", help="show what is shared").set_defaults(func=cmd_status)

    set_server = commands.add_parser(
        "set-server", help="follow the team server to a new address"
    )
    set_server.add_argument("url", help="e.g. http://192.168.1.21:5050")
    set_server.set_defaults(func=cmd_set_server)

    commands.add_parser(
        "requeue-dead",
        help="queue observations the server refused (dead_letter) again",
    ).set_defaults(func=cmd_requeue_dead)

    leave = commands.add_parser("leave", help="leave and delete your data")
    leave.add_argument("--local-only", action="store_true",
                       help="only forget the team on this machine (when the "
                            "server is gone); shared data stays on the server")
    leave.set_defaults(func=cmd_leave)

    focus = commands.add_parser(
        "focus",
        help="run a timed focus session (only the given apps count as on task)",
    )
    focus.add_argument("--minutes", type=int,
                       help=f"session length, 1 to {FOCUS_MAX_MINUTES}")
    focus.add_argument("--apps", help='allowed apps, e.g. "Visual Studio Code,Terminal"')
    focus.add_argument("--title", help=f'what you are working on (default "{FOCUS_DEFAULT_TITLE}")')
    focus.add_argument("--goal", help="what you want to get done (default: the title)")
    focus.add_argument("--yes", action="store_true",
                       help="share with the team server without asking")
    focus.add_argument("--no-share", action="store_true",
                       help="keep the session on this machine (no team needed)")
    focus.add_argument("--list-apps", action="store_true",
                       help="list open and recently used apps, then exit")
    focus.set_defaults(func=cmd_focus)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
