"""
The headless focus engine (focus/engine.py), desktop notifications
(focus/notify.py) and the application lists (focus/apps.py).

Everything runs on fake samplers, idle readers, notifiers and clocks:
no real window is read and no notification is shown.
"""

import csv
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from focus import analytics, apps, notify, scoring
from focus import engine as engine_module
from focus.engine import FocusBusy, FocusEngine, FocusNotRunning
from focus.focus_manager import InstanceLock, assistant_running_elsewhere

from tests.test_focus_scoring import LIVE_CONTRACT_KEYS


PROJECT_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 10, 1, 9, 0, 0)

CODE = "Visual Studio Code"
CHROME = "Google Chrome"
SLACK = "Slack"
TERMINAL = "Terminal"


# ---------------------------------------------------------
# Fakes
# ---------------------------------------------------------

class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Screen:
    """
    What the fake sampler and idle reader report.
    """

    def __init__(self, app=TERMINAL, title="zsh", idle=0.0, pid=4242):
        self.app, self.title, self.idle, self.pid = app, title, idle, pid
        self.samples = 0

    def show(self, app, title="window", idle=0.0):
        self.app, self.title, self.idle = app, title, idle

    def sampler(self):
        self.samples += 1
        return self.app, self.title, self.pid

    def idle_reader(self):
        return self.idle


class Notifier:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


@pytest.fixture
def rig(data_dir):
    """
    (engine, clock, screen, notifier) on fakes; the session is stopped
    afterwards if a test left it running.
    """

    clock, screen, notifier = Clock(), Screen(), Notifier()
    engine = FocusEngine(
        data_dir=data_dir, sampler=screen.sampler, idle_reader=screen.idle_reader,
        notifier=notifier, clock=clock, tick_seconds=0.02,
    )
    yield engine, clock, screen, notifier
    engine.stop()


def run(engine, clock, screen, steps):
    """
    Each step (seconds, app[, title[, idle]]): show it and tick once a
    second for that many seconds. Returns the live payloads.
    """

    payloads = []

    for seconds, *window in steps:
        screen.show(*window)

        for _ in range(seconds):
            payloads.append(engine.tick())
            clock.advance(1)

    return payloads


def recorded(data_dir, session_id, now):
    """
    The session as the dashboard reads it from the files at `now`.
    """

    for record in analytics.session_records(
        data_dir, now=now.replace(microsecond=0), active_session_id=session_id
    ):
        if record["id"] == session_id:
            return record

    raise AssertionError(f"session {session_id} not recorded")


# ---------------------------------------------------------
# Start, input checks, busy
# ---------------------------------------------------------

def test_start_returns_a_full_live_payload(rig):
    engine, clock, screen, _ = rig
    screen.show(CODE, "main.py")

    live = engine.start("Write report", 25, [CODE, "Notion"], goal="Finish the draft")

    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is True and live["stale"] is False
    assert live["source"] == "dashboard"
    assert live["title"] == "Write report" and live["goal"] == "Finish the draft"
    assert live["started_at"] == T0.isoformat(timespec="seconds")
    assert live["planned_s"] == live["remaining_s"] == 25 * 60
    assert live["allowed_apps"] == [CODE, "Notion"]
    assert live["current_app"] == CODE and live["current_window"] == "main.py"
    assert live["allowed"] is True and live["state"] == "allowed"
    assert live["score"] == 100 and live["deviations"] == 0
    assert live["last_deviation"] is None and live["recent_deviations"] == []
    assert live["pid"] == os.getpid()
    assert engine.session_id == live["session_id"]


def test_goal_defaults_to_title(rig):
    engine, *_ = rig
    assert engine.start("Write report", 25, [CODE])["goal"] == "Write report"


