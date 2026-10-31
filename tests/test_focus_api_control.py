"""
Session control through the dashboard API (api/focus.py): start, stop,
extend and allow, the application lists, and the scores added to the
summary and session list. The engine runs on fakes (no real windows,
no notifications).
"""

import subprocess
import sys
import time
from datetime import datetime

import pytest
from flask import Flask

from api import focus as focus_api_module
from api.focus import focus_api
from focus import analytics, engine as engine_module
from focus.engine import FocusEngine
from focus.focus_manager import InstanceLock

from tests.test_focus_engine import (
    CHROME, CODE, LOCK_HOLDER, PROJECT_ROOT, SLACK, Clock, Notifier, Screen,
)
from tests.test_focus_scoring import LIVE_CONTRACT_KEYS


JSON = {"Content-Type": "application/json"}

START = {
    "title": "Write report",
    "duration_minutes": 25,
    "allowed_apps": [CODE],
    "goal": "Finish the draft",
}

SESSION_KEYS = {
    "id", "title", "goal", "start", "end", "status", "planned_min", "actual_s",
    "adherence_pct", "deviations", "switches", "longest_focus_s",
    "top_deviation_app", "score",
}


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


@pytest.fixture
def rig(tmp_path, data_dir):
    """
    (client, engine, screen, notifier): a Flask app with the focus
    blueprint and an engine on fakes ticking every 20 ms.
    """

    screen, notifier = Screen(app=CODE, title="main.py"), Notifier()
    engine = FocusEngine(
        data_dir=data_dir, sampler=screen.sampler, idle_reader=screen.idle_reader,
        notifier=notifier, tick_seconds=0.02,
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["FOCUS_DATA_DIR"] = str(data_dir)
    app.config["RAW_DATA_FILE"] = str(tmp_path / "raw" / "workspace_activity.csv")
    app.register_blueprint(focus_api)
    app.extensions["focus_engine"] = engine

    yield app.test_client(), engine, screen, notifier

    engine.stop()
    engine.join(2)


def _wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)

    return condition()


# ---------------------------------------------------------
# Only JSON POSTs
# ---------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/focus/session",
    "/api/focus/session/stop",
    "/api/focus/session/extend",
    "/api/focus/session/allow",
])
@pytest.mark.parametrize("kwargs", [
    {},                                                          # no body
    {"data": {"title": "x"}},                                    # an HTML form
    {"data": '{"title": "x"}', "content_type": "text/plain"},    # no preflight
    {"data": "title=x", "content_type": "application/x-www-form-urlencoded"},
    {"data": "--b--", "content_type": "multipart/form-data; boundary=b"},
])
def test_posts_need_json(rig, path, kwargs):
    client, engine, *_ = rig

    response = client.post(path, **kwargs)

    assert response.status_code == 415
    assert "error" in response.get_json()
    assert engine.running is False


def test_start_through_a_text_plain_post_cannot_start_a_session(rig, data_dir):
    client, engine, *_ = rig
    response = client.post("/api/focus/session", data='{"title": "x", "duration_minutes": 5, '
                           '"allowed_apps": ["Slack"]}', content_type="text/plain")

    assert response.status_code == 415
    assert not (data_dir / analytics.SESSIONS_FILE_NAME).exists()
    assert "Access-Control-Allow-Origin" not in response.headers


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------

@pytest.mark.parametrize("body", [
    [],
    "text",
    {},
    {**START, "title": ""},
    {**START, "title": 5},
    {**START, "goal": 7},
    {**START, "duration_minutes": 0},
    {**START, "duration_minutes": 481},
    {**START, "duration_minutes": "25"},
    {**START, "duration_minutes": None},
    {**START, "allowed_apps": []},
    {**START, "allowed_apps": "Visual Studio Code"},
    {**START, "allowed_apps": [f"App {n}" for n in range(31)]},
    {**START, "allowed_apps": [""]},
    {**START, "allowed_apps": [None]},
])
def test_start_rejects_invalid_input(rig, body):
    client, engine, *_ = rig

    response = client.post("/api/focus/session", json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]
    assert engine.running is False


def test_malformed_json_is_400(rig):
    client, *_ = rig
    response = client.post("/api/focus/session", data="{nope", headers=JSON)
    assert response.status_code == 400


