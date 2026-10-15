"""
The member collector, run against the real API through Flask's
test client instead of a network.
"""

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from collector import team_sync
from team import db
from tests.conftest import PROJECT_ROOT, observation

# The `sync` fixture routes request_json to the Flask test client;
# this is the real one, for tests that need actual HTTP behaviour.
REAL_REQUEST_JSON = team_sync.request_json


# Rows stamped within the last hour, which the server accepts.
BASE_TIME = (datetime.now() - timedelta(hours=1)).replace(microsecond=0)


def stamp(offset_seconds=0):
    return (BASE_TIME + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@pytest.fixture
def sync(tmp_path, monkeypatch, client):
    """
    Point team_sync's state at a temp dir and route its HTTP
    calls to the test client.
    """

    monkeypatch.setattr(team_sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(team_sync, "CONFIG_FILE", tmp_path / "state" / "team.json")
    monkeypatch.setattr(team_sync, "QUEUE_FILE", tmp_path / "state" / "queue.db")

    calls = []

    def fake_request(method, url, body=None, token=None, timeout=15):
        calls.append((method, urlparse(url).path))
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.open(urlparse(url).path, method=method,
                               json=body, headers=headers)
        if response.status_code >= 400:
            # Parsed the way request_json parses a real error answer.
            raise team_sync.server_error(
                response.status_code, response.status, response.get_data()
            )
        return response.get_json()

    monkeypatch.setattr(team_sync, "request_json", fake_request)
    return calls


def _join(monkeypatch, invite_code, answer="yes"):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    return team_sync.main(
        ["join", "--server", "http://127.0.0.1:5050", "--invite", invite_code]
    )


def _offline(*_args, **_kwargs):
    raise OSError("connection refused")


def test_declining_consent_sends_nothing(sync, monkeypatch, invite, capsys):
    assert _join(monkeypatch, invite(), answer="no") == 1

    assert sync == []
    assert not team_sync.CONFIG_FILE.exists()


def test_consent_screen_lists_window_titles(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite(), answer="no")
    out = capsys.readouterr().out

    assert "window" in out.lower()
    assert "does not collect keystrokes" in out.lower()


def test_join_saves_private_config(sync, monkeypatch, invite):
    assert _join(monkeypatch, invite("Asha")) == 0

    config = json.loads(team_sync.CONFIG_FILE.read_text())
    assert config["name"] == "Asha"
    assert config["token"]
    assert config["server"] == "http://127.0.0.1:5050"

    # Windows has no POSIX permission bits; the profile ACL protects it.
    if sys.platform != "win32":
        assert team_sync.CONFIG_FILE.stat().st_mode & 0o077 == 0

        # The queue holds window titles: owner-only as well.
        team_sync.open_queue().close()
        assert team_sync.QUEUE_FILE.stat().st_mode & 0o077 == 0
        assert team_sync.STATE_DIR.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_existing_world_readable_queue_is_made_private(sync):
    team_sync.STATE_DIR.mkdir(mode=0o755)
    team_sync.QUEUE_FILE.touch()
    os.chmod(team_sync.QUEUE_FILE, 0o644)

    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation())
    conn.close()

    assert team_sync.QUEUE_FILE.stat().st_mode & 0o777 == 0o600
    # A folder the user chose is left as it is.
    assert team_sync.STATE_DIR.stat().st_mode & 0o777 == 0o755


# --------------------------------------------------
# WORKSPACE_TEAM_HOME
# --------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_blank_team_home_means_the_default_folder(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WORKSPACE_TEAM_HOME", raising=False)
    else:
        monkeypatch.setenv("WORKSPACE_TEAM_HOME", value)

    assert team_sync.state_dir_from_environment() == (
        Path.home() / ".workspace-analytics"
    )


def test_team_home_is_used_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_TEAM_HOME", f"  {tmp_path}  ")
    assert team_sync.state_dir_from_environment() == tmp_path

    monkeypatch.setenv("WORKSPACE_TEAM_HOME", "~/team-home")
    assert team_sync.state_dir_from_environment() == Path.home() / "team-home"


def test_blank_team_home_is_never_the_current_folder_at_import(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()

    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "WORKSPACE_TEAM_HOME": "   ",
        "WORKSPACE_DATA_DIR": str(tmp_path / "data"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c",
         "from collector import team_sync; print(team_sync.CONFIG_FILE)"],
        cwd=work, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == home / ".workspace-analytics" / "team.json"
    assert list(work.iterdir()) == []


# --------------------------------------------------
# What is uploaded
# --------------------------------------------------

def test_window_title_with_a_lone_surrogate_does_not_block_the_queue(
    sync, monkeypatch, invite, db_file
):
    # A Windows title cut in the middle of an emoji.
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()

    team_sync.enqueue(conn, observation(stamp(0), window_title="Chat with Ana \ud83d"))
    # A row queued before titles were cleaned at enqueue time.
    conn.execute(
        "INSERT INTO queue (payload) VALUES (?)",
        (json.dumps(observation(stamp(5), application="Slack \udc80")),),
    )
    conn.commit()
    team_sync.enqueue(conn, observation(stamp(10)))

    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 0
    assert team_sync.dead_letter_count(conn) == 0
    conn.close()

    with db.connect(db_file) as c:
        stored = c.execute(
            "SELECT application, window_title FROM observations ORDER BY timestamp"
        ).fetchall()

    assert [tuple(row) for row in stored] == [
        ("Visual Studio Code", "Chat with Ana �"),
        ("Slack �", "main.py - VS Code"),
        ("Visual Studio Code", "main.py - VS Code"),
    ]


def test_long_titles_are_cut_to_what_the_server_keeps(sync):
    from team import api

    assert team_sync.MAX_TEXT_LENGTH == api.MAX_TEXT_LENGTH

    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(window_title="  " + "é" * 5000))

    payload = json.loads(conn.execute("SELECT payload FROM queue").fetchone()[0])
    assert payload["window_title"] == "é" * team_sync.MAX_TEXT_LENGTH
    conn.close()


def test_join_accepts_an_address_without_scheme(sync, monkeypatch, invite):
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert team_sync.main(
        ["join", "--server", "127.0.0.1:5050/", "--invite", invite()]
    ) == 0
    assert team_sync.load_config()["server"] == "http://127.0.0.1:5050"


def test_queue_survives_outage_then_uploads(sync, monkeypatch, invite, db_file):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()

    for second in range(0, 30, 5):
        team_sync.enqueue(conn, observation(stamp(second)))

    real = team_sync.request_json
    monkeypatch.setattr(team_sync, "request_json", _offline)
    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 6

    monkeypatch.setattr(team_sync, "request_json", real)
    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 0

    with db.connect(db_file) as c:
        assert c.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 6


def test_queue_only_holds_consented_fields(sync, monkeypatch, invite):
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, {**observation(), "extra_secret": "should not be sent"})

    payload = json.loads(conn.execute("SELECT payload FROM queue").fetchone()[0])
    assert "extra_secret" not in payload


