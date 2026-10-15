"""
Focus Assistant accounting (focus/analytics.py), the session manager
(focus/focus_manager.py) and the read-only /api/focus endpoints.

None of this needs tkinter.
"""

import csv
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask

from api.focus import focus_api
from focus import analytics
from focus.focus_manager import EVENT_FIELDS, SESSION_FIELDS, FocusManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 10, 1, 9, 0, 0)

# Column order of the files the first prototype wrote (title last).
LEGACY_SESSION_FIELDS = [
    "session_id", "event_type", "timestamp", "goal",
    "planned_duration_minutes", "allowed_apps", "completion_status", "title",
]
LEGACY_EVENT_FIELDS = [
    "timestamp", "focus_session_id", "goal", "application", "window_title",
    "allowed", "status", "remaining_seconds", "title",
]

LIVE_KEYS = {
    "running", "stale", "title", "goal", "remaining_s", "current_app",
    "allowed", "status", "updated_at",
}
SUMMARY_KEYS = {
    "sessions", "completed", "adherence_pct", "focus_seconds_today",
    "focus_seconds_total", "deviation_count", "deviation_sources",
}
SESSION_KEYS = {
    "id", "title", "goal", "start", "end", "status", "planned_min", "actual_s",
    "adherence_pct", "deviations", "switches", "longest_focus_s",
    "top_deviation_app",
}


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def stamp(offset_seconds=0, base=T0):
    return (base + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def session_row(sid, event_type, offset, base=T0, status="", planned=25,
                apps="Visual Studio Code", title="Write report"):
    return {
        "session_id": sid,
        "event_type": event_type,
        "timestamp": stamp(offset, base),
        "title": title,
        "goal": "Finish the draft",
        "planned_duration_minutes": planned,
        "allowed_apps": apps,
        "completion_status": status,
    }


def event_row(sid, offset, application, window_title, allowed, status=None, base=T0):
    if status is None:
        status = {True: "Within focus zone", False: "Focus deviation"}.get(allowed, "Neutral")
    return {
        "timestamp": stamp(offset, base),
        "focus_session_id": sid,
        "title": "Write report",
        "goal": "Finish the draft",
        "application": application,
        "window_title": window_title,
        "allowed": "" if allowed is None else str(allowed),
        "status": status,
        "remaining_seconds": 0,
    }


def make_session(data_dir, sid, events, end_offset=None, base=T0, status="Completed", **kwargs):
    """
    Append one session (START, optional END) and its events to data_dir.
    """

    sessions_file = data_dir / analytics.SESSIONS_FILE_NAME
    events_file = data_dir / analytics.EVENTS_FILE_NAME
    sessions = analytics.read_csv(sessions_file)
    old_events = analytics.read_csv(events_file)

    sessions.append(session_row(sid, "START", 0, base, **kwargs))
    if end_offset is not None:
        sessions.append(session_row(sid, "END", end_offset, base, status=status, **kwargs))

    write_csv(sessions_file, SESSION_FIELDS, sessions)
    write_csv(events_file, EVENT_FIELDS, old_events + [
        event_row(sid, offset, app, title, allowed, base=base, **({"status": st} if st else {}))
        for offset, app, title, allowed, st in (
            item if len(item) == 5 else (*item, None) for item in events
        )
    ])


def only_record(data_dir, **kwargs):
    records = analytics.session_records(data_dir, now=T0 + timedelta(hours=6), **kwargs)
    assert len(records) == 1
    return records[0]


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


# ---------------------------------------------------------
# Accounting
# ---------------------------------------------------------

def test_title_changes_inside_one_excursion_are_one_deviation(data_dir):
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (60, "Google Chrome", "Video 1 - YouTube", False),
        (90, "Google Chrome", "Video 2 - YouTube", False),
        (120, "Google Chrome", "Video 3 - YouTube", False),
        (150, "Google Chrome", "Video 4 - YouTube", False),
        (180, "Google Chrome", "Video 5 - YouTube", False),
        (210, "Visual Studio Code", "main.py", True),
    ], end_offset=300)

    record = only_record(data_dir)

    assert record["deviations"] == 1
    assert record["deviation_seconds"] == 150
    assert record["allowed_seconds"] == 150
    assert record["adherence"] == pytest.approx(50.0)
    assert record["switches"] == 2
    assert record["top_deviation_app"] == "Google Chrome"