@pytest.mark.parametrize("title, minutes, allowed", [
    ("", 25, [CODE]),
    ("   ", 25, [CODE]),
    (None, 25, [CODE]),
    ("x" * 201, 25, [CODE]),
    ("Write", 0, [CODE]),
    ("Write", 481, [CODE]),
    ("Write", True, [CODE]),
    ("Write", "25", [CODE]),
    ("Write", 2.5, [CODE]),
    ("Write", 25, []),
    ("Write", 25, [""]),
    ("Write", 25, ["  "]),
    ("Write", 25, CODE),
    ("Write", 25, [3]),
    ("Write", 25, ["Code | Slack"]),
    ("Write", 25, [f"App {n}" for n in range(31)]),
])
def test_start_rejects_invalid_input(rig, title, minutes, allowed):
    engine, *_ = rig

    with pytest.raises(ValueError):
        engine.start(title, minutes, allowed)

    assert engine.running is False
    # Nothing was held or written for a refused start.
    assert InstanceLock(engine.data_dir).acquire() is True


def test_duplicate_apps_are_merged(rig):
    engine, *_ = rig
    live = engine.start("Write", 25, [CODE, "visual studio code", " Slack "])
    assert live["allowed_apps"] == [CODE, SLACK]


def test_second_start_while_running_is_busy(rig):
    engine, *_ = rig
    engine.start("Write", 25, [CODE])

    with pytest.raises(FocusBusy):
        engine.start("Other", 25, [CODE])

    assert isinstance(FocusBusy("x"), RuntimeError)


def test_second_engine_on_the_same_data_is_busy(rig, data_dir):
    engine, *_ = rig
    engine.start("Write", 25, [CODE])

    other = FocusEngine(data_dir=data_dir, sampler=lambda: (CODE, "x", None),
                        idle_reader=lambda: 0, notifier=Notifier())

    with pytest.raises(FocusBusy):
        other.start("Other", 25, [CODE])


def test_controls_without_a_session(rig):
    engine, *_ = rig

    assert engine.stop() is None

    with pytest.raises(FocusNotRunning):
        engine.extend(5)

    with pytest.raises(FocusNotRunning):
        engine.allow(SLACK)

    live = engine.tick()
    assert live["running"] is False and set(live) == LIVE_CONTRACT_KEYS


# ---------------------------------------------------------
# The Tk assistant and the engine exclude each other
# ---------------------------------------------------------

LOCK_PROBE = """
import sys
sys.path.insert(0, {root!r})
from focus.focus_manager import InstanceLock, assistant_running_elsewhere
lock = InstanceLock({data_dir!r})
print(assistant_running_elsewhere({data_dir!r}), lock.acquire(), lock.holder_pid())
"""

LOCK_HOLDER = """
import sys
sys.path.insert(0, {root!r})
from focus.focus_manager import InstanceLock
lock = InstanceLock({data_dir!r})
print("held" if lock.acquire() else "busy", flush=True)
sys.stdin.read()
"""


def _probe(data_dir):
    code = LOCK_PROBE.format(root=str(PROJECT_ROOT), data_dir=str(data_dir))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_running_engine_blocks_a_tk_assistant_in_another_process(rig, data_dir):
    engine, *_ = rig
    engine.start("Write", 25, [CODE])

    # What launch_focus_assistant() checks before opening its window.
    elsewhere, acquired, holder = _probe(data_dir)
    assert (elsewhere, acquired, holder) == ("True", "False", str(os.getpid()))

    engine.stop()

    elsewhere, acquired, _ = _probe(data_dir)
    assert (elsewhere, acquired) == ("False", "True")


def test_open_tk_assistant_makes_the_engine_busy(rig, data_dir):
    engine, *_ = rig
    data_dir.mkdir(parents=True)
    code = LOCK_HOLDER.format(root=str(PROJECT_ROOT), data_dir=str(data_dir))
    holder = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)

    try:
        assert holder.stdout.readline().strip() == "held"
        assert assistant_running_elsewhere(data_dir) is True

        with pytest.raises(FocusBusy, match="Focus Assistant is open"):
            engine.start("Write", 25, [CODE])

        assert engine.running is False
        # The refused start wrote nothing.
        assert not (data_dir / analytics.SESSIONS_FILE_NAME).exists()
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)

    assert engine.start("Write", 25, [CODE])["running"] is True