def test_removed_member_stops_and_keeps_queue_and_config(
    sync, monkeypatch, invite, client, admin, capsys
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))

    client.delete(f"/api/team/members/{config['member_id']}", headers=admin)

    assert team_sync.flush(conn, config) is False
    conn.close()

    # `run` stops on the 401 but deletes nothing.
    monkeypatch.setattr(team_sync, "collect_data", lambda: observation(stamp(5)))
    assert team_sync.main(["run", "--quiet"]) == 1

    out = capsys.readouterr().out
    assert "set-server" in out
    assert "leave" in out
    assert team_sync.load_config() == config

    conn = team_sync.open_queue()
    assert team_sync.queued_count(conn) == 2
    conn.close()


def test_leave_deletes_server_data(sync, monkeypatch, invite, db_file):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))
    team_sync.flush(conn, config)
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert team_sync.main(["leave"]) == 0

    assert db.get_member(config["member_id"], db_file) is None
    assert not team_sync.CONFIG_FILE.exists()


def test_leave_after_removal_clears_local_state(
    sync, monkeypatch, invite, client, admin, capsys
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    client.delete(f"/api/team/members/{config['member_id']}", headers=admin)

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert team_sync.main(["leave"]) == 0

    assert not team_sync.CONFIG_FILE.exists()
    assert "already removed" in capsys.readouterr().out


def test_leave_local_only_does_not_contact_server(sync, monkeypatch, invite, db_file):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))
    conn.close()
    sync.clear()

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert team_sync.main(["leave", "--local-only"]) == 0

    assert sync == []
    assert not team_sync.CONFIG_FILE.exists()
    assert not team_sync.QUEUE_FILE.exists()
    assert db.get_member(config["member_id"], db_file) is not None


