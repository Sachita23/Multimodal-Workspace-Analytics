"""
Team collector: run on each member's machine.

    python collector/team_sync.py join --server http://HOST:5050 --invite CODE
    python collector/team_sync.py run
    python collector/team_sync.py status
    python collector/team_sync.py set-server http://NEW-HOST:5050
    python collector/team_sync.py requeue-dead
    python collector/team_sync.py leave
    python collector/team_sync.py leave --local-only

`join` shows exactly what will be shared and only contacts the
server after the member types "yes". `run` collects every few
seconds into a local queue and uploads it in batches, so nothing
is lost while the server is unreachable. `set-server` follows the
main user's server to a new address. `requeue-dead` queues again
what the team server refused and `run` set aside. `leave` removes
the member from the team and deletes everything they shared;
`leave --local-only` only forgets the team on this machine (a
`run` still going in another terminal stops by itself).
"""

import argparse
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