def test_lock_is_released_when_the_session_ends(rig, data_dir):
    engine, clock, *_ = rig
    engine.start("Write", 1, [CODE])
    clock.advance(61)
    engine.tick()

    assert engine.running is False
    lock = InstanceLock(data_dir)
    assert lock.acquire() is True
    lock.release()


# ---------------------------------------------------------
# Deviations and notifications
# ---------------------------------------------------------

def test_a_new_deviation_notifies_exactly_once(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE, "main.py")
    engine.start("Write report", 25, [CODE])
    run(engine, clock, screen, [(5, CODE, "main.py")])
    assert notifier.calls == []

    payloads = run(engine, clock, screen, [
        (3, CHROME, "YouTube - video 1"),
        (3, CHROME, "YouTube - video 2"),    # another tab: same excursion
        (3, SLACK, "general"),               # another app: same excursion
        (2, "Finder", ""),                   # the desktop (neutral)
        (3, CHROME, "YouTube - video 3"),    # still the same excursion
    ])

    assert len(notifier.calls) == 1
    title, message = notifier.calls[0]
    assert title == "Focus deviation"
    assert message == (
        f"You switched to {CHROME}. -{scoring.SWITCH_PENALTY} points. Return to {CODE}."
    )

    first = payloads[0]
    assert first["allowed"] is False and first["state"] == "deviation"
    assert first["last_deviation"] == {
        "app": CHROME, "window_title": "YouTube - video 1",
        "at": (T0 + timedelta(seconds=5)).isoformat(timespec="seconds"),
        "penalty": scoring.SWITCH_PENALTY,
    }
    assert all(p["last_deviation"] == first["last_deviation"] for p in payloads)

    # Back to work, then out again: a second episode, a second alert.
    payloads = run(engine, clock, screen, [(4, CODE, "main.py"), (2, SLACK, "random")])
    assert len(notifier.calls) == 2
    assert SLACK in notifier.calls[1][1]

    live = payloads[-1]
    assert live["deviations"] == 2
    assert live["last_deviation"]["app"] == SLACK
    assert [d["app"] for d in live["recent_deviations"]] == [SLACK, CHROME]
    # Chrome's excursion: 14 s (the 2 s on the desktop are not counted).
    assert live["recent_deviations"][1]["seconds"] == 12
    assert live["recent_deviations"][1]["at"] == (
        T0 + timedelta(seconds=5)
    ).isoformat(timespec="seconds")


def test_score_drops_with_each_episode_and_each_minute_off_task(rig, data_dir):
    engine, clock, screen, _ = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])

    payloads = run(engine, clock, screen, [(10, CODE), (121, CHROME), (10, CODE)])
    live = payloads[-1]

    assert live["deviations"] == 1
    assert live["deviation_seconds"] == 121
    assert live["score"] == scoring.session_score(1, 121) == 100 - 5 - 12
    # The score only goes down while off task.
    scores = [p["score"] for p in payloads]
    assert scores == sorted(scores, reverse=True)


def test_idle_and_sleep_do_not_count_against_the_score(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 60, [CODE])

    run(engine, clock, screen, [(5, CODE), (5, CHROME)])
    assert len(notifier.calls) == 1

    # Away from the keyboard on Chrome: no input for 70 s (the idle
    # threshold is 60 s; idle time counts from the last input).
    payloads = run(engine, clock, screen, [
        (1, CHROME, "YouTube", float(seconds)) for seconds in range(70)
    ])
    assert payloads[-1]["state"] == "idle"
    assert payloads[-1]["status"] == engine_module.STATUS_PAUSED
    assert payloads[-1]["allowed"] is None
    # A short break does not start a new episode.
    run(engine, clock, screen, [(2, CHROME, "YouTube")])
    assert len(notifier.calls) == 1

    # The laptop sleeps for 10 minutes: a gap, logged as idle, which is
    # long enough to end the excursion.
    clock.advance(600)
    payloads = run(engine, clock, screen, [(2, CHROME, "YouTube")])
    assert len(notifier.calls) == 2
    assert payloads[-1]["deviations"] == 2
    # Off task: 5 s until the last input, 1 s before the sleep (from the
    # last tick before it) and 1 s since waking up (until this tick). The
    # idle and asleep time is free.
    assert payloads[-1]["deviation_seconds"] == 7