def test_unreachable_leave_suggests_local_only(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite())
    monkeypatch.setattr(team_sync, "request_json", _offline)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert team_sync.main(["leave"]) == 1

    assert "--local-only" in capsys.readouterr().out
    assert team_sync.CONFIG_FILE.exists()


# --------------------------------------------------
# Retry behaviour
# --------------------------------------------------

def test_backoff_doubles_up_to_five_minutes_then_resets(sync, monkeypatch, invite):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))

    real = team_sync.request_json
    monkeypatch.setattr(team_sync, "request_json", _offline)
    state = team_sync.SyncState()
    delays = []

    for _ in range(6):
        before = time.monotonic()
        assert team_sync.flush(conn, config, state, team_sync.MAX_BATCHES_PER_SYNC)
        delays.append(round(state.next_sync - before))

    assert delays == [30, 60, 120, 240, 300, 300]
    assert team_sync.queued_count(conn) == 1

    monkeypatch.setattr(team_sync, "request_json", real)
    before = time.monotonic()
    assert team_sync.flush(conn, config, state, team_sync.MAX_BATCHES_PER_SYNC)

    assert state.failures == 0
    assert team_sync.queued_count(conn) == 0
    assert round(state.next_sync - before) == team_sync.TEAM_SYNC_INTERVAL


def test_413_halves_the_batch_until_the_server_accepts_it(
    sync, monkeypatch, invite, db_file
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()

    for second in range(0, 50, 5):
        team_sync.enqueue(conn, observation(stamp(second)))

    real = team_sync.request_json
    sizes = []

    def server_limit_3(method, url, body=None, token=None, timeout=15):
        if url.endswith("/ingest"):
            sizes.append(len(body["observations"]))
            if len(body["observations"]) > 3:
                raise team_sync.ServerError(413, "At most 3 observations per upload.")
        return real(method, url, body, token, timeout)

    monkeypatch.setattr(team_sync, "request_json", server_limit_3)
    state = team_sync.SyncState()

    assert team_sync.flush(conn, config, state) is True

    assert sizes == [10, 5, 2, 2, 2, 2, 2]
    assert state.batch_size == 2
    assert team_sync.queued_count(conn) == 0

    with db.connect(db_file) as c:
        assert c.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 10


def test_refused_batch_goes_to_dead_letter_after_retries(
    sync, monkeypatch, invite, capsys
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))
    team_sync.enqueue(conn, observation(stamp(5)))

    real = team_sync.request_json

    def refusing(*_args, **_kwargs):
        raise team_sync.server_error(
            400, "BAD REQUEST", b'{"error": "Expected an \'observations\' list."}'
        )

    monkeypatch.setattr(team_sync, "request_json", refusing)
    state = team_sync.SyncState()

    for _ in range(team_sync.REFUSED_BATCH_ATTEMPTS - 1):
        assert team_sync.flush(conn, config, state, team_sync.MAX_BATCHES_PER_SYNC)
        assert team_sync.queued_count(conn) == 2

    assert team_sync.flush(conn, config, state, team_sync.MAX_BATCHES_PER_SYNC)
    assert team_sync.queued_count(conn) == 0
    assert team_sync.dead_letter_count(conn) == 2
    out = capsys.readouterr().out
    assert "dead_letter" in out
    assert "requeue-dead" in out

    # Later observations are no longer stuck behind the bad batch.
    monkeypatch.setattr(team_sync, "request_json", real)
    team_sync.enqueue(conn, observation(stamp(10)))
    assert team_sync.flush(conn, config, state) is True
    assert team_sync.queued_count(conn) == 0
    conn.close()

    # ...and the set-aside rows can be sent again.
    assert team_sync.main(["requeue-dead"]) == 0
    conn = team_sync.open_queue()
    assert team_sync.dead_letter_count(conn) == 0
    assert team_sync.queued_count(conn) == 2
    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 0
    conn.close()