def test_shell_and_own_windows_are_neutral(data_dir):
    # Logged as deviations by the old prototype; none of them are.
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (100, "File Explorer", "Task Switching", False),
        (102, "Google Chrome", "News", False),
        (130, "File Explorer", "Task Switching", False),
        (131, "Visual Studio Code", "main.py", True),
        (200, "Loginwindow", "Unknown", False),
        (260, "Searchhost", "Search", False),
        (262, "Visual Studio Code", "main.py", True),
        (290, "Python", "Focus Timer", False),
    ], end_offset=300)

    record = only_record(data_dir)

    assert record["deviations"] == 1
    assert record["deviation_apps"] == {"Google Chrome": 28}
    assert record["allowed_seconds"] == 100 + 69 + 28
    assert record["neutral_seconds"] == 2 + 1 + 60 + 2 + 10
    # The Alt+Tab before Chrome does not break the first 100 s run.
    assert record["longest_focus_seconds"] == 100
    assert record["switches"] == 2


def test_idle_time_counts_as_neither_focus_nor_deviation(data_dir):
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (60, "Visual Studio Code", "main.py", None, "Idle"),
        (660, "Visual Studio Code", "main.py", True),
    ], end_offset=720)

    record = only_record(data_dir)

    assert record["allowed_seconds"] == 120
    assert record["idle_seconds"] == 600
    assert record["deviation_seconds"] == 0
    assert record["adherence"] == pytest.approx(100.0)
    # Walking away interrupts the focus run.
    assert record["longest_focus_seconds"] == 60


def test_completion_prompt_time_is_not_counted(data_dir):
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (1500, "Focus Assistant", "Focus time completed", None, "Completion prompt"),
        (1800, "Visual Studio Code", "main.py", True),
    ], end_offset=2100)

    record = only_record(data_dir)

    assert record["allowed_seconds"] == 1800
    assert record["neutral_seconds"] == 300


def test_deviation_time_is_split_per_application(data_dir):
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (10, "Slack", "general", False),
        (210, "Spotify", "Playlist", False),
        (360, "Visual Studio Code", "main.py", True),
    ], end_offset=400)

    records = analytics.session_records(data_dir, now=T0 + timedelta(hours=1))
    summary = analytics.summarize(records, now=T0 + timedelta(hours=1))

    assert summary["deviation_sources"] == [
        {"app": "Slack", "seconds": 200},
        {"app": "Spotify", "seconds": 150},
    ]
    assert summary["deviation_count"] == 1
    assert records[0]["top_deviation_app"] == "Slack"


def test_orphan_session_ends_at_its_last_event(data_dir):
    make_session(data_dir, "s1", [
        (0, "Visual Studio Code", "main.py", True),
        (120, "Google Chrome", "News", False),
        (200, "Visual Studio Code", "main.py", True),
    ])

    record = only_record(data_dir)

    assert record["status"] == analytics.STATUS_INTERRUPTED
    assert record["running"] is False
    assert record["end"] == T0 + timedelta(seconds=200)
    assert record["actual_seconds"] == 200
    assert record["allowed_seconds"] == 120
    assert record["deviation_seconds"] == 80


def test_orphan_session_uses_its_last_heartbeat(data_dir):
    make_session(data_dir, "s1", [(0, "Visual Studio Code", "main.py", True)])
    analytics.write_live({
        "running": True,
        "session_id": "s1",
        "updated_at": (T0 + timedelta(seconds=250)).isoformat(),
    }, data_dir)

    record = only_record(data_dir)

    assert record["status"] == analytics.STATUS_INTERRUPTED
    assert record["actual_seconds"] == 250
    assert record["allowed_seconds"] == 250


def test_running_session_ends_now(data_dir):
    make_session(data_dir, "s1", [(0, "Visual Studio Code", "main.py", True)])
    now = T0 + timedelta(seconds=900)

    record = analytics.session_records(data_dir, now=now, active_session_id="s1")[0]

    assert record["status"] == analytics.STATUS_IN_PROGRESS
    assert record["running"] is True
    assert record["actual_seconds"] == 900
    assert analytics.session_json(record)["end"] is None