def test_start_grace_for_the_application_the_session_starts_from(rig):
    engine, clock, screen, notifier = rig
    # Started from the dashboard in the browser.
    screen.show(CHROME, "Workspace Analytics")
    live = engine.start("Write", 25, [CODE])

    assert live["state"] == "neutral"
    assert live["status"] == engine_module.STATUS_GRACE
    assert live["allowed"] is None

    payloads = run(engine, clock, screen, [(engine_module.START_GRACE_SECONDS - 1, CHROME)])
    assert notifier.calls == [] and payloads[-1]["score"] == 100

    # Staying on it past the grace period is a deviation.
    payloads = run(engine, clock, screen, [(2, CHROME)])
    assert len(notifier.calls) == 1
    assert payloads[-1]["allowed"] is False


def test_other_apps_during_the_grace_period_are_deviations(rig):
    engine, clock, screen, notifier = rig
    screen.show(TERMINAL)
    engine.start("Write", 25, [CODE])

    payloads = run(engine, clock, screen, [(1, TERMINAL), (1, SLACK)])

    assert len(notifier.calls) == 1 and SLACK in notifier.calls[0][1]
    assert payloads[-1]["state"] == "deviation"


def test_allowed_app_at_start_is_simply_allowed(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    payloads = run(engine, clock, screen, [(3, CODE)])
    assert {p["state"] for p in payloads} == {"allowed"} and notifier.calls == []


def test_unknown_window_and_failing_readers(rig):
    engine, clock, screen, notifier = rig

    def broken():
        raise OSError("no window server")

    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    engine._sampler = broken
    engine._idle_reader = broken

    live = engine.tick()
    assert live["running"] is True
    assert live["state"] == "unknown" and live["current_app"] is None
    assert live["status"] == "Active window unknown"
    assert notifier.calls == []


def test_a_failing_notifier_never_stops_monitoring(rig):
    engine, clock, screen, _ = rig

    def broken(_title, _message):
        raise RuntimeError("no notification daemon")

    engine._notifier = broken
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    payloads = run(engine, clock, screen, [(2, SLACK), (2, SLACK)])

    assert payloads[-1]["running"] is True and payloads[-1]["deviations"] == 1


# ---------------------------------------------------------
# Live score == recorded score
# ---------------------------------------------------------

SCRIPTS = {
    "steady": [(30, CODE), (40, CHROME), (20, CODE), (15, SLACK, "dm"), (25, CODE)],
    "tabs_and_desktop": [
        (12, CODE), (6, CHROME, "a"), (6, CHROME, "b"), (4, "Finder", ""),
        (8, SLACK), (10, CODE), (3, CHROME, "c"), (7, "Unknown", "Unknown"),
    ],
    "idle_in_the_middle": [
        (10, CODE), (10, CHROME),
        *[(1, CHROME, "x", float(idle)) for idle in range(90)],
        (10, CHROME), (5, CODE),
    ],
    "starts_off_task": [(20, TERMINAL), (5, CHROME), (20, CODE)],
}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
@pytest.mark.parametrize("fraction", [0, 0.4, 0.999])
def test_live_score_equals_recorded_score(data_dir, name, fraction):
    clock, screen, notifier = Clock(T0 + timedelta(seconds=fraction)), Screen(), Notifier()
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=notifier, clock=clock)
    screen.show(*SCRIPTS[name][0][1:])
    engine.start("Write", 60, [CODE])
    sid = engine.session_id

    for live in run(engine, clock, screen, SCRIPTS[name]):
        pass

    # Stop at the time of the last tick: the recorded session ends there.
    clock.now -= timedelta(seconds=1)
    session = engine.stop()
    record = recorded(data_dir, sid, clock.now)

    assert session["score"] == record["score"] == live["score"]
    assert record["deviations"] == live["deviations"] == len(notifier.calls)
    assert record["deviation_seconds"] == live["deviation_seconds"]
    assert session["deviations"] == live["deviations"]