def test_400_from_something_else_at_the_address_is_not_dead_lettered(
    sync, monkeypatch, invite, capsys
):
    # e.g. a TLS proxy put on the port: "400 Client sent an HTTP
    # request to an HTTPS server" in plain text.
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))

    def proxy(*_args, **_kwargs):
        raise team_sync.server_error(
            400, "Bad Request", b"Client sent an HTTP request to an HTTPS server.\n"
        )

    monkeypatch.setattr(team_sync, "request_json", proxy)
    state = team_sync.SyncState()

    for _ in range(team_sync.REFUSED_BATCH_ATTEMPTS + 2):
        state.next_sync = 0
        assert team_sync.flush(conn, config, state, team_sync.MAX_BATCHES_PER_SYNC)

    assert team_sync.queued_count(conn) == 1
    assert team_sync.dead_letter_count(conn) == 0
    assert state.failures == team_sync.REFUSED_BATCH_ATTEMPTS + 2
    assert "set-server" in capsys.readouterr().out
    conn.close()


@pytest.mark.parametrize("status, reason, body, message, from_api", [
    (400, "BAD REQUEST", b'{"error": "Expected a JSON object."}',
     "Expected a JSON object.", True),
    (400, "Bad Request", b"<html>400</html>", "Bad Request", False),
    (422, "Unprocessable", b'["error"]', "Unprocessable", False),
    (502, "Bad Gateway", None, "Bad Gateway", False),
])
def test_server_error_knows_whether_the_team_api_answered(
    status, reason, body, message, from_api
):
    error = team_sync.server_error(status, reason, body)

    assert error.status == status
    assert str(error) == message
    assert error.from_team_api is from_api


def test_requeue_dead_needs_a_team(sync, capsys):
    assert team_sync.main(["requeue-dead"]) == 1
    assert not team_sync.QUEUE_FILE.exists()


def test_queue_is_kept_when_the_answer_is_not_from_a_team_server(
    sync, monkeypatch, invite
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))

    monkeypatch.setattr(team_sync, "request_json", lambda *a, **k: {"status": "ok"})
    state = team_sync.SyncState()

    assert team_sync.flush(conn, config, state) is True
    assert team_sync.queued_count(conn) == 1
    assert state.failures == 1


# --------------------------------------------------
# set-server and status
# --------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("192.168.1.20:5050", "http://192.168.1.20:5050"),
    ("  http://192.168.1.20:5050/  ", "http://192.168.1.20:5050"),
    ("HTTPS://team.example.org:8443/", "https://team.example.org:8443"),
    ("localhost:5050", "http://localhost:5050"),
    ("http://[::1]:5050", "http://[::1]:5050"),
])
def test_normalise_server(raw, expected):
    assert team_sync.normalise_server(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "ftp://host:21", "http://", "http://host:port",
    "192.168.1..20:5050", "http://a..b:5050", ".host:5050", "x" * 64 + ".com",
])
def test_normalise_server_rejects_non_addresses(raw):
    with pytest.raises(ValueError):
        team_sync.normalise_server(raw)


