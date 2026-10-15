"""
Regression tests for the Focus Assistant review fixes:

- one assistant per data directory (a second one never closes the
  first one's running session)
- damaged CSV files (non-UTF-8 bytes, unterminated or oversized quoted
  fields) never hide the whole history, and are never rewritten
- time asleep and the first minute of a break are idle
- unknown windows, the macOS desktop, idle breaks and zero-length rows
- focus time is in-zone time
- run_focus_assistant.py --help
- the GUI monitor tick, driven through a minimal tkinter stub
"""

import csv
import importlib
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import focus.focus_manager as focus_manager
from focus import analytics
from focus.focus_manager import (
    EVENT_FIELDS,
    SESSION_FIELDS,
    FocusManager,
    InstanceLock,
    assistant_running_elsewhere,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 10, 1, 9, 0, 0)

ZONE = ["Visual Studio Code"]


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def stamp(offset_seconds=0, base=T0):
    return (base + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def event(sid, offset, application, window_title, allowed, status=None):
    if status is None:
        status = {True: "Within focus zone", False: "Focus deviation"}.get(allowed, "Neutral")
    return {
        "timestamp": stamp(offset),
        "focus_session_id": sid,
        "application": application,
        "window_title": window_title,
        "allowed": "" if allowed is None else str(allowed),
        "status": status,
    }


def record_for(events, end_offset=None, now=None):
    sessions = [{"session_id": "s", "event_type": "START", "timestamp": stamp(0),
                 "title": "t", "planned_duration_minutes": "60"}]
    if end_offset is not None:
        sessions.append({"session_id": "s", "event_type": "END", "timestamp": stamp(end_offset),
                         "completion_status": "Completed"})
    return analytics.build_records(sessions, events, now=now or T0 + timedelta(hours=5))[0]


def csv_bytes(fieldnames, rows):
    lines = [",".join(fieldnames)] + [",".join(row) for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


@pytest.fixture
def clock(monkeypatch):
    """A fake wall clock for FocusManager (focus/focus_manager.py)."""

    fake = Clock()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.now

    monkeypatch.setattr(focus_manager, "datetime", FakeDateTime)
    return fake


def ticks(manager, clock, seconds, application="Visual Studio Code", title="main.py",
          idle=lambda _second: 0.0, pid=1):
    """One monitor tick per second for `seconds` seconds."""

    for second in range(seconds):
        manager.evaluate_application(application, title, pid=pid, idle_seconds=idle(second))
        clock.advance(1)


def finished_record(manager, clock):
    manager.stop_session("Stopped")
    return analytics.session_records(manager.data_dir, now=clock.now)[0]


# ---------------------------------------------------------
# One assistant per data directory
# ---------------------------------------------------------

HOLDER = """
import sys
sys.path.insert(0, {root!r})
from focus.focus_manager import FocusManager, InstanceLock
lock = InstanceLock({data_dir!r})
assert lock.acquire() and lock.held
manager = FocusManager({data_dir!r})
manager.start_session("A session", "goal", 25, ["Visual Studio Code"])
manager.evaluate_application("Visual Studio Code", "main.py", pid=1)
manager.write_heartbeat("Visual Studio Code", True, "Within focus zone")
print(manager.session_id, flush=True)
sys.stdin.readline()
manager.stop_session("Completed")
lock.release()
"""


def start_holder(data_dir):
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(root=str(PROJECT_ROOT), data_dir=str(data_dir))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    sid = child.stdout.readline().strip()
    assert sid, "the holder process did not start"
    return child, sid


def test_running_session_survives_a_second_assistant(data_dir):
    child, sid = start_holder(data_dir)

    try:
        second = InstanceLock(data_dir)
        assert second.acquire() is False
        assert second.holder_pid() == child.pid
        assert assistant_running_elsewhere(data_dir) is True

        # Even a manager that skipped the lock, writing an idle heartbeat
        # over A's, and a third start-up after it leave A's session open.
        bystander = FocusManager(data_dir)
        assert bystander.recovered_sessions == []
        bystander.write_heartbeat(None, None, "No active session")
        assert FocusManager(data_dir).recovered_sessions == []
        assert child.poll() is None

        child.stdin.write("stop\n")
        child.stdin.flush()
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    ends = [row["completion_status"]
            for row in analytics.read_csv(data_dir / analytics.SESSIONS_FILE_NAME)
            if row["event_type"] == "END"]
    assert ends == ["Completed"]
    assert [record["status"] for record in analytics.session_records(data_dir)] == ["Completed"]


def test_a_sessions_own_end_overrides_a_wrong_interrupted_end():
    # Files written before the lock: a second assistant's recovery closed
    # a running session, which later ended normally.
    sessions = [
        {"session_id": "s", "event_type": "START", "timestamp": stamp(0), "title": "t"},
        {"session_id": "s", "event_type": "END", "timestamp": stamp(60),
         "completion_status": "Interrupted"},
        {"session_id": "s", "event_type": "END", "timestamp": stamp(1500),
         "completion_status": "Completed"},
        {"session_id": "s", "event_type": "END", "timestamp": stamp(1600),
         "completion_status": "Interrupted"},
    ]
    events = [event("s", 0, "Visual Studio Code", "main.py", True)]

    record = analytics.build_records(sessions, events, now=T0 + timedelta(hours=1))[0]

    assert record["status"] == "Completed"
    assert record["actual_seconds"] == 1500


def test_lock_is_free_again_when_the_holder_dies(data_dir):
    child, sid = start_holder(data_dir)
    child.kill()
    child.wait()

    assert assistant_running_elsewhere(data_dir) is False
    lock = InstanceLock(data_dir)
    try:
        assert lock.acquire() is True
        assert lock.holder_pid() == os.getpid()
        # This process holds it now: its own recovery still runs.
        assert assistant_running_elsewhere(data_dir) is False
        assert FocusManager(data_dir).recovered_sessions == [sid]
    finally:
        lock.release()


def test_no_lock_file_is_created_without_an_assistant(data_dir):
    assert assistant_running_elsewhere(data_dir) is False
    FocusManager(data_dir)
    assert not (data_dir / focus_manager.LOCK_FILE_NAME).exists()


# ---------------------------------------------------------
# Damaged CSV files
# ---------------------------------------------------------

EVENT_HEADER = ["timestamp", "focus_session_id", "application", "window_title", "allowed", "status"]


def good_event(second, application="Visual Studio Code", title="main.py", allowed="True"):
    return [stamp(second), "s1", application, title, allowed, "Within focus zone"]


def test_one_non_utf8_byte_does_not_hide_the_file(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    data = csv_bytes(EVENT_HEADER, [good_event(second) for second in range(200)])
    # A title saved as Windows-1252 (e.g. by Excel): 0xE9 is 'é'.
    data += f"{stamp(300)},s1,Notes,caf".encode() + b"\xe9" + b" menu,False,Focus deviation\r\n"
    path.write_bytes(data)

    rows = analytics.read_csv(path)

    assert len(rows) == 201
    assert rows[-1]["window_title"] == "café menu"
    assert rows[-1]["application"] == "Notes"

    with pytest.raises(ValueError):
        analytics.read_csv(path, strict=True)


def test_undecodable_byte_in_sessions_keeps_every_session(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.SESSIONS_FILE_NAME
    rows = [["s1", "START", stamp(0), "Report", "Write", "25", "Visual Studio Code", ""],
            ["s1", "END", stamp(60), "Report", "Write", "25", "Visual Studio Code", "Completed"]]
    # 0x8F has no Windows-1252 character either: it is replaced, not fatal.
    path.write_bytes(csv_bytes(SESSION_FIELDS, rows)
                     + b"s2,START," + stamp(120).encode() + b",T\x8f,Write,25,Slack,\r\n")

    records = analytics.session_records(data_dir, now=T0 + timedelta(hours=1))

    assert sorted(record["id"] for record in records) == ["s1", "s2"]
    assert next(r for r in records if r["id"] == "s2")["title"] == "T�"


def test_unterminated_quote_loses_only_its_own_row(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    data = csv_bytes(EVENT_HEADER, [good_event(0)])
    data += f'{stamp(10)},s1,Slack,"general, unfinished,False,Focus deviation\r\n'.encode()
    data += csv_bytes(EVENT_HEADER, [good_event(20), good_event(30)]).split(b"\r\n", 1)[1]
    path.write_bytes(data)

    rows = analytics.read_csv(path)

    assert [row["timestamp"] for row in rows] == [stamp(0), stamp(20), stamp(30)]
    with pytest.raises(ValueError, match="line 3"):
        analytics.read_csv(path, strict=True)


def test_oversized_field_loses_only_its_own_row(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    huge = "x" * (csv.field_size_limit() + 10)
    data = csv_bytes(EVENT_HEADER, [good_event(0)])
    data += f'{stamp(10)},s1,Slack,"{huge}",False,Focus deviation\r\n'.encode()
    data += csv_bytes(EVENT_HEADER, [good_event(20)]).split(b"\r\n", 1)[1]
    path.write_bytes(data)

    assert [row["timestamp"] for row in analytics.read_csv(path)] == [stamp(0), stamp(20)]


def test_multiline_titles_and_extra_columns_still_read(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    data = csv_bytes(EVENT_HEADER, [good_event(0)])
    data += f'{stamp(10)},s1,Slack,"two\r\nlines",False,Focus deviation\r\n'.encode()
    data += f"{stamp(20)},s1,Slack,general,False,Focus deviation,EXTRA\r\n".encode()
    path.write_bytes(b"\xef\xbb\xbf" + data)

    rows = analytics.read_csv(path)

    assert list(rows[0])[:2] == ["timestamp", "focus_session_id"]
    assert rows[1]["window_title"] == "two\r\nlines"
    assert rows[2][None] == ["EXTRA"]
    assert analytics.read_csv(path, strict=True) == rows


def test_appending_works_after_a_non_utf8_byte_near_the_start(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    data = csv_bytes(EVENT_FIELDS, [])
    data += f"{stamp(0)},old,T,G,Notes,caf".encode() + b"\xe9" + b",False,Focus deviation,0\r\n"
    path.write_bytes(data)

    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ZONE)
    result = manager.evaluate_application("Visual Studio Code", "main.py", pid=1)

    assert result["logged"] is True
    assert manager.last_write_error is None
    assert path.read_bytes().startswith(data)
    assert [row["application"] for row in analytics.read_csv(path)] == ["Notes", "Visual Studio Code"]


def test_a_cut_off_quoted_field_does_not_swallow_later_rows(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    # The last row stops inside its quoted last field (crash mid-write).
    path.write_bytes(csv_bytes(EVENT_FIELDS, []) + f'{stamp(0)},old,T,G,Slack,x,False,Dev,"12'.encode())

    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ZONE)
    manager.evaluate_application("Visual Studio Code", "main.py", pid=1)
    manager.evaluate_application("Slack", "general", pid=1)

    rows = analytics.read_csv(path, strict=True)
    assert [row["application"] for row in rows] == ["Slack", "Visual Studio Code", "Slack"]
    assert rows[0]["remaining_seconds"] == "12"


def legacy_events(count, bad_row=None):
    """A prototype events file without the title and remaining_seconds columns."""

    header = "timestamp,focus_session_id,goal,application,window_title,allowed,status"
    lines = [header.encode()]
    for index in range(count):
        title = b"main.py"
        if index == bad_row:
            title = "Spotify – Song".encode("cp1252")
        lines.append(
            f"2026-09-18 10:{index // 60:02d}:{index % 60:02d},s1,Study,Visual Studio Code,".encode()
            + title + b",True,Within focus goal"
        )
    return b"\r\n".join(lines) + b"\r\n"


@pytest.mark.parametrize("bad_row", [5, 400])
def test_migration_never_rewrites_a_file_it_cannot_read_completely(data_dir, bad_row):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    original = legacy_events(500, bad_row=bad_row)
    path.write_bytes(original)

    manager = FocusManager(data_dir)

    assert path.read_bytes() == original
    assert "left unchanged" in manager.last_write_error
    assert not list(data_dir.glob("*.tmp"))
    rows = analytics.read_csv(path)
    assert len(rows) == 500
    assert rows[bad_row]["window_title"] == "Spotify – Song"

    # Appends still work, in the file's own columns.
    manager.start_session("Write report", "Finish the draft", 25, ZONE)
    manager.evaluate_application("Visual Studio Code", "report.md", pid=1)
    assert path.read_bytes().startswith(original)
    assert analytics.read_csv(path)[-1]["window_title"] == "report.md"


def test_migration_still_runs_on_a_clean_legacy_file(data_dir):
    data_dir.mkdir(parents=True)
    path = data_dir / analytics.EVENTS_FILE_NAME
    path.write_bytes(legacy_events(500))

    manager = FocusManager(data_dir)

    assert manager.last_write_error is None
    with open(path, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert set(EVENT_FIELDS) <= set(reader.fieldnames)
        assert len(list(reader)) == 500


# ---------------------------------------------------------
# Sleep and idle
# ---------------------------------------------------------

def test_time_asleep_is_idle_not_focus(data_dir, clock):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 240, ZONE)

    ticks(manager, clock, 600)
    clock.advance(2 * 3600 - 1)        # lid closed: no ticks at all
    ticks(manager, clock, 60)          # input on wake resets the idle time

    record = finished_record(manager, clock)
    assert record["idle_seconds"] == 2 * 3600 - 1 + 1
    assert record["allowed_seconds"] == 600 - 1 + 60
    rows = analytics.read_csv(manager.events_file)
    assert [row["status"] for row in rows] == ["Within focus zone", "Idle", "Within focus zone"]
    assert rows[1]["timestamp"] == stamp(599)


def test_time_asleep_on_a_distraction_is_not_deviation(data_dir, clock):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 240, ZONE)

    ticks(manager, clock, 60, application="YouTube", title="Video")
    clock.advance(3 * 3600)
    ticks(manager, clock, 30, application="YouTube", title="Video")

    record = finished_record(manager, clock)
    assert record["deviation_seconds"] == 59 + 30
    assert record["idle_seconds"] == 3 * 3600 + 1
    # The long break ends the excursion: two separate deviations.
    assert record["deviations"] == 2


def test_short_tick_delays_are_not_gaps(data_dir, clock):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 240, ZONE)

    ticks(manager, clock, 10)
    clock.advance(focus_manager.MAX_TICK_GAP_SECONDS - 1)
    ticks(manager, clock, 10)

    assert finished_record(manager, clock)["idle_seconds"] == 0


def test_idle_starts_when_input_stopped(data_dir, clock):
    manager = FocusManager(data_dir, idle_threshold=60)
    manager.start_session("Write report", "Finish the draft", 240, ZONE)

    ticks(manager, clock, 300)                                   # 0..299 working
    ticks(manager, clock, 600, idle=lambda second: second + 1)  # input stopped at 299
    ticks(manager, clock, 60)                                    # back at 900

    record = finished_record(manager, clock)
    rows = analytics.read_csv(manager.events_file)
    assert [(row["status"], row["timestamp"]) for row in rows] == [
        ("Within focus zone", stamp(0)),
        ("Idle", stamp(299)),
        ("Within focus zone", stamp(900)),
    ]
    assert record["allowed_seconds"] == 299 + 60
    assert record["idle_seconds"] == 601


def test_idle_is_never_dated_before_the_last_row(data_dir, clock):
    manager = FocusManager(data_dir, idle_threshold=60)
    manager.start_session("Write report", "Finish the draft", 240, ZONE)

    ticks(manager, clock, 100)
    # A window came to the front by itself at 100, with no input since 80.
    ticks(manager, clock, 100, application="Slack", title="general",
          idle=lambda second: second + 20)

    record = finished_record(manager, clock)
    rows = analytics.read_csv(manager.events_file)
    assert [(row["status"], row["timestamp"]) for row in rows] == [
        ("Within focus zone", stamp(0)),
        ("Focus deviation", stamp(100)),
        ("Idle", stamp(100)),
    ]
    assert record["deviation_seconds"] == 0
    assert record["deviations"] == 0


def test_the_completion_prompt_is_not_a_gap(data_dir, clock):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 1, ZONE)

    ticks(manager, clock, 60)
    manager.mark_due()
    clock.advance(120)                 # the prompt is answered two minutes later
    manager.extend_session(5)
    ticks(manager, clock, 30)

    rows = analytics.read_csv(manager.events_file)
    assert [row["status"] for row in rows] == ["Within focus zone", "Completion prompt", "Within focus zone"]
    record = finished_record(manager, clock)
    assert record["idle_seconds"] == 0
    assert record["neutral_seconds"] == 120


# ---------------------------------------------------------
# Window classification and episodes
# ---------------------------------------------------------

def test_unknown_window_time_is_not_credited_to_the_previous_window(data_dir, clock):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 60, ZONE)

    ticks(manager, clock, 10)
    ticks(manager, clock, 590, application="Unknown", title="Task Manager")

    record = finished_record(manager, clock)
    assert record["allowed_seconds"] == 10
    assert record["neutral_seconds"] == 590
    assert record["deviations"] == 0
    statuses = [row["status"] for row in analytics.read_csv(manager.events_file)]
    assert statuses == ["Within focus zone", "Active window unknown"]


@pytest.mark.parametrize("application, title, expected", [
    ("Finder", "", "neutral"),
    ("Finder", "Unknown", "neutral"),
    ("Finder", "Documents", "deviation"),
    ("File Explorer", "Task View", "neutral"),
    ("File Explorer", "Program Manager", "neutral"),
    ("File Explorer", "Downloads", "deviation"),
])
def test_the_desktop_is_neutral_on_macos_and_windows(data_dir, application, title, expected):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ZONE)

    assert manager.classify(application, title, pid=321) == expected
    row = {"application": application, "window_title": title, "allowed": "False",
           "status": "Focus deviation"}
    assert analytics.event_state(row) == (
        analytics.NEUTRAL if expected == "neutral" else analytics.DEVIATION
    )


def test_a_long_break_splits_deviations_and_a_short_one_does_not():
    long_break = record_for([
        event("s", 0, "Visual Studio Code", "main.py", True),
        event("s", 60, "YouTube", "v", False),
        event("s", 120, "YouTube", "v", None, "Idle"),
        event("s", 3720, "Reddit", "r", False),
        event("s", 3780, "Visual Studio Code", "main.py", True),
    ], end_offset=3900)
    short_break = record_for([
        event("s", 0, "Visual Studio Code", "main.py", True),
        event("s", 60, "YouTube", "v", False),
        event("s", 120, "YouTube", "v", None, "Idle"),
        event("s", 240, "YouTube", "w", False),
        event("s", 300, "Visual Studio Code", "main.py", True),
    ], end_offset=400)

    assert long_break["deviations"] == 2
    assert long_break["deviation_apps"] == {"YouTube": 60, "Reddit": 60}
    assert short_break["deviations"] == 1


def test_last_row_of_an_interrupted_session_is_not_an_episode():
    record = record_for([
        event("s", 0, "Visual Studio Code", "main.py", True),
        event("s", 300, "Slack", "general", False),
    ])

    assert record["status"] == analytics.STATUS_INTERRUPTED
    assert record["deviations"] == 0
    assert record["deviation_seconds"] == 0
    assert record["switches"] == 0


# ---------------------------------------------------------
# Focus time is in-zone time
# ---------------------------------------------------------

def test_focus_time_counts_only_in_zone_time():
    sessions = [
        {"session_id": "late", "event_type": "START", "timestamp": "2026-10-01 23:50:00", "title": "t"},
        {"session_id": "late", "event_type": "END", "timestamp": "2026-10-02 00:20:00",
         "completion_status": "Completed"},
    ]
    events = [
        {"timestamp": "2026-10-01 23:50:00", "focus_session_id": "late", "application": "Code",
         "allowed": "True", "status": "Within focus zone"},
        {"timestamp": "2026-10-02 00:05:00", "focus_session_id": "late", "application": "YouTube",
         "allowed": "False", "status": "Focus deviation"},
        {"timestamp": "2026-10-02 00:10:00", "focus_session_id": "late", "application": "Python",
         "window_title": "Focus Assistant", "allowed": "", "status": "Focus Assistant"},
    ]
    now = datetime(2026, 10, 2, 9, 0, 0)
    records = analytics.build_records(sessions, events, now=now)

    summary = analytics.summarize(records, now=now)
    today = analytics.today_summary(records, now=now)

    assert summary["focus_seconds_total"] == 15 * 60
    assert summary["focus_seconds_today"] == 5 * 60
    assert summary["session_seconds_total"] == 30 * 60
    assert summary["session_seconds_today"] == 20 * 60
    # The GUI's Today tab uses the same midnight rule as the API.
    assert today["focus_seconds"] == summary["focus_seconds_today"]
    assert today["sessions"] == 1


# ---------------------------------------------------------
# run_focus_assistant.py
# ---------------------------------------------------------

def test_help_prints_usage_and_never_starts_the_assistant():
    for blocked in ("", "sys.modules['tkinter'] = None; "):
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys, runpy; {blocked}"
             f"sys.argv = ['run_focus_assistant.py', '--help']; "
             f"runpy.run_path({str(PROJECT_ROOT / 'run_focus_assistant.py')!r}, run_name='__main__')"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("usage: run_focus_assistant.py")
        assert "Open the Focus Assistant" in " ".join(result.stdout.split())
        assert result.stderr == ""


def test_help_in_process_does_not_import_the_gui(monkeypatch, capsys):
    import run_focus_assistant

    gui = types.ModuleType("focus.focus_gui")

    def launch(*_args, **_kwargs):
        raise AssertionError("--help launched the assistant")

    gui.launch_focus_assistant = launch
    monkeypatch.setitem(sys.modules, "focus.focus_gui", gui)

    with pytest.raises(SystemExit) as exit_info:
        run_focus_assistant.main(["--help"])

    assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Only one assistant runs per data directory." in out


def test_launcher_returns_the_assistants_exit_code(monkeypatch):
    import run_focus_assistant

    gui = types.ModuleType("focus.focus_gui")
    gui.launch_focus_assistant = lambda: 1
    monkeypatch.setitem(sys.modules, "focus.focus_gui", gui)
    monkeypatch.setitem(sys.modules, "tkinter", types.ModuleType("tkinter"))

    assert run_focus_assistant.main([]) == 1


# ---------------------------------------------------------
# GUI monitor tick (a minimal tkinter stub; the real Tk is not needed)
# ---------------------------------------------------------

def make_tkinter_stub():
    tk = types.ModuleType("tkinter")

    class TclError(Exception):
        pass

    class Variable:
        def __init__(self, master=None, value=None):
            self._value = value

        def get(self):
            return self._value

        def set(self, value):
            self._value = value

    class Widget:
        def __init__(self, master=None, *_args, **kwargs):
            self.master = master
            self.children = []
            self.options = dict(kwargs)
            self.bindings = {}
            self.destroyed = False
            self.withdrawn = False
            if isinstance(master, Widget):
                master.children.append(self)
            tk.widgets.append(self)

        def configure(self, **kwargs):
            if self.destroyed:
                raise TclError("invalid command name")
            self.options.update(kwargs)

        config = configure
        place_configure = configure

        def bind(self, sequence, func=None, add=None):
            self.bindings[sequence] = func

        bind_all = bind

        def winfo_children(self):
            return [child for child in self.children if not child.destroyed]

        def winfo_exists(self):
            return 0 if self.destroyed else 1

        def winfo_width(self):
            return 600

        winfo_height = winfo_screenwidth = winfo_screenheight = winfo_width

        def destroy(self):
            for child in list(self.children):
                child.destroy()
            self.destroyed = True

        def after(self, _ms, func=None, *args):
            if self.destroyed:
                raise TclError("application has been destroyed")
            tk.after_queue.append((func, args))
            return f"after#{len(tk.after_queue)}"

        def withdraw(self):
            self.withdrawn = True

        def deiconify(self):
            self.withdrawn = False

        def bbox(self, *_args):
            return (0, 0, 1, 1)

        def __getattr__(self, name):
            # pack, grid, geometry, title, create_text, ...: no-ops.
            if name.startswith("__"):
                raise AttributeError(name)
            return lambda *args, **kwargs: None

    tk.TclError = TclError
    tk.widgets = []
    tk.after_queue = []
    tk.StringVar = tk.BooleanVar = tk.IntVar = Variable
    for name in ("Tk", "Toplevel", "Frame", "Label", "Button", "Entry", "Spinbox",
                 "Checkbutton", "Canvas", "Scrollbar"):
        setattr(tk, name, type(name, (Widget,), {}))

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.answers = {"askyesno": False}
    messagebox.calls = []

    messagebox.on_ask = None

    def recorder(kind):
        def call(*args, **_kwargs):
            messagebox.calls.append((kind, args))
            if kind.startswith("ask") and messagebox.on_ask:
                messagebox.on_ask()
            return messagebox.answers.get(kind, "ok")
        return call

    for kind in ("showerror", "showwarning", "showinfo", "askyesno"):
        setattr(messagebox, kind, recorder(kind))

    simpledialog = types.ModuleType("tkinter.simpledialog")
    simpledialog.answers = {"askinteger": None}
    simpledialog.askinteger = lambda *args, **kwargs: simpledialog.answers["askinteger"]

    tk.messagebox = messagebox
    tk.simpledialog = simpledialog
    return tk


@pytest.fixture
def gui(monkeypatch):
    """focus.focus_gui imported against the tkinter stub."""

    tk = make_tkinter_stub()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk.messagebox)
    monkeypatch.setitem(sys.modules, "tkinter.simpledialog", tk.simpledialog)
    monkeypatch.delitem(sys.modules, "focus.focus_gui", raising=False)

    module = importlib.import_module("focus.focus_gui")
    module.tk_stub = tk
    window = {"active": ("Visual Studio Code", "main.py", 4242), "idle": 0.0}
    monkeypatch.setattr(module, "get_active_process", lambda: window["active"])
    monkeypatch.setattr(module, "get_idle_time", lambda: window["idle"])
    module.window = window

    yield module

    sys.modules.pop("focus.focus_gui", None)
    import focus
    if getattr(focus, "focus_gui", None) is module:
        delattr(focus, "focus_gui")


def start_gui_session(gui, data_dir, minutes="25"):
    app = gui.FocusAssistant(data_dir)
    app.title_var.set("Write report")
    app.goal_var.set("Finish the draft")
    app.duration_var.set(minutes)
    app.selected_apps["Visual Studio Code"].set(True)
    app.start_focus()
    assert app.manager.running
    return app


def heartbeat(data_dir):
    return json.loads((data_dir / analytics.LIVE_FILE_NAME).read_text(encoding="utf-8"))


def statuses(app):
    return [row["status"] for row in analytics.read_csv(app.manager.events_file)]


def test_gui_tick_deviation_snooze_allow_idle_and_self(gui, data_dir):
    app = start_gui_session(gui, data_dir)
    assert heartbeat(data_dir)["running"] is True
    assert heartbeat(data_dir)["allowed"] is True

    gui.window["active"] = ("Slack", "general", 4243)
    app._monitor_loop()
    assert app.deviation_popup.withdrawn is False
    assert heartbeat(data_dir)["allowed"] is False
    assert heartbeat(data_dir)["current_app"] == "Slack"

    app._snooze_deviation()
    assert app.deviation_popup.withdrawn is True
    app._monitor_loop()
    assert app.deviation_popup.withdrawn is True
    assert "snoozed" in app.current_status_var.get()

    app._allow_application("Slack")
    app._monitor_loop()
    assert heartbeat(data_dir)["allowed"] is True
    sessions = analytics.read_csv(app.manager.sessions_file)
    assert [row["event_type"] for row in sessions] == ["START", "ALLOW"]

    gui.window["idle"] = 120.0
    app._monitor_loop()
    assert heartbeat(data_dir)["status"] == "Paused (idle)"
    assert app.deviation_popup.withdrawn is True

    gui.window["idle"] = 0.0
    gui.window["active"] = ("Python", "Unknown", os.getpid())
    app._monitor_loop()
    assert heartbeat(data_dir)["status"] == "Focus Assistant"

    assert statuses(app) == [
        "Within focus zone", "Focus deviation", "Within focus zone", "Idle", "Focus Assistant",
    ]
    # Every tick re-arms the loop.
    assert sum(func == app._monitor_loop for func, _ in gui.tk_stub.after_queue) >= 6

    # Every tab builds with the data so far (the analytics tab uses the
    # summary's session_seconds_total), and its charts draw.
    for tab in ("history", "analytics", "dashboard", "focus"):
        app._show_tab(tab)
        for widget in gui.tk_stub.widgets:
            draw = widget.bindings.get("<Configure>")
            if type(widget).__name__ == "Canvas" and draw and not widget.destroyed:
                draw(types.SimpleNamespace(width=600, height=300))


def test_gui_tick_failure_is_logged_once_and_heartbeat_still_written(gui, data_dir, caplog):
    app = start_gui_session(gui, data_dir)
    live = data_dir / analytics.LIVE_FILE_NAME

    def broken(*_args, **_kwargs):
        raise RuntimeError("window query exploded")

    app.manager.evaluate_application = broken
    for _ in range(3):
        live.unlink()
        app._monitor_loop()
        assert live.exists()

    failures = [record for record in caplog.records if "monitor tick failed" in record.getMessage()]
    assert len(failures) == 1


def run_to_the_planned_end(app, clock, seconds=60):
    for _ in range(seconds):
        clock.advance(1)
        app._monitor_loop()


def test_gui_completion_no_ends_at_the_planned_time(gui, data_dir, clock):
    app = start_gui_session(gui, data_dir, minutes="1")
    messagebox = gui.tk_stub.messagebox
    messagebox.answers["askyesno"] = False
    # The prompt is answered five minutes after it opened.
    messagebox.on_ask = lambda: clock.advance(300)

    run_to_the_planned_end(app, clock)

    assert app.manager.running is False
    assert app.completion_prompt_open is False
    end = analytics.read_csv(app.manager.sessions_file)[-1]
    assert (end["event_type"], end["completion_status"]) == ("END", "Completed")
    assert end["timestamp"] == stamp(60)
    assert statuses(app) == ["Within focus zone", "Completion prompt"]
    assert heartbeat(data_dir)["running"] is False
    record = analytics.session_records(data_dir, now=clock.now)[0]
    assert (record["actual_seconds"], record["allowed_seconds"]) == (60, 60)


def test_gui_completion_yes_extends_the_session(gui, data_dir, clock):
    app = start_gui_session(gui, data_dir, minutes="1")
    gui.tk_stub.messagebox.answers["askyesno"] = True
    gui.tk_stub.messagebox.on_ask = lambda: clock.advance(120)
    gui.tk_stub.simpledialog.answers["askinteger"] = 5

    run_to_the_planned_end(app, clock)

    assert app.manager.running is True
    assert app.completion_prompt_open is False
    assert app.manager.remaining_seconds() == 5 * 60
    events = [row["event_type"] for row in analytics.read_csv(app.manager.sessions_file)]
    assert events == ["START", "EXTEND"]
    rows = analytics.read_csv(app.manager.events_file)
    assert [(row["status"], row["timestamp"]) for row in rows] == [
        ("Within focus zone", stamp(0)),
        ("Completion prompt", stamp(60)),
        ("Within focus zone", stamp(180)),
    ]


def test_gui_sleep_through_the_planned_end_is_idle(gui, data_dir, clock):
    app = start_gui_session(gui, data_dir, minutes="1")
    for _ in range(10):
        clock.advance(1)
        app._monitor_loop()
    clock.advance(2 * 3600)            # asleep past the planned end
    gui.tk_stub.messagebox.answers["askyesno"] = False

    app._monitor_loop()

    rows = analytics.read_csv(app.manager.events_file)
    assert [(row["status"], row["timestamp"]) for row in rows] == [
        ("Within focus zone", stamp(0)),
        ("Idle", stamp(10)),
        ("Completion prompt", stamp(60)),
    ]
    record = analytics.session_records(data_dir, now=clock.now)[0]
    assert record["status"] == "Completed"
    assert (record["allowed_seconds"], record["idle_seconds"]) == (10, 50)


def test_second_launch_says_already_running_and_opens_no_window(gui, data_dir, capsys):
    lock = InstanceLock(data_dir)
    assert lock.acquire()
    try:
        built = []
        gui.FocusAssistant = lambda *args, **kwargs: built.append(args)

        assert gui.launch_focus_assistant(data_dir) == 1
    finally:
        lock.release()

    assert built == []
    assert f"already running (PID {os.getpid()})" in capsys.readouterr().err
    assert gui.tk_stub.messagebox.calls[-1][0] == "showinfo"


def test_launch_holds_the_lock_while_the_window_is_open(gui, data_dir):
    seen = []

    class Window:
        def __init__(self, directory):
            seen.append(directory)

        def mainloop(self):
            seen.append(assistant_running_elsewhere(data_dir))
            probe = InstanceLock(data_dir)
            seen.append(probe.acquire())
            probe.release()

    gui.FocusAssistant = Window

    assert gui.launch_focus_assistant(data_dir) == 0
    # Inside: this process holds it (so its own recovery runs), and a
    # second lock on the same directory is refused.
    assert seen == [data_dir, False, False]
    with InstanceLock(data_dir) as after:
        assert after.acquire() is True