@pytest.mark.parametrize("seed", range(12))
def test_live_score_matches_the_files_at_every_tick(tmp_path, seed):
    """
    Random timelines (apps, tabs, idle, sleep gaps, allow, extend): after
    every tick the live payload says what the dashboard would record if
    the session ended right then.
    """

    rng = random.Random(seed)
    data_dir = tmp_path / f"focus-{seed}"
    clock = Clock(T0 + timedelta(seconds=rng.random()))
    screen, notifier = Screen(), Notifier()
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=notifier, clock=clock)
    windows = [
        (CODE, "main.py"), (CODE, "test.py"), (CHROME, "docs"), (CHROME, "YouTube"),
        (SLACK, "general"), ("Finder", ""), ("Unknown", "Unknown"), ("Dock", ""),
        (TERMINAL, "zsh"),
    ]
    screen.show(*rng.choice(windows))
    engine.start("Random", 30, [CODE, "Notion"])
    sid = engine.session_id
    live = None

    for _ in range(150):
        roll = rng.random()

        if roll < 0.15:
            screen.show(*rng.choice(windows))
        elif roll < 0.18:
            screen.idle = rng.choice([0.0, 30.0, 75.0, 400.0])
        elif roll < 0.20:
            clock.advance(rng.choice([20, 120, 400]))   # asleep
        elif roll < 0.21:
            engine.allow(rng.choice([CHROME, SLACK]))
        elif roll < 0.22:
            engine.extend(5)

        live = engine.tick()

        if not live["running"]:
            break

        record = recorded(data_dir, sid, clock.now)
        assert (live["score"], live["deviations"], live["deviation_seconds"]) == (
            record["score"], record["deviations"], record["deviation_seconds"]
        )
        assert live["score"] == scoring.session_score(
            live["deviations"], live["deviation_seconds"]
        )
        last_tick = clock.now
        clock.advance(rng.choice([1, 1, 1, 0.5, 2]))

    if engine.running:
        # Stopped at the last tick, the session keeps the live score.
        clock.now = last_tick
        session = engine.stop()
        assert session["score"] == live["score"]


def test_window_that_took_focus_while_away_alerts_but_costs_nothing(rig, data_dir):
    """
    A window that comes to the front without any input (a pop-up while
    the user is away) raises the alert at once. Once the idle reader shows
    the user had left before it appeared, idle time starts before it, so
    it was never a deviation: no points lost, live and recorded alike.
    """

    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(30, CODE, "main.py", 20.0)])
    sid = engine.session_id

    live = run(engine, clock, screen, [(1, SLACK, "Incoming call", 50.0)])[-1]
    assert len(notifier.calls) == 1 and live["allowed"] is False

    live = run(engine, clock, screen, [(1, SLACK, "Incoming call", 61.0)])[-1]
    assert live["state"] == "idle"
    assert live["deviations"] == 0 and live["score"] == 100
    clock.now -= timedelta(seconds=1)
    assert recorded(data_dir, sid, clock.now)["score"] == 100


# ---------------------------------------------------------
# Heartbeat, extend, allow, completion
# ---------------------------------------------------------

def test_heartbeat_round_trips_through_live_status(rig, data_dir):
    engine, clock, screen, _ = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(3, CODE), (4, SLACK)])
    clock.now -= timedelta(seconds=1)

    assert analytics.live_status(data_dir, now=clock.now) == engine.live()


def test_extend_and_allow(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(10, CODE)])

    live = engine.extend(15)
    assert live["planned_s"] == 40 * 60
    assert live["remaining_s"] == 40 * 60 - 10

    with pytest.raises(ValueError):
        engine.extend(0)

    with pytest.raises(ValueError):
        engine.extend(241)

    run(engine, clock, screen, [(3, SLACK)])
    assert len(notifier.calls) == 1

    live = engine.allow(SLACK)
    assert live["allowed_apps"] == [CODE, SLACK]
    assert live["allowed"] is True and live["state"] == "allowed"

    with pytest.raises(ValueError):
        engine.allow("  ")

    # The deviation already recorded stays; nothing new is raised.
    live = run(engine, clock, screen, [(5, SLACK)])[-1]
    assert live["deviations"] == 1 and len(notifier.calls) == 1