def test_chart_uses_the_newest_sessions(data_dir):
    for index in range(10):
        make_session(
            data_dir, f"s{index}",
            [(0, "Visual Studio Code", "main.py", True)],
            end_offset=600, base=T0 + timedelta(hours=index),
        )

    records = analytics.session_records(data_dir, now=T0 + timedelta(days=1))
    recent = analytics.recent_records(records, 8)

    assert [record["id"] for record in recent] == [f"s{index}" for index in range(9, 1, -1)]
    assert records[0]["id"] == "s9"


def test_overall_adherence_is_time_weighted_over_finished_sessions(data_dir):
    make_session(data_dir, "short", [
        (0, "Visual Studio Code", "main.py", True),
    ], end_offset=100)
    make_session(data_dir, "long", [
        (0, "Visual Studio Code", "main.py", True),
        (900, "Slack", "general", False),
    ], end_offset=1000, base=T0 + timedelta(hours=1))
    make_session(data_dir, "running", [
        (0, "Slack", "general", False),
    ], base=T0 + timedelta(hours=2))

    now = T0 + timedelta(hours=2, seconds=500)
    records = analytics.session_records(data_dir, now=now, active_session_id="running")
    summary = analytics.summarize(records, now=now)

    # Mean of per-session percentages would be 95; time-weighted is 1000/1100.
    assert summary["adherence_pct"] == pytest.approx(90.9)
    assert summary["sessions"] == 3
    assert summary["completed"] == 2
    # Focus time is in-zone time only (the running session is all Slack).
    assert summary["focus_seconds_total"] == 100 + 900
    assert summary["focus_seconds_today"] == 100 + 900
    assert summary["session_seconds_total"] == 100 + 1000 + 500
    assert summary["deviation_count"] == 2


def test_focus_today_is_split_at_midnight(data_dir):
    base = datetime(2026, 10, 1, 23, 50, 0)
    make_session(data_dir, "late", [
        (0, "Visual Studio Code", "main.py", True),
    ], end_offset=1200, base=base)

    now = datetime(2026, 10, 2, 9, 0, 0)
    summary = analytics.summarize(analytics.session_records(data_dir, now=now), now=now)

    assert summary["focus_seconds_total"] == 1200
    assert summary["focus_seconds_today"] == 600


# ---------------------------------------------------------
# FocusManager
# ---------------------------------------------------------

def test_extend_session_never_shortens_the_session(data_dir):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 30, ["Visual Studio Code"])

    remaining = manager.extend_session(5)

    assert 35 * 60 - 2 <= remaining <= 35 * 60
    assert manager.duration_minutes == 35

    # Once the planned end has passed, the extension starts from now.
    manager.expected_end_at = datetime.now() - timedelta(minutes=10)
    remaining = manager.extend_session(5)

    assert 5 * 60 - 2 <= remaining <= 5 * 60
    assert manager.duration_minutes == 40

    rows = analytics.read_csv(manager.sessions_file)
    assert [row["event_type"] for row in rows] == ["START", "EXTEND", "EXTEND"]


