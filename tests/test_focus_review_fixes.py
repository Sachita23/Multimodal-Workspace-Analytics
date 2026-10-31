"""
Regression tests for the focus engine / API review: idle never erases an
announced deviation, the switch penalty shows at once, failed CSV writes
keep live and recorded equal, exact application matching, the DNS
rebinding guard, crashed heartbeats and sampling outside the engine lock.

Everything runs on fakes: no real window is read and no notification
is shown.
"""

import os
import stat
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from flask import Flask

from api.focus import focus_api
from focus import analytics
from focus.engine import FocusEngine
from focus.focus_manager import FocusManager, canonical_app_name

from tests.test_focus_engine import (
    CHROME, CODE, SLACK, Clock, Notifier, Screen, recorded, run,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


@pytest.fixture
def rig(data_dir):
    clock, screen, notifier = Clock(), Screen(), Notifier()
    engine = FocusEngine(
        data_dir=data_dir, sampler=screen.sampler, idle_reader=screen.idle_reader,
        notifier=notifier, clock=clock, tick_seconds=0.02,
    )
    yield engine, clock, screen, notifier
    engine.stop()


def _finish(engine, clock):
    clock.now -= timedelta(seconds=1)
    return engine.stop()


# ---------------------------------------------------------
# Idle after a switch keeps the deviation
# ---------------------------------------------------------

def test_idle_after_clicking_into_a_deviation_keeps_the_episode(rig, data_dir):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 60, [CODE])
    run(engine, clock, screen, [(20, CODE)])

    # The click into YouTube is the last input; then the user just watches.
    payloads = run(engine, clock, screen, [
        (1, CHROME, "YouTube", float(second)) for second in range(20 * 60)
    ])

    assert len(notifier.calls) == 1
    assert payloads[59]["deviations"] == 1
    live = payloads[-1]
    assert live["state"] == "idle"
    assert live["deviations"] == 1
    assert live["score"] < 100
    sid = engine.session_id

    clock.now -= timedelta(seconds=1)
    assert recorded(data_dir, sid, clock.now)["score"] == live["score"]
    session = engine.stop()
    assert session["deviations"] == 1 and session["score"] == live["score"]


def test_the_switch_penalty_shows_on_the_tick_that_sees_it(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 60, [CODE])
    run(engine, clock, screen, [(5, CODE)])

    live = run(engine, clock, screen, [(1, SLACK)])[-1]

    assert len(notifier.calls) == 1
    assert live["last_deviation"]["penalty"] == 5
    assert live["deviations"] == 1 and live["score"] == 95


def test_a_session_stopped_in_the_second_of_a_switch_counts_it(rig, data_dir):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 60, [CODE])
    run(engine, clock, screen, [(5, CODE)])
    screen.show(SLACK)
    live = engine.tick()

    session = engine.stop()

    assert live["deviations"] == 1
    assert session["deviations"] == 1 and session["score"] == live["score"] == 95


# ---------------------------------------------------------
# A CSV that cannot be written
# ---------------------------------------------------------

def _read_only(path):
    path.chmod(stat.S_IRUSR)


def _writable(path):
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="needs POSIX permissions and a non-root user")
def test_failed_event_writes_are_queued_alerted_and_recorded(rig, data_dir):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 60, [CODE])
    run(engine, clock, screen, [(10, CODE)])
    events = data_dir / analytics.EVENTS_FILE_NAME
    sid = engine.session_id

    _read_only(events)
    try:
        payloads = run(engine, clock, screen, [(30, SLACK)])
        # The alert still fires, and the status says the file is stuck.
        assert len(notifier.calls) == 1
        assert "Could not save" in payloads[-1]["status"]
        assert len(engine._manager.event_rows) == 2
    finally:
        _writable(events)

    live = run(engine, clock, screen, [(30, CODE)])[-1]
    assert live["deviations"] == 1 and live["deviation_seconds"] == 30
    assert engine._manager.pending_events == []

    rows = [row for row in analytics.read_csv(events) if row["focus_session_id"] == sid]
    assert [row["application"] for row in rows] == [CODE, SLACK, CODE]

    clock.now -= timedelta(seconds=1)
    assert recorded(data_dir, sid, clock.now)["score"] == live["score"]
    session = engine.stop()
    assert session["score"] == live["score"] and session["deviations"] == 1


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="needs POSIX permissions and a non-root user")
def test_a_session_row_that_was_not_saved_is_not_remembered(rig, data_dir):
    engine, clock, screen, _ = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    sessions = data_dir / analytics.SESSIONS_FILE_NAME
    before = list(engine._manager.session_rows)

    _read_only(sessions)
    try:
        live = engine.extend(5)
    finally:
        _writable(sessions)

    assert engine._manager.session_rows == before
    # Live and the files agree on the planned time (the EXTEND was lost).
    assert live["planned_s"] == 30 * 60
    assert recorded(data_dir, engine.session_id, clock.now)["planned"] == 25


# ---------------------------------------------------------
# Application matching
# ---------------------------------------------------------