def test_session_completes_when_the_time_is_up(rig, data_dir):
    engine, clock, screen, _ = rig
    screen.show(CODE)
    engine.start("Write", 1, [CODE])
    sid = engine.session_id
    run(engine, clock, screen, [(40, CODE), (20, SLACK)])

    clock.advance(30)       # the next tick comes late
    live = engine.tick()

    assert live["running"] is False
    session = engine.last_session
    assert session["id"] == sid and session["status"] == "Completed"
    # It ended at the planned end, not when the late tick noticed.
    assert session["end"] == (T0 + timedelta(minutes=1)).isoformat(timespec="seconds")
    assert session["actual_s"] == 60
    assert session["score"] == scoring.session_score(1, 20)

    heartbeat = analytics.read_live_file(data_dir)
    assert heartbeat["running"] is False and heartbeat["status"] == "Session completed"


def test_stop_returns_the_recorded_session(rig, data_dir):
    engine, clock, screen, _ = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    sid = engine.session_id
    run(engine, clock, screen, [(10, CODE), (10, CHROME)])

    session = engine.stop()

    assert session["id"] == sid and session["status"] == "Stopped"
    assert session["score"] == scoring.session_score(1, 10)
    assert engine.running is False and engine.stop() is None
    assert engine.live()["running"] is False

    rows = analytics.read_csv(data_dir / analytics.SESSIONS_FILE_NAME)
    assert [row["event_type"] for row in rows] == ["START", "END"]


def test_member_source(data_dir):
    screen = Screen()
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=Notifier(),
                         source="member")
    try:
        assert engine.start("Write", 25, [CODE])["source"] == "member"
        assert analytics.read_live_file(data_dir)["source"] == "member"
    finally:
        engine.stop()

    with pytest.raises(ValueError):
        FocusEngine(data_dir=data_dir, source="somebody")


# ---------------------------------------------------------
# Background thread
# ---------------------------------------------------------

def _wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)

    return condition()


def test_background_thread_ticks_until_stopped(data_dir):
    screen, notifier = Screen(), Notifier()
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=notifier,
                         tick_seconds=0.02)
    screen.show(CODE)

    assert engine.start_background() is False   # nothing to run yet

    engine.start("Write", 25, [CODE])
    assert engine.start_background() is True
    assert engine.start_background() is True    # still one thread
    assert _wait_for(lambda: engine.ticks >= 4)

    screen.show(SLACK)
    assert _wait_for(lambda: len(notifier.calls) == 1)
    assert engine.live()["allowed"] is False

    engine.stop()
    assert engine.join(2) is True
    ticks = engine.ticks
    time.sleep(0.06)
    assert engine.ticks == ticks
    assert len(notifier.calls) == 1


def test_background_thread_ends_with_a_completed_session(data_dir):
    clock, screen = Clock(), Screen(app=CODE)
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=Notifier(),
                         clock=clock, tick_seconds=0.02)
    engine.start("Write", 1, [CODE])
    engine.start_background()

    clock.advance(61)

    assert _wait_for(lambda: not engine.running)
    assert engine.join(2) is True
    assert engine.last_session["status"] == "Completed"


def test_background_thread_survives_a_failing_tick(data_dir, monkeypatch):
    screen = Screen(app=CODE)
    engine = FocusEngine(data_dir=data_dir, sampler=screen.sampler,
                         idle_reader=screen.idle_reader, notifier=Notifier(),
                         tick_seconds=0.02)
    engine.start("Write", 25, [CODE])
    calls = []
    real = engine._monitor_once

    def flaky(*args):
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("boom")
        real(*args)

    monkeypatch.setattr(engine, "_monitor_once", flaky)
    engine.start_background()

    try:
        assert _wait_for(lambda: len(calls) >= 4)
        assert engine.running is True
    finally:
        engine.stop()
        engine.join(2)