def test_start_runs_a_session_in_the_background(rig):
    client, engine, screen, notifier = rig
    # Each look at the screen is one (fake) second later, so the 20 ms
    # ticks cover whole seconds of focus time.
    clock = Clock(datetime.now().replace(microsecond=0))
    engine._clock = clock

    def sampler():
        clock.advance(1)
        return screen.sampler()

    engine._sampler = sampler

    response = client.post("/api/focus/session", json=START)
    live = response.get_json()

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is True and live["source"] == "dashboard"
    assert live["title"] == "Write report" and live["goal"] == "Finish the draft"
    assert live["allowed_apps"] == [CODE] and live["allowed"] is True
    assert live["score"] == 100
    assert engine.thread_alive

    # The background thread notices a switch on its own.
    screen.show(SLACK, "general")
    assert _wait_for(lambda: len(notifier.calls) == 1)

    live = client.get("/api/focus/live").get_json()
    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is True and live["stale"] is False
    assert live["allowed"] is False and live["state"] == "deviation"
    assert live["current_app"] == SLACK
    assert live["last_deviation"]["app"] == SLACK
    assert live["last_deviation"]["penalty"] == 5

    # The episode counts once it has lasted a whole second (the moment
    # a recorded session ending then would count it too).
    assert _wait_for(lambda: client.get("/api/focus/live").get_json()["deviations"] == 1)
    live = client.get("/api/focus/live").get_json()
    assert live["score"] <= 95
    assert live["recent_deviations"][0]["app"] == SLACK


def test_start_while_running_is_409(rig):
    client, *_ = rig
    assert client.post("/api/focus/session", json=START).status_code == 201

    response = client.post("/api/focus/session", json=START)

    assert response.status_code == 409
    assert "already running" in response.get_json()["error"]


def test_start_while_the_tk_assistant_is_open_is_409(rig, data_dir):
    client, engine, *_ = rig
    data_dir.mkdir(parents=True)
    # A Focus Assistant in another process holds the data directory's lock.
    code = LOCK_HOLDER.format(root=str(PROJECT_ROOT), data_dir=str(data_dir))
    assistant = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)

    try:
        assert assistant.stdout.readline().strip() == "held"
        response = client.post("/api/focus/session", json=START)
        assert response.status_code == 409
        assert "Focus Assistant is open" in response.get_json()["error"]
        assert engine.running is False
    finally:
        assistant.stdin.close()
        assistant.wait(timeout=30)

    assert client.post("/api/focus/session", json=START).status_code == 201


def test_another_engine_in_this_process_is_409(rig, data_dir):
    client, *_ = rig
    other = InstanceLock(data_dir)
    assert other.acquire() is True

    try:
        response = client.post("/api/focus/session", json=START)
        assert response.status_code == 409
        assert "already running" in response.get_json()["error"]
    finally:
        other.release()


def test_engine_is_created_lazily_once_per_app(tmp_path, monkeypatch):
    screen, notifier = Screen(app=CODE), Notifier()
    monkeypatch.setattr(engine_module, "_default_sampler", screen.sampler)
    monkeypatch.setattr(engine_module, "_default_idle_reader", screen.idle_reader)
    monkeypatch.setattr(engine_module.desktop_notify, "notify", notifier)

    app = Flask(__name__)
    app.config["FOCUS_DATA_DIR"] = str(tmp_path / "focus")
    app.register_blueprint(focus_api)
    client = app.test_client()

    assert client.get("/api/focus/live").get_json()["running"] is False
    assert "focus_engine" not in app.extensions

    assert client.post("/api/focus/session", json=START).status_code == 201
    engine = app.extensions["focus_engine"]

    try:
        assert isinstance(engine, FocusEngine)
        assert engine.data_dir == tmp_path / "focus"
        assert engine.source == "dashboard"
        assert client.post("/api/focus/session", json=START).status_code == 409
        assert app.extensions["focus_engine"] is engine
    finally:
        engine.stop()
        engine.join(2)


# ---------------------------------------------------------
# Stop, extend, allow
# ---------------------------------------------------------

@pytest.mark.parametrize("path, body", [
    ("/api/focus/session/stop", {}),
    ("/api/focus/session/extend", {"minutes": 5}),
    ("/api/focus/session/allow", {"app": SLACK}),
])
def test_controls_without_a_session_are_409(rig, path, body):
    client, *_ = rig
    response = client.post(path, json=body)
    assert response.status_code == 409
    assert response.get_json()["error"]