def test_append_keeps_a_legacy_header_without_rewriting(data_dir):
    sessions_file = data_dir / analytics.SESSIONS_FILE_NAME
    events_file = data_dir / analytics.EVENTS_FILE_NAME
    old = {
        "session_id": "old", "goal": "Study", "planned_duration_minutes": 25,
        "allowed_apps": "Visual Studio Code", "title": "",
    }
    write_csv(sessions_file, LEGACY_SESSION_FIELDS, [
        {**old, "event_type": "START", "timestamp": stamp(0), "completion_status": ""},
        {**old, "event_type": "END", "timestamp": stamp(60), "completion_status": "Stopped early"},
    ])
    write_csv(events_file, LEGACY_EVENT_FIELDS, [{
        "timestamp": stamp(1), "focus_session_id": "old", "goal": "Study",
        "application": "Visual Studio Code", "window_title": "main.py",
        "allowed": "True", "status": "Within focus goal", "remaining_seconds": 1499,
        "title": "",
    }])
    # A malformed row with an extra column, and no newline at the end.
    with open(events_file, "a", newline="", encoding="utf-8") as file:
        file.write(f"{stamp(2)},old,Study,Slack,general,False,Focus deviation,1498,,EXTRA")

    before = {path: (path.stat().st_ino, path.read_bytes()) for path in (sessions_file, events_file)}

    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    manager.evaluate_application("Visual Studio Code", "report.md")
    manager.stop_session("Completed")

    for path, (inode, content) in before.items():
        assert path.stat().st_ino == inode
        assert path.read_bytes().startswith(content)

    with open(sessions_file, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == LEGACY_SESSION_FIELDS
        new_rows = [row for row in reader if row["session_id"] == manager.session_id]
    assert [row["event_type"] for row in new_rows] == ["START", "END"]
    assert new_rows[0]["title"] == "Write report"

    with open(events_file, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == LEGACY_EVENT_FIELDS
        new_events = [row for row in reader if row["focus_session_id"] == manager.session_id]
    assert len(new_events) == 1
    assert new_events[0]["application"] == "Visual Studio Code"
    assert new_events[0]["window_title"] == "report.md"
    assert new_events[0]["title"] == "Write report"

    # Both sessions still read back.
    statuses = {record["id"]: record["status"] for record in analytics.session_records(data_dir)}
    assert statuses == {"old": "Stopped early", manager.session_id: "Completed"}


def test_missing_columns_are_added_once_at_start_up(data_dir):
    events_file = data_dir / analytics.EVENTS_FILE_NAME
    short_header = [field for field in EVENT_FIELDS if field not in ("title", "remaining_seconds")]
    write_csv(events_file, short_header, [event_row("old", 0, "Slack", "general", False)])

    FocusManager(data_dir)

    with open(events_file, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert set(EVENT_FIELDS) <= set(reader.fieldnames)
        assert [row["application"] for row in reader] == ["Slack"]

    inode = events_file.stat().st_ino
    FocusManager(data_dir)
    assert events_file.stat().st_ino == inode


def test_orphaned_start_gets_an_interrupted_end_on_start_up(data_dir):
    make_session(data_dir, "orphan", [
        (0, "Visual Studio Code", "main.py", True),
        (300, "Slack", "general", False),
    ])

    manager = FocusManager(data_dir)

    assert manager.recovered_sessions == ["orphan"]
    ends = [row for row in analytics.read_csv(manager.sessions_file) if row["event_type"] == "END"]
    assert len(ends) == 1
    assert ends[0]["timestamp"] == stamp(300)
    assert ends[0]["completion_status"] == analytics.STATUS_INTERRUPTED
    assert ends[0]["title"] == "Write report"

    # Nothing left to recover the second time.
    assert FocusManager(data_dir).recovered_sessions == []


def test_session_of_another_running_assistant_is_left_alone(data_dir):
    make_session(data_dir, "theirs", [(0, "Visual Studio Code", "main.py", True)])
    analytics.write_live({
        "running": True,
        "session_id": "theirs",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getppid(),  # a process that is certainly alive
    }, data_dir)

    assert FocusManager(data_dir).recovered_sessions == []


def test_own_windows_are_recognised_and_unknown_is_neutral(data_dir):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    me = os.getpid()

    def event_count():
        return len(analytics.read_csv(manager.events_file))

    # Our own process, whatever macOS calls it and whatever the title.
    result = manager.evaluate_application("Python", "Unknown", pid=me)
    assert result["state"] == "self"
    assert result["allowed"] is None
    assert event_count() == 1

    # Nothing can be said about an unknown window: one neutral row (so
    # its time is not credited to the window before it), no deviation.
    result = manager.evaluate_application("Unknown", "Unknown")
    assert result["state"] == "unknown"
    assert result["allowed"] is None and result["logged"] is True
    assert event_count() == 2
    assert manager.evaluate_application("Unknown", "Task Manager")["logged"] is False
    assert event_count() == 2

    # Without a pid, a 'Python' window is ours only by its title.
    assert manager.evaluate_application("Python", "Focus Assistant")["state"] == "self"
    assert manager.evaluate_application("Python", "Add application")["state"] == "self"
    assert manager.evaluate_application("Python", "Unknown")["state"] == "unknown"

    # Another Python program is an ordinary application.
    assert manager.evaluate_application("Python", "matplotlib", pid=me + 1)["state"] == "deviation"
    assert manager.evaluate_application("File Explorer", "Task Switching")["allowed"] is None

    # (These calls share one second, so the timeline gives the rows no
    # duration; check how each logged row is counted instead.)
    states = [
        (row["application"], row["window_title"], analytics.event_state(row))
        for row in analytics.read_csv(manager.events_file)
    ]
    assert [item for item in states if item[2] == analytics.DEVIATION] == [
        ("Python", "matplotlib", analytics.DEVIATION),
    ]
    assert states[-1] == ("File Explorer", "Task Switching", analytics.NEUTRAL)
    assert states[1] == ("Unknown", "Unknown", analytics.NEUTRAL)


def test_idle_is_logged_once_and_not_as_a_deviation(data_dir):
    manager = FocusManager(data_dir, idle_threshold=60)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])

    first = manager.evaluate_application("Slack", "general", idle_seconds=61)
    second = manager.evaluate_application("Slack", "random", idle_seconds=75)

    assert first["state"] == second["state"] == "idle"
    assert first["logged"] is True and second["logged"] is False
    rows = analytics.read_csv(manager.events_file)
    assert [(row["status"], row["allowed"]) for row in rows] == [("Idle", "")]


def test_allow_application_is_a_session_row_not_timeline_time(data_dir):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])

    assert manager.evaluate_application("Slack", "general")["allowed"] is False
    assert manager.allow_application("Slack") is True
    assert manager.allow_application("slack") is False
    result = manager.evaluate_application("Slack", "general")

    assert result["allowed"] is True and result["logged"] is True
    sessions = analytics.read_csv(manager.sessions_file)
    assert [row["event_type"] for row in sessions] == ["START", "ALLOW"]
    assert sessions[-1]["allowed_apps"] == "Visual Studio Code | Slack"
    events = analytics.read_csv(manager.events_file)
    assert [row["allowed"] for row in events] == ["False", "True"]

    record = analytics.session_records(data_dir, active_session_id=manager.session_id)[0]
    assert record["apps"] == ["Visual Studio Code", "Slack"]