@pytest.mark.parametrize("allowed, front, expected", [
    ("Code", "Xcode", False),
    ("Notes", "Sticky Notes", False),
    ("Mail", "Gmail", False),
    ("Messages", "Message", False),
    ("a", "Safari", False),
    ("Visual Studio Code", "Visual Studio Code Insiders", False),
    ("Code", "Visual Studio Code", True),
    ("VS Code", "Visual Studio Code", True),
    ("Visual Studio Code", "code.exe", True),
    ("Chrome", "Google Chrome", True),
    ("Word", "Microsoft Word", True),
    ("slack", "Slack", True),
    ("Slack", "slack.exe", True),
    ("  Google   Chrome ", "Google Chrome", True),
])
def test_applications_match_by_name_not_by_part_of_it(tmp_path, allowed, front, expected):
    manager = FocusManager(tmp_path / "focus")
    manager.start_session("Write", "Draft", 25, [allowed])

    assert manager._matches_allowed(front) is expected
    assert (manager.classify(front, "window", pid=1) == "allowed") is expected


def test_canonical_names():
    assert canonical_app_name("CODE.EXE") == "visual studio code"
    assert canonical_app_name("") == ""
    assert canonical_app_name(None) == ""


# ---------------------------------------------------------
# DNS rebinding
# ---------------------------------------------------------

JSON = {"Content-Type": "application/json"}
START = {"title": "Write", "duration_minutes": 25, "allowed_apps": [CODE]}


@pytest.fixture
def client(tmp_path, data_dir):
    screen = Screen(app=CODE, title="main.py")
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=Notifier(),
                         tick_seconds=0.02)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["FOCUS_DATA_DIR"] = str(data_dir)
    app.config["TEAM_ADMIN_KEY"] = "k" * 32
    app.register_blueprint(focus_api)
    app.extensions["focus_engine"] = engine
    yield app.test_client(), engine
    engine.stop()
    engine.join(2)


@pytest.mark.parametrize("path, body", [
    ("/api/focus/session", START),
    ("/api/focus/session/stop", {}),
    ("/api/focus/session/extend", {"minutes": 5}),
    ("/api/focus/session/allow", {"app": SLACK}),
])
def test_a_rebound_host_name_cannot_control_sessions(client, path, body):
    client, engine = client
    if path != "/api/focus/session":
        engine.start("Write", 25, [CODE])

    response = client.post(path, json=body, headers={
        "Host": "attacker.example:5050", "Origin": "http://attacker.example:5050",
    })

    assert response.status_code == 403
    assert "error" in response.get_json()
    assert engine.running is (path != "/api/focus/session")
    assert engine.live()["allowed_apps"] in ([CODE], [])


def test_a_rebound_host_name_cannot_read_focus_data(client):
    client, _ = client

    response = client.get("/api/focus/sessions", headers={"Host": "attacker.example"})

    assert response.status_code == 403


def test_a_foreign_origin_cannot_post_even_to_localhost(client):
    client, engine = client

    for origin in ("http://attacker.example", "null"):
        response = client.post("/api/focus/session", json=START,
                               headers={"Host": "127.0.0.1:5050", "Origin": origin})
        assert response.status_code == 403

    assert engine.running is False


@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:5050", "Origin": "http://localhost:5173"},    # Vite proxy
    {"Host": "localhost:5050", "Origin": "http://127.0.0.1:5173"},
    {"Host": "[::1]:5050"},
    {"Host": "192.168.1.20:5050", "Origin": "http://192.168.1.20:5173"},
    {"Host": "attacker.example", "X-Admin-Key": "k" * 32},
])
def test_the_dashboard_can_still_control_sessions(client, headers):
    client, engine = client

    response = client.post("/api/focus/session", json=START, headers=headers)

    assert response.status_code == 201, response.get_json()
    assert engine.running is True


def test_a_configured_public_host_is_trusted(client, monkeypatch):
    client, engine = client
    monkeypatch.setenv("WORKSPACE_PUBLIC_URL", "https://focus.example.com")

    response = client.post("/api/focus/session", json=START, headers={
        "Host": "focus.example.com", "Origin": "https://focus.example.com",
    })

    assert response.status_code == 201


# ---------------------------------------------------------
# A heartbeat whose process died
# ---------------------------------------------------------

def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_a_dead_process_heartbeat_is_not_running(data_dir):
    now = analytics.datetime.now().replace(microsecond=0)
    heartbeat = {
        "running": True, "session_id": "s", "title": "Write",
        "updated_at": now.isoformat(), "pid": _dead_pid(),
    }
    analytics.write_live(heartbeat, data_dir)

    live = analytics.live_status(data_dir, now=now)
    assert live["running"] is False and live["session_id"] is None
    assert live["stale"] is False
    assert analytics.running_session_id(heartbeat, now) is None

    heartbeat["pid"] = os.getpid()
    assert analytics.running_session_id(heartbeat, now) == "s"
    heartbeat["pid"] = None
    assert analytics.running_session_id(heartbeat, now) == "s"


# ---------------------------------------------------------
# Slow window readers do not block the engine
# ---------------------------------------------------------

def test_live_and_stop_do_not_wait_for_a_slow_sampler(data_dir):
    release = threading.Event()
    slow = threading.Event()

    def sampler():
        if slow.is_set():
            release.wait(5)
        return CODE, "main.py", 1

    engine = FocusEngine(data_dir=data_dir, sampler=sampler, idle_reader=lambda: 0.0,
                         notifier=Notifier())
    engine.start("Write", 25, [CODE])
    slow.set()
    ticker = threading.Thread(target=engine.tick)
    ticker.start()

    try:
        time.sleep(0.05)
        began = time.monotonic()
        assert engine.live()["running"] is True
        session = engine.stop()
        assert time.monotonic() - began < 1.0
        assert session is not None
    finally:
        release.set()
        ticker.join(5)

    # The tick that was sampling finds the session gone and does nothing.
    assert engine.running is False