# ---------------------------------------------------------
# Notifications
# ---------------------------------------------------------

HOSTILE = 'x"; do shell script "touch /tmp/pwned" --\n$(rm -rf ~) `id`'


def test_mac_notification_passes_texts_as_arguments(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv, env = notify.command("Focus deviation", HOSTILE, platform="darwin")

    assert argv[0] == "osascript" and env == {}
    script = [argv[i + 1] for i, item in enumerate(argv) if item == "-e"]
    assert script == notify.MAC_SCRIPT
    assert 'sound name "Basso"' in script[1]
    # The texts are the run handler's arguments, never script source.
    assert argv[-2:] == ["Focus deviation", notify._clean(HOSTILE, notify.MAX_MESSAGE_LENGTH)]
    assert all(HOSTILE[:5] not in line for line in script)


def test_windows_and_linux_notifications(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/bin/{name}")

    argv, env = notify.command("Focus deviation", HOSTILE, platform="win32")
    assert argv[0] == "/bin/powershell" and argv[-1] == notify.WINDOWS_SCRIPT
    assert env["WORKSPACE_NOTIFY_MESSAGE"] == notify._clean(HOSTILE, notify.MAX_MESSAGE_LENGTH)
    assert HOSTILE[:5] not in notify.WINDOWS_SCRIPT

    argv, env = notify.command("-t", HOSTILE, platform="linux")
    assert argv[-3] == "--" and argv[-2] == "-t"

    assert notify.command("T", "M", platform="sunos5") is None
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.command("T", "M", platform="darwin") is None


def test_notification_texts_are_one_bounded_line():
    assert notify._clean("a\nb\tc\x00d", 50) == "a b c d"
    assert len(notify._clean("x" * 1000, notify.MAX_MESSAGE_LENGTH)) == notify.MAX_MESSAGE_LENGTH
    assert notify._clean(None, 10) == ""


def test_notify_is_non_blocking_and_never_raises(monkeypatch):
    started = []

    class FakeProcess:
        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **options):
        started.append((argv, options))
        return FakeProcess()

    monkeypatch.setattr(notify, "command", lambda title, message: (["helper", title, message], {}))
    monkeypatch.setattr(notify.subprocess, "Popen", fake_popen)
    monkeypatch.delenv(notify.DISABLE_ENV, raising=False)

    assert notify.notify("T", "M") is True
    argv, options = started[0]
    assert argv == ["helper", "T", "M"]
    assert "shell" not in options
    assert options["stdin"] is notify.subprocess.DEVNULL

    def broken(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(notify.subprocess, "Popen", broken)
    assert notify.notify("T", "M") is False

    monkeypatch.setattr(notify, "command", lambda title, message: 1 / 0)
    assert notify.notify("T", "M") is False

    monkeypatch.setenv(notify.DISABLE_ENV, "0")
    monkeypatch.setattr(notify.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(notify, "command", lambda title, message: (["helper"], {}))
    assert notify.notify("T", "M") is False
    assert len(started) == 1


# ---------------------------------------------------------
# Application lists
# ---------------------------------------------------------

def test_running_applications_on_macos(monkeypatch):
    commands = []

    def fake_run(argv):
        commands.append(argv)
        # osascript, the helper asking System Events, lists itself.
        return "Google Chrome\nFinder\ncode\nDock\nSlack\nslack\n\nUnknown\nosascript\n"

    monkeypatch.setattr(apps, "_run", fake_run)
    monkeypatch.setattr(apps.sys, "platform", "darwin")

    assert apps.running_applications() == [
        "Finder", CHROME, SLACK, CODE,
    ]
    assert commands[0][0] == "osascript"
    assert "background only is false" in " ".join(commands[0])


def test_running_applications_never_raises(monkeypatch):
    def broken(_argv):
        raise RuntimeError("System Events refused")

    monkeypatch.setattr(apps, "_run", broken)
    monkeypatch.setattr(apps.sys, "platform", "darwin")
    assert apps.running_applications() == []

    monkeypatch.setattr(apps.sys, "platform", "plan9")
    assert apps.running_applications() == []


def _write_raw(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["timestamp", "application", "window_title", "activity_status",
                         "idle_seconds"])
        for row in rows:
            writer.writerow(row)


def test_recent_applications_ranks_active_time(tmp_path):
    now = datetime(2026, 10, 10, 12, 0, 0)
    old = now - timedelta(days=9)
    rows = []

    def add(start, application, count, status="Active"):
        for n in range(count):
            moment = start + timedelta(seconds=5 * n)
            rows.append([moment.strftime("%Y-%m-%d %H:%M:%S"), application, "w", status, 0])
        return start + timedelta(seconds=5 * count)

    add(old, "Spotify", 500)                       # too long ago
    moment = add(now - timedelta(hours=3), "Code", 40)   # collector name -> VS Code
    moment = add(moment, CHROME, 20)
    moment = add(moment, SLACK, 50, status="Idle")       # idle time is not use
    moment = add(moment, "Unknown", 30)
    moment = add(moment, "loginwindow", 30)
    add(moment, "Notion", 2)
    raw = tmp_path / "raw" / "workspace_activity.csv"
    _write_raw(raw, rows)

    assert apps.recent_applications(raw_file=raw, now=now) == [CODE, CHROME, "Notion"]
    assert apps.recent_applications(raw_file=raw, now=now, limit=1) == [CODE]
    assert apps.recent_applications(raw_file=tmp_path / "missing.csv", now=now) == []


def test_recent_applications_reads_only_the_tail(tmp_path):
    raw = tmp_path / "raw.csv"
    _write_raw(raw, [
        [f"2026-10-10 12:00:{n:02d}", f"App{n}", "w", "Active", 0] for n in range(40)
    ])

    rows = list(apps._tail_rows(raw, max_bytes=200))

    assert 0 < len(rows) < 40
    assert rows[-1]["application"] == "App39"
    # Every row is whole: the partial line at the cut is dropped.
    assert all(row["timestamp"].startswith("2026-10-10 12:00:") for row in rows)
    assert all(set(row) == {"timestamp", "application", "window_title",
                            "activity_status", "idle_seconds"} for row in rows)


# --------------------------------------------------
# Looking at the Focus page is not a deviation
# --------------------------------------------------

def test_viewing_the_focus_page_in_the_browser_costs_nothing(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(engine_module.START_GRACE_SECONDS + 1, CODE)])

    # The user checks the timer in the browser for 20 s; the visible Focus
    # page polls once a second and says it is being viewed.
    screen.show(CHROME, "Workspace Analytics")
    for _ in range(20):
        engine.note_dashboard_view(CHROME)
        live = engine.tick()
        clock.advance(1)

    assert notifier.calls == []
    assert live["state"] == "neutral"
    assert live["status"] == engine_module.STATUS_DASHBOARD
    assert live["score"] == 100 and live["deviations"] == 0


def test_another_tab_in_the_same_browser_is_a_deviation_again(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(engine_module.START_GRACE_SECONDS + 1, CODE)])

    screen.show(CHROME, "Workspace Analytics")
    engine.note_dashboard_view(CHROME)
    engine.tick()

    # Switching to YouTube hides the Focus page: the polls stop saying
    # "viewing", and once the short window passes it counts.
    payloads = run(engine, clock, screen, [
        (engine_module.DASHBOARD_VIEW_SECONDS + 2, CHROME, "YouTube"),
    ])

    assert len(notifier.calls) == 1
    assert payloads[-1]["allowed"] is False
    assert payloads[-1]["score"] < 100


def test_viewing_one_browser_does_not_cover_another_app(rig):
    engine, clock, screen, notifier = rig
    screen.show(CODE)
    engine.start("Write", 25, [CODE])
    run(engine, clock, screen, [(engine_module.START_GRACE_SECONDS + 1, CODE)])

    screen.show(SLACK)
    engine.note_dashboard_view(CHROME)
    live = engine.tick()

    assert live["state"] == "deviation" and len(notifier.calls) == 1