def test_completion_marker_and_end_at_the_due_time(data_dir):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    due = manager.started_at
    manager.expected_end_at = due

    manager.mark_due()
    manager.stop_session("Completed", ended_at=due)

    events = analytics.read_csv(manager.events_file)
    assert events[-1]["status"] == "Completion prompt"
    assert events[-1]["allowed"] == ""
    assert events[-1]["timestamp"] == analytics.format_ts(due)
    end = analytics.read_csv(manager.sessions_file)[-1]
    assert end["event_type"] == "END"
    assert end["timestamp"] == analytics.format_ts(due)


def test_write_errors_never_reach_the_monitor(data_dir, monkeypatch):
    manager = FocusManager(data_dir)

    def broken(*_args, **_kwargs):
        raise PermissionError("focus_events.csv is open in another program")

    monkeypatch.setattr(FocusManager, "_append_csv", staticmethod(broken))

    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    result = manager.evaluate_application("Slack", "general")
    manager.stop_session()

    assert result["allowed"] is False
    assert "open in another program" in manager.last_write_error
    assert manager.running is False


def test_heartbeat_round_trip_and_staleness(data_dir):
    assert analytics.live_status(data_dir)["stale"] is True

    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    assert manager.write_heartbeat("Visual Studio Code", True, "Within focus zone")

    live = analytics.live_status(data_dir)
    assert set(live) == LIVE_KEYS
    assert live["running"] is True and live["stale"] is False
    assert live["title"] == "Write report"
    assert live["current_app"] == "Visual Studio Code"
    assert live["allowed"] is True
    assert 24 * 60 <= live["remaining_s"] <= 25 * 60

    later = analytics.live_status(data_dir, now=datetime.now() + timedelta(seconds=6))
    assert later["stale"] is True and later["running"] is False
    assert later["title"] is None

    assert sorted(path.name for path in data_dir.iterdir()) == [
        analytics.SESSIONS_FILE_NAME, analytics.LIVE_FILE_NAME,
    ]