def test_set_server_with_a_typo_in_the_address_explains_it(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite())
    sync.clear()
    capsys.readouterr()

    assert team_sync.main(["set-server", "192.168.1..20:5050"]) == 1

    assert sync == []
    assert "Not a server address" in capsys.readouterr().out


def test_set_server_verifies_then_saves(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite("Asha"))
    sync.clear()

    assert team_sync.main(["set-server", "192.168.1.21:5050/"]) == 0

    assert sync == [("GET", "/api/team/me")]
    assert team_sync.load_config()["server"] == "http://192.168.1.21:5050"
    assert "Asha" in capsys.readouterr().out


def test_running_collector_follows_set_server(sync, monkeypatch, invite):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    real = team_sync.request_json
    hosts = []

    def old_address_gone(method, url, body=None, token=None, timeout=15):
        hosts.append(urlparse(url).netloc)
        if urlparse(url).netloc == "127.0.0.1:5050":
            raise OSError("no route to host")
        return real(method, url, body, token, timeout)

    samples = []

    def collect():
        samples.append(stamp(len(samples) * 5))
        if len(samples) == 3:
            # What `set-server` writes from another terminal.
            team_sync.save_config({**config, "server": "http://127.0.0.1:5151"})
        if len(samples) > 5:
            raise KeyboardInterrupt
        return observation(samples[-1])

    monkeypatch.setattr(team_sync, "request_json", old_address_gone)
    monkeypatch.setattr(team_sync, "collect_data", collect)
    monkeypatch.setattr(team_sync, "COLLECTION_INTERVAL", 0.01)

    assert team_sync.main(["run", "--quiet"]) == 0

    # One failed try at the old address, then no waiting out the 30 s
    # backoff: the third sample is already uploaded to the new one.
    assert hosts[0] == "127.0.0.1:5050"
    assert hosts[1:] == ["127.0.0.1:5151"] * (len(hosts) - 1)
    assert len(hosts) >= 2

    conn = team_sync.open_queue()
    assert team_sync.queued_count(conn) == 0
    conn.close()


def test_set_server_keeps_old_address_when_unreachable(sync, monkeypatch, invite):
    _join(monkeypatch, invite())
    monkeypatch.setattr(team_sync, "request_json", _offline)

    assert team_sync.main(["set-server", "http://192.168.1.21:5050"]) == 1
    assert team_sync.load_config()["server"] == "http://127.0.0.1:5050"


def test_set_server_keeps_old_address_when_token_unknown(
    sync, monkeypatch, invite, client, admin
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    client.delete(f"/api/team/members/{config['member_id']}", headers=admin)

    assert team_sync.main(["set-server", "http://192.168.1.21:5050"]) == 1
    assert team_sync.load_config()["server"] == "http://127.0.0.1:5050"


def test_set_server_rejects_bad_address(sync, monkeypatch, invite):
    _join(monkeypatch, invite())
    sync.clear()

    assert team_sync.main(["set-server", "ftp://192.168.1.21"]) == 1
    assert sync == []


def test_status_shows_oldest_queued_sample(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite())
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))
    team_sync.enqueue(conn, observation(stamp(5)))
    conn.close()
    capsys.readouterr()

    assert team_sync.main(["status"]) == 0

    out = capsys.readouterr().out
    assert f"Queued locally (not yet uploaded): 2 (oldest from {stamp(0)})" in out


@pytest.mark.parametrize("argv", [["--help"], ["join", "--help"], ["set-server", "--help"]])
def test_help_uses_port_5050(argv, capsys):
    with pytest.raises(SystemExit):
        team_sync.main(argv)

    out = capsys.readouterr().out
    assert ":5000" not in out
    assert ":5000" not in team_sync.__doc__