def test_stop_returns_the_recorded_session(rig):
    client, engine, screen, _ = rig
    client.post("/api/focus/session", json=START)
    sid = engine.session_id

    response = client.post("/api/focus/session/stop", json={})
    body = response.get_json()

    assert response.status_code == 200
    assert body["stopped"] is True
    assert body["session"]["id"] == sid
    assert body["session"]["status"] == "Stopped"
    assert set(body["session"]) == SESSION_KEYS
    assert isinstance(body["session"]["score"], int)
    assert engine.join(2) is True

    # An empty JSON body works too (fetch with no body).
    client.post("/api/focus/session", json=START)
    response = client.post("/api/focus/session/stop", headers=JSON)
    assert response.status_code == 200

    live = client.get("/api/focus/live").get_json()
    assert live["running"] is False
    assert live["status"] == "Session stopped"
    assert client.post("/api/focus/session/stop", json={}).status_code == 409


def test_extend(rig):
    client, *_ = rig
    client.post("/api/focus/session", json=START)

    for minutes in (0, 241, "5", None, True, 2.5):
        response = client.post("/api/focus/session/extend", json={"minutes": minutes})
        assert response.status_code == 400, minutes

    response = client.post("/api/focus/session/extend", json={"minutes": 15})
    live = response.get_json()

    assert response.status_code == 200
    assert live["planned_s"] == 40 * 60
    assert 39 * 60 <= live["remaining_s"] <= 40 * 60


def test_allow(rig):
    client, engine, screen, notifier = rig
    client.post("/api/focus/session", json=START)
    screen.show(CHROME, "docs")
    assert _wait_for(lambda: client.get("/api/focus/live").get_json()["allowed"] is False)

    for app_name in ("", "   ", None, 5, "A | B", "x" * 121):
        response = client.post("/api/focus/session/allow", json={"app": app_name})
        assert response.status_code == 400, app_name

    response = client.post("/api/focus/session/allow", json={"app": CHROME})
    live = response.get_json()

    assert response.status_code == 200
    assert live["allowed_apps"] == [CODE, CHROME]
    assert live["allowed"] is True and live["state"] == "allowed"
    assert len(notifier.calls) == 1


# ---------------------------------------------------------
# Applications, summary, sessions
# ---------------------------------------------------------

def test_apps_lists_running_and_recent(rig, tmp_path, monkeypatch):
    client, *_ = rig
    seen = {}

    def recent(raw_file=None):
        seen["raw_file"] = raw_file
        return [CODE, "Notion"]

    monkeypatch.setattr(focus_api_module.focus_apps, "running_applications",
                        lambda: [CHROME, CODE])
    monkeypatch.setattr(focus_api_module.focus_apps, "recent_applications", recent)

    response = client.get("/api/focus/apps")

    assert response.status_code == 200
    assert response.get_json() == {"running": [CHROME, CODE], "recent": [CODE, "Notion"]}
    assert seen["raw_file"] == str(tmp_path / "raw" / "workspace_activity.csv")


def test_apps_without_any_data(rig, monkeypatch):
    client, *_ = rig
    monkeypatch.setattr(focus_api_module.focus_apps, "running_applications", lambda: [])

    assert client.get("/api/focus/apps").get_json() == {"running": [], "recent": []}


def test_summary_and_sessions_carry_scores(rig):
    client, engine, screen, _ = rig

    summary = client.get("/api/focus/summary").get_json()
    assert summary["focus_score"] is None
    assert {"sessions", "completed", "adherence_pct", "focus_seconds_today",
            "focus_seconds_total", "deviation_count", "deviation_sources"} < set(summary)

    client.post("/api/focus/session", json=START)
    stopped = client.post("/api/focus/session/stop", json={}).get_json()["session"]

    summary = client.get("/api/focus/summary").get_json()
    assert summary["sessions"] == 1
    assert summary["focus_score"] == float(stopped["score"])

    sessions = client.get("/api/focus/sessions").get_json()["sessions"]
    assert set(sessions[0]) == SESSION_KEYS
    assert sessions[0]["score"] == stopped["score"]


def test_live_without_an_engine_session_reads_the_heartbeat(rig, data_dir):
    client, engine, *_ = rig
    # The Tk assistant's heartbeat, with none of the new fields.
    analytics.write_live({
        "running": True, "session_id": "tk", "title": "From Tk", "goal": "G",
        "remaining_s": 300, "current_app": CODE, "allowed": True,
        "status": "Within focus zone",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, data_dir)

    live = client.get("/api/focus/live").get_json()

    assert engine.running is False
    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is True and live["title"] == "From Tk"
    assert live["score"] is None and live["deviations"] == 0