# ---------------------------------------------------------
# /api/focus
# ---------------------------------------------------------

@pytest.fixture
def focus_client(data_dir):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["FOCUS_DATA_DIR"] = str(data_dir)
    app.register_blueprint(focus_api)
    return app.test_client()


def test_focus_api_without_any_data(focus_client):
    live = focus_client.get("/api/focus/live").get_json()
    assert set(live) == LIVE_KEYS
    assert live["stale"] is True and live["running"] is False

    summary = focus_client.get("/api/focus/summary").get_json()
    assert set(summary) == SUMMARY_KEYS
    assert summary["sessions"] == 0 and summary["adherence_pct"] is None

    assert focus_client.get("/api/focus/sessions").get_json() == {"sessions": []}


def test_focus_api_endpoints(focus_client, data_dir):
    now = datetime.now().replace(microsecond=0)
    make_session(data_dir, "old", [
        (0, "Visual Studio Code", "main.py", True),
        (300, "Slack", "general", False),
    ], end_offset=400, base=now - timedelta(days=2))
    make_session(data_dir, "running", [
        (0, "Visual Studio Code", "main.py", True),
    ], base=now - timedelta(minutes=5), title="Live session")
    analytics.write_live({
        "running": True,
        "session_id": "running",
        "title": "Live session",
        "goal": "Finish the draft",
        "remaining_s": 1200,
        "current_app": "Visual Studio Code",
        "allowed": True,
        "status": "Within focus zone",
        "updated_at": now.isoformat(timespec="seconds"),
    }, data_dir)

    response = focus_client.get("/api/focus/live")
    live = response.get_json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(live) == LIVE_KEYS
    assert live["running"] is True and live["stale"] is False
    assert live["title"] == "Live session" and live["remaining_s"] == 1200

    summary = focus_client.get("/api/focus/summary").get_json()
    assert set(summary) == SUMMARY_KEYS
    assert summary["sessions"] == 2
    assert summary["completed"] == 1
    assert summary["adherence_pct"] == pytest.approx(75.0)
    assert summary["deviation_count"] == 1
    assert summary["deviation_sources"] == [{"app": "Slack", "seconds": 100}]
    # The running session started 5 minutes ago (less of it is "today"
    # just after midnight).
    midnight = now.replace(hour=0, minute=0, second=0)
    today_part = (now - max(now - timedelta(minutes=5), midnight)).total_seconds()
    assert today_part - 5 <= summary["focus_seconds_today"] <= today_part + 30

    sessions = focus_client.get("/api/focus/sessions").get_json()["sessions"]
    assert [session["id"] for session in sessions] == ["running", "old"]
    assert all(set(session) == SESSION_KEYS for session in sessions)
    assert sessions[0]["status"] == "In progress" and sessions[0]["end"] is None
    assert sessions[1] == {
        "id": "old",
        "title": "Write report",
        "goal": "Finish the draft",
        "start": (now - timedelta(days=2)).isoformat(timespec="seconds"),
        "end": (now - timedelta(days=2) + timedelta(seconds=400)).isoformat(timespec="seconds"),
        "status": "Completed",
        "planned_min": 25,
        "actual_s": 400,
        "adherence_pct": 75.0,
        "deviations": 1,
        "switches": 1,
        "longest_focus_s": 300,
        "top_deviation_app": "Slack",
    }


# ---------------------------------------------------------
# No tkinter needed
# ---------------------------------------------------------

def _run_without_tk(code):
    # sys.modules["tkinter"] = None makes any `import tkinter` fail.
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['tkinter'] = None\n" + code],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
    )


def test_focus_modules_import_without_tkinter():
    result = _run_without_tk("import focus.analytics, focus.focus_manager, api.focus")
    assert result.returncode == 0, result.stderr


def test_launcher_explains_how_to_install_tk():
    script = PROJECT_ROOT / "run_focus_assistant.py"
    result = _run_without_tk(
        "import runpy\n"
        f"sys.argv = [{str(script)!r}]\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert f"brew install python-tk@{version}" in result.stderr
    assert "python3-tk" in result.stderr