# --------------------------------------------------
# HTTP
# --------------------------------------------------

class _SlowJSONHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.3)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_request_json_has_short_connect_but_full_read_timeout(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(team_sync, "_opener", None)
    monkeypatch.setattr(team_sync, "CONNECT_TIMEOUT_SECONDS", 0.1)

    connect_timeouts = []
    real_create = socket.create_connection

    def recording_create(address, timeout=None, *args, **kwargs):
        connect_timeouts.append(timeout)
        return real_create(address, timeout, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", recording_create)

    server = HTTPServer(("127.0.0.1", 0), _SlowJSONHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # The answer takes 0.3 s, longer than the 0.1 s connect limit.
        answer = team_sync.request_json(
            "GET", f"http://127.0.0.1:{server.server_port}/api/team/me", timeout=5
        )
    finally:
        server.shutdown()
        server.server_close()

    assert answer == {"ok": True}
    assert connect_timeouts == [0.1]


class _CutOffBody:
    """
    An error body that ends early: Content-Length 200, 13 bytes sent,
    then the connection closed (a proxy or server dying mid-answer).
    """

    def read(self, *_args):
        raise http.client.IncompleteRead(b'{"error": "up', 187)

    def close(self):
        pass


def test_cut_off_error_answer_is_still_a_server_error(monkeypatch):
    def cut_off(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 502, "Bad Gateway", {}, _CutOffBody()
        )

    monkeypatch.setattr(team_sync, "_open", cut_off)

    with pytest.raises(team_sync.ServerError) as caught:
        team_sync.request_json(
            "POST", "http://127.0.0.1:5050/api/team/ingest", {"observations": []}
        )

    assert caught.value.status == 502
    assert str(caught.value) == "Bad Gateway"
    assert caught.value.from_team_api is False


@pytest.mark.parametrize("failure", [
    UnicodeError("encoding with 'idna' codec failed (label empty or too long)"),
    http.client.IncompleteRead(b"{", 10),
])
def test_request_json_raises_only_server_or_os_errors(monkeypatch, failure):
    def broken(req, timeout):
        raise failure

    monkeypatch.setattr(team_sync, "_open", broken)

    with pytest.raises(ConnectionError):
        team_sync.request_json("GET", "http://127.0.0.1:5050/api/team/me")


def test_flush_backs_off_on_a_host_name_that_cannot_be_encoded(sync, monkeypatch):
    # A team.json written by hand (or before addresses were checked).
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(team_sync, "_opener", None)
    monkeypatch.setattr(team_sync, "request_json", REAL_REQUEST_JSON)

    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation())
    state = team_sync.SyncState()

    assert team_sync.flush(
        conn, {"server": "http://192.168.1..20:5050", "token": "t"}, state
    ) is True

    assert state.failures == 1
    assert team_sync.queued_count(conn) == 1
    conn.close()


# --------------------------------------------------
# `leave` while `run` is going in another terminal
# --------------------------------------------------

def _run_until(monkeypatch, on_sample, samples_before_stop=8):
    """
    `run` with fast sampling; on_sample(n) is called while taking
    sample n (what another terminal does meanwhile).
    """

    samples = []

    def collect():
        samples.append(stamp(len(samples) * 5))
        on_sample(len(samples))
        if len(samples) > samples_before_stop:
            raise KeyboardInterrupt
        return observation(samples[-1])

    monkeypatch.setattr(team_sync, "collect_data", collect)
    monkeypatch.setattr(team_sync, "COLLECTION_INTERVAL", 0.01)
    monkeypatch.setattr(team_sync, "TEAM_SYNC_INTERVAL", 0)

    return team_sync.main(["run", "--quiet"]), samples


def test_leave_local_only_stops_a_running_collector(
    sync, monkeypatch, invite, db_file, capsys
):
    _join(monkeypatch, invite())
    # Windows cannot delete the queue while `run` holds it, and here
    # `run` is the same thread: do not wait for it to let go.
    monkeypatch.setattr(team_sync, "QUEUE_RELEASE_SECONDS", 0)

    def other_terminal(sample):
        if sample == 3:
            assert team_sync.main(["leave", "--local-only"]) == 0
            sync.clear()

    code, samples = _run_until(monkeypatch, other_terminal)

    assert code == 0
    assert len(samples) == 3
    assert sync == []                   # nothing uploaded after leaving
    assert "nothing more is shared" in capsys.readouterr().out
    assert not team_sync.CONFIG_FILE.exists()

    if team_sync.QUEUE_FILE.exists():   # Windows: emptied instead
        conn = team_sync.open_queue()
        assert team_sync.queued_count(conn) == 0
        conn.close()


def test_a_new_join_stops_the_collector_of_the_old_membership(
    sync, monkeypatch, invite, capsys
):
    _join(monkeypatch, invite())
    config = team_sync.load_config()

    def other_terminal(sample):
        if sample == 2:
            team_sync.save_config({**config, "token": "new-token", "member_id": 99})
            sync.clear()

    code, samples = _run_until(monkeypatch, other_terminal)

    assert code == 0
    assert len(samples) == 2
    assert sync == []
    assert "replaced by a new `join`" in capsys.readouterr().out


def test_queue_deleted_between_check_and_write_stops_cleanly(
    sync, monkeypatch, invite, capsys
):
    _join(monkeypatch, invite())

    def deleted_under_us(conn, observation):
        team_sync.CONFIG_FILE.unlink()
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(team_sync, "enqueue", deleted_under_us)

    code, _samples = _run_until(monkeypatch, lambda sample: None)

    assert code == 0
    assert "nothing more is shared" in capsys.readouterr().out


def test_other_queue_errors_are_not_hidden(sync, monkeypatch, invite):
    _join(monkeypatch, invite())

    def disk_full(conn, observation):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(team_sync, "enqueue", disk_full)

    with pytest.raises(sqlite3.OperationalError):
        _run_until(monkeypatch, lambda sample: None)


def test_leave_waits_for_a_running_collector_to_release_the_queue(
    sync, monkeypatch, invite
):
    _join(monkeypatch, invite())
    team_sync.open_queue().close()
    real_remove = team_sync._remove_file
    refusals = []

    def in_use_twice(path):
        # What Windows does while `run` still has the file open.
        if path == team_sync.QUEUE_FILE and len(refusals) < 2:
            refusals.append(path)
            raise PermissionError(32, "The process cannot access the file")
        real_remove(path)

    monkeypatch.setattr(team_sync, "_remove_file", in_use_twice)
    monkeypatch.setattr(team_sync, "QUEUE_RELEASE_SECONDS", 5)

    assert team_sync.main(["leave", "--local-only"]) == 0

    assert len(refusals) == 2
    assert not team_sync.CONFIG_FILE.exists()
    assert not team_sync.QUEUE_FILE.exists()


def test_leave_empties_a_queue_that_stays_in_use(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite())
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation(stamp(0)))
    conn.close()
    real_remove = team_sync._remove_file

    def always_in_use(path):
        if path == team_sync.QUEUE_FILE:
            raise PermissionError(32, "The process cannot access the file")
        real_remove(path)

    monkeypatch.setattr(team_sync, "_remove_file", always_in_use)
    monkeypatch.setattr(team_sync, "QUEUE_RELEASE_SECONDS", 0)

    assert team_sync.main(["leave", "--local-only"]) == 0

    # team.json goes first, so the running collector stops sharing.
    assert not team_sync.CONFIG_FILE.exists()
    conn = team_sync.open_queue()
    assert team_sync.queued_count(conn) == 0
    conn.close()
    assert "emptied instead of deleted" in capsys.readouterr().out
