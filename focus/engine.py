"""
The headless focus engine: a focus session without the Tk window.

The dashboard (api/focus.py) and a team member's collector CLI
(collector/team_sync.py) run focus sessions through FocusEngine. Once a
second (FOCUS_TICK_SECONDS) it looks at the frontmost window, exactly as
the desktop Focus Assistant does (focus/focus_gui.py, _monitor_once):

    1. a gap since the last tick (the computer slept) is logged as idle;
    2. a session whose planned time is up ends as "Completed";
    3. the frontmost window is classified (allowed, deviation, idle,
       neutral, ...) and logged to focus_events.csv when it changed.

Switching to an application outside the declared focus zone starts a
deviation episode (the rule lives in focus/analytics.account_timeline).
Each new episode calls notifier(title, message) once; the episode lowers
the session's score (focus/scoring.py). Applications are never closed.

The live score is computed by analytics.build_records from the very rows
the engine writes, so it always equals the score the recorded session
gets if it ends at that moment.

The engine holds the data directory's InstanceLock for the whole
session, so it and the Tk Focus Assistant never run a session at once.
"""

import copy
import logging
import os
import sys
import threading
import time

from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from focus import analytics, scoring  # noqa: E402
from focus import notify as desktop_notify  # noqa: E402
from focus.focus_manager import (  # noqa: E402
    STATE_ALLOWED,
    STATE_DEVIATION,
    STATE_IDLE,
    STATE_NEUTRAL,
    STATE_SELF,
    STATE_UNKNOWN,
    FocusManager,
    InstanceLock,
    canonical_app_name,
)


LOGGER = logging.getLogger(__name__)

TICK_SECONDS = getattr(config, "FOCUS_TICK_SECONDS", 1)
START_GRACE_SECONDS = getattr(config, "FOCUS_START_GRACE_SECONDS", 10)

MAX_DURATION_MINUTES = 480
MAX_EXTEND_MINUTES = 240
MAX_ALLOWED_APPS = 30
MAX_TITLE_LENGTH = 200
MAX_GOAL_LENGTH = 500
MAX_APP_LENGTH = 120

SOURCES = analytics.LIVE_SOURCES

STATUS_GRACE = "Starting - switch to an allowed application"
STATUS_DASHBOARD = "Viewing the Focus page"

# While the dashboard's Focus page is the visible, focused tab it says so
# on each poll (once a second); the browser showing it then counts as
# neutral for this long after the last such poll, so checking the timer
# or pressing Stop never costs points. Another tab in that browser hides
# the page, so it is a deviation again within this time.
DASHBOARD_VIEW_SECONDS = 3
STATUS_PAUSED = "Paused (idle)"

DEVIATION_TITLE = "Focus deviation"


class FocusBusy(RuntimeError):
    """A session already runs here, or the Tk Focus Assistant is open."""


class FocusNotRunning(RuntimeError):
    """stop/extend/allow while no session runs."""


# ---------------------------------------------------------
# Input checks
# ---------------------------------------------------------

def _text(value, name, limit, required=False):
    if value is None:
        value = ""

    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")

    value = value.strip()

    if required and not value:
        raise ValueError(f"Please enter a {name}.")

    if len(value) > limit:
        raise ValueError(f"The {name} can be at most {limit} characters.")

    return value


def _whole_number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a whole number of minutes.")

    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be a whole number of minutes.")

    value = int(value)

    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high} minutes.")

    return value


def application_name(value):
    """
    A cleaned application name; ValueError when it cannot be one.
    """

    name = _text(value, "application name", MAX_APP_LENGTH, required=True)

    # Allowed apps are stored joined by " | " (focus_sessions.csv).
    if "|" in name:
        raise ValueError("An application name cannot contain '|'.")

    return name


def application_list(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("allowed_apps must be a list of application names.")

    if len(values) > MAX_ALLOWED_APPS:
        raise ValueError(f"Choose at most {MAX_ALLOWED_APPS} applications.")

    names = []
    seen = set()

    for value in values:
        name = application_name(value)

        if name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)

    if not names:
        raise ValueError("Please select at least one application.")

    return names


def validate_minutes(value, high=MAX_DURATION_MINUTES, name="duration_minutes"):
    return _whole_number(value, 1, high, name)


# ---------------------------------------------------------
# Defaults
# ---------------------------------------------------------

def _default_sampler():
    from collector.collector import get_active_process

    return get_active_process()


def _default_idle_reader():
    from collector.collector import get_idle_time

    return get_idle_time()


def _whole_second(moment):
    return moment.replace(microsecond=0)


# ---------------------------------------------------------
# Session manager that remembers what it wrote
# ---------------------------------------------------------

class _EngineManager(FocusManager):
    """
    A FocusManager that keeps the running session's rows in memory, as
    the CSV files store them (text), so the live score comes from the
    same rows (and the same code) as the recorded one.
    """

    def __init__(self, data_dir, clock, source):
        self.session_rows = []
        self.event_rows = []
        self.grace_app = None
        self.grace_until = None
        self.dashboard_app = None
        self.dashboard_until = None
        self.source = source
        super().__init__(data_dir, clock=clock)

    def start_session(self, title, goal, duration_minutes, allowed_apps):
        self.session_rows = []
        self.event_rows = []
        self.grace_app = None
        self.grace_until = None
        self.dashboard_app = None
        self.dashboard_until = None
        return super().start_session(title, goal, duration_minutes, allowed_apps)

    @staticmethod
    def _text_row(row):
        return {key: "" if value is None else str(value) for key, value in row.items()}

    def _safe_append(self, file_path, fieldnames, row):
        written = super()._safe_append(file_path, fieldnames, row)
        session_id = self.session_id

        # Only rows that reached the file: a session row that could not
        # be saved is not part of the recorded session either.
        if (
            written
            and session_id
            and file_path == self.sessions_file
            and row.get("session_id") == session_id
        ):
            self.session_rows.append(self._text_row(row))

        return written

    def _queue_focus_event(self, row):
        # Timeline rows are remembered once accepted (saved, or queued
        # until the file can be written again and saved then), never on
        # each write attempt.
        accepted = super()._queue_focus_event(row)

        if accepted and self.session_id and row.get("focus_session_id") == self.session_id:
            self.event_rows.append(self._text_row(row))

        return accepted

    def in_grace(self, application):
        """
        True while `application` is the one the session was started
        from and the start grace period has not passed.
        """

        return bool(
            self.grace_app is not None
            and self.grace_until is not None
            and self._now() < self.grace_until
            and self.normalize_app_name(application)
            == self.normalize_app_name(self.grace_app)
        )

    def viewing_dashboard(self, application):
        """
        True while `application` is the browser showing the Focus page
        and that page reported itself visible within DASHBOARD_VIEW_SECONDS.
        """

        return bool(
            self.dashboard_app
            and self.dashboard_until is not None
            and self._now() < self.dashboard_until
            and self._matches_name(application, self.dashboard_app)
        )

    @staticmethod
    def _matches_name(application, other):
        return bool(canonical_app_name(application)) and (
            canonical_app_name(application) == canonical_app_name(other)
        )

    def classify(self, application, window_title="", pid=None, idle_seconds=0.0):
        state = super().classify(application, window_title, pid, idle_seconds)

        if state == STATE_DEVIATION and self.viewing_dashboard(application):
            return STATE_NEUTRAL

        if state == STATE_DEVIATION and self.in_grace(application):
            # Logged as a neutral row: neither focus nor deviation.
            return STATE_NEUTRAL

        return state

    def start_dt(self):
        """
        The session start as recorded (whole seconds).
        """

        for row in self.session_rows:
            if row.get("event_type") == "START":
                parsed = analytics.parse_dt(row.get("timestamp"))
                if parsed is not None:
                    return parsed

        return _whole_second(self.started_at) if self.started_at else None


# ---------------------------------------------------------
# Engine
# ---------------------------------------------------------

class FocusEngine:
    """
    Runs one focus session at a time on `data_dir` (config.FOCUS_DATA_DIR
    by default), with no window of its own.

    sampler() -> (application, window_title, pid or None) and
    idle_reader() -> seconds without input default to the collector's
    readers; notifier(title, message) defaults to focus.notify.notify;
    clock() -> local datetime defaults to datetime.now. Tests pass fakes.
    """

    def __init__(self, data_dir=None, sampler=None, idle_reader=None, notifier=None,
                 source="dashboard", clock=None, tick_seconds=None):
        if source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}.")

        self.data_dir = analytics.focus_data_dir(data_dir)
        self.source = source
        self.tick_seconds = TICK_SECONDS if tick_seconds is None else tick_seconds

        self._sampler = sampler or _default_sampler
        self._idle_reader = idle_reader or _default_idle_reader
        self._notifier = desktop_notify.notify if notifier is None else notifier
        self._clock = clock or datetime.now

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._manager = None
        self._instance_lock = None
        self._tick_now = None
        self._live = None
        self._view = {}
        self._last_deviation = None
        self._last_error = None

        # The finished session's session_json (after stop or completion).
        self.last_session = None
        self.ticks = 0

    # ------------------------------------------------------------ state
    @property
    def running(self):
        manager = self._manager
        return bool(manager is not None and manager.running)

    @property
    def session_id(self):
        return self._manager.session_id if self.running else None

    @property
    def thread_alive(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _now(self):
        return self._tick_now if self._tick_now is not None else self._clock()

    def note_dashboard_view(self, application):
        """
        The Focus page is the visible, focused tab of `application` (the
        browser showing it): don't count that browser for a few seconds.
        """

        application = str(application or "").strip()[:120]

        with self._lock:
            manager = self._manager
            if application and manager is not None and manager.running:
                manager.dashboard_app = application
                manager.dashboard_until = self._now() + timedelta(seconds=DASHBOARD_VIEW_SECONDS)

    def live(self):
        """
        The current live payload (the GET /api/focus/live shape).
        """

        with self._lock:
            if self.running and self._live is not None:
                return copy.deepcopy(self._live)

        return analytics.live_status(self.data_dir)

    # ------------------------------------------------------------ control
    def start(self, title, duration_minutes, allowed_apps, goal=None):
        """
        Start a session and take a first look at the frontmost window.

        FocusBusy when a session already runs (here, or in a Tk Focus
        Assistant holding the lock); ValueError for invalid input.
        """

        title = _text(title, "focus session title", MAX_TITLE_LENGTH, required=True)
        goal = _text(goal, "goal", MAX_GOAL_LENGTH) or title
        minutes = validate_minutes(duration_minutes)
        apps = application_list(allowed_apps)

        with self._lock:
            if self.running:
                raise FocusBusy("A focus session is already running.")

            lock = InstanceLock(self.data_dir)

            if not lock.acquire():
                holder = lock.holder_pid()

                if holder == os.getpid():
                    # Another engine in this process (e.g. a second app).
                    raise FocusBusy("A focus session is already running.")

                who = f" (PID {holder})" if holder else ""
                raise FocusBusy(
                    f"Focus Assistant is open{who}. Close it, or use it to run "
                    "the session: two sessions would record over each other."
                )

            if lock.error:
                LOGGER.warning("%s; another Focus Assistant would not be detected.", lock.error)

            try:
                manager = _EngineManager(self.data_dir, self._now, self.source)
                manager.start_session(title, goal, minutes, apps)
            except Exception:
                lock.release()
                raise

            self._manager = manager
            self._instance_lock = lock
            self._live = None
            self._view = {}
            self._last_deviation = None
            self._last_error = None
            self.last_session = None
            self.ticks = 0
            self._wake.clear()

        return self.tick()

    def stop(self, status="Stopped"):
        """
        End the running session; its session_json, or None when none ran.
        """

        with self._lock:
            if not self.running:
                return None

            return self._finish(status)

    def extend(self, minutes):
        minutes = validate_minutes(minutes, MAX_EXTEND_MINUTES, "minutes")

        with self._lock:
            if not self.running:
                raise FocusNotRunning("There is no active focus session to extend.")

            self._manager.extend_session(minutes)
            self._refresh_live()
            return self.live()

    def allow(self, application):
        """
        Add an application to the running session's focus zone.
        """

        application = application_name(application)

        with self._lock:
            if not self.running:
                raise FocusNotRunning("There is no active focus session.")

            self._manager.allow_application(application)

        # Look again now, so the window in front is re-judged at once.
        return self.tick()

    # ------------------------------------------------------------ monitor
    def tick(self, now=None):
        """
        One monitor step; returns the live payload.

        The frontmost window and the idle time are read before the lock
        is taken: those readers start processes (osascript, ioreg) that
        can take up to seconds, and live(), stop(), extend() and allow()
        must not wait for them.
        """

        if not self.running:
            return self.live()

        sample = self._sample()
        idle_seconds = self._idle_seconds()

        with self._lock:
            if not self.running:
                return self.live()

            self._tick_now = now or self._clock()
            self.ticks += 1

            try:
                self._monitor_once(sample, idle_seconds)
            except Exception as error:
                self._log_error(error)
            finally:
                try:
                    if self.running:
                        self._refresh_live()
                except Exception as error:
                    self._log_error(error)

                self._tick_now = None

        return self.live()

    def _log_error(self, error):
        # Each distinct error once, not every second.
        message = f"{type(error).__name__}: {error}"

        if message != self._last_error:
            self._last_error = message
            LOGGER.exception("Focus engine tick failed")

    def _sample(self):
        try:
            application, window_title, pid = self._sampler()
        except Exception:
            return "Unknown", "Unknown", None

        return (
            str(application or "Unknown"),
            str(window_title or "Unknown"),
            pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        )

    def _idle_seconds(self):
        try:
            return float(self._idle_reader() or 0.0)
        except Exception:
            return 0.0

    def _monitor_once(self, sample, idle_seconds):
        manager = self._manager
        now = self._now()

        # After a sleep, log the time asleep as idle before anything else.
        manager.record_tick_gap(now)

        if manager.is_due():
            due = manager.expected_end_at or now
            self._finish(analytics.STATUS_COMPLETED, ended_at=min(due, now))
            return

        application, window_title, pid = sample

        if manager.grace_until is None:
            # The window in front at the start (the dashboard's browser,
            # the member's terminal) gets a moment before it counts.
            manager.grace_app = application
            manager.grace_until = manager.started_at + timedelta(seconds=START_GRACE_SECONDS)

        result = manager.evaluate_application(
            application, window_title, pid=pid, idle_seconds=idle_seconds
        )
        state = result["state"]

        if state == STATE_DEVIATION and result["logged"]:
            timeline = analytics.session_timeline(manager.event_rows, manager.session_id)

            if analytics.deviation_starts_episode(timeline, manager.start_dt()):
                self._on_new_deviation(application, window_title, now)

        status = result["status"]

        if state == STATE_SELF:
            view = ("Focus Assistant", None, None, "Focus Assistant")
        elif state == STATE_UNKNOWN:
            view = (None, None, None, "Active window unknown")
        elif state == STATE_IDLE:
            view = (application, window_title, None, STATUS_PAUSED)
        elif state == STATE_NEUTRAL:
            if manager.viewing_dashboard(application) and not manager._matches_allowed(application):
                view = (application, window_title, None, STATUS_DASHBOARD)
            else:
                grace = manager.in_grace(application) and not manager._matches_allowed(application)
                view = (application, window_title, None, STATUS_GRACE if grace else "Neutral")
        elif state == STATE_ALLOWED:
            view = (application, window_title, True, status)
        else:
            view = (application, window_title, False, status)

        current_app, current_window, allowed, status = view

        if manager.last_write_error:
            status = manager.last_write_error

        self._view = {
            "current_app": current_app,
            "current_window": current_window,
            "allowed": allowed,
            "state": state if state in analytics.LIVE_STATES else None,
            "status": status,
        }

    def _on_new_deviation(self, application, window_title, now):
        allowed = ", ".join(self._manager.allowed_apps)
        penalty = scoring.SWITCH_PENALTY

        self._last_deviation = {
            "app": application,
            "window_title": window_title,
            "at": analytics.iso_ts(_whole_second(now)),
            "penalty": penalty,
        }

        message = (
            f"You switched to {application}. -{penalty} points. "
            f"Return to {allowed}."
        )

        try:
            self._notifier(DEVIATION_TITLE, message)
        except Exception as error:  # a notifier must never stop monitoring
            self._log_error(error)

    # ------------------------------------------------------------ payloads
    def _record(self, end):
        manager = self._manager
        records = analytics.build_records(
            manager.session_rows,
            manager.event_rows,
            now=_whole_second(end),
            active_session_id=manager.session_id,
        )

        for record in records:
            if record["id"] == manager.session_id:
                return record

        return None

    def _refresh_live(self):
        """
        Rebuild the live payload and write it to live.json.
        """

        manager = self._manager
        now = self._now()
        record = self._record(now)
        start = manager.start_dt()
        episodes = record["deviation_episodes"] if record else []

        payload = {
            "running": True,
            "stale": False,
            "session_id": manager.session_id,
            "source": self.source,
            "title": manager.title,
            "goal": manager.goal,
            "started_at": analytics.iso_ts(start),
            "planned_s": int(manager.duration_minutes) * 60,
            "remaining_s": manager.remaining_seconds(),
            "allowed_apps": list(manager.allowed_apps),
            "current_app": self._view.get("current_app"),
            "current_window": self._view.get("current_window"),
            "allowed": self._view.get("allowed"),
            "state": self._view.get("state"),
            "status": self._view.get("status") or "Focus session running",
            "score": record["score"] if record else scoring.START_SCORE,
            "deviations": record["deviations"] if record else 0,
            "deviation_seconds": record["deviation_seconds"] if record else 0,
            "last_deviation": copy.deepcopy(self._last_deviation),
            "recent_deviations": [
                {
                    "app": episode["app"],
                    "at": analytics.iso_ts(episode["at"]),
                    "seconds": int(round(episode["seconds"])),
                }
                for episode in reversed(episodes)
            ][:analytics.MAX_RECENT_DEVIATIONS],
            "updated_at": analytics.iso_ts(_whole_second(now)),
            "pid": os.getpid(),
        }

        self._live = payload
        heartbeat = {key: value for key, value in payload.items() if key != "stale"}
        analytics.write_live(heartbeat, self.data_dir)

    def _finish(self, status, ended_at=None):
        """
        End the session (lock held): END row, final heartbeat, lock
        released. Returns the finished session's session_json.
        """

        manager = self._manager
        session_id = manager.session_id
        manager.stop_session(status, ended_at=ended_at)
        now = self._now()

        analytics.write_live({
            "running": False,
            "session_id": None,
            "source": self.source,
            "status": f"Session {str(status).lower()}",
            "updated_at": analytics.iso_ts(_whole_second(now)),
            "pid": os.getpid(),
        }, self.data_dir)

        if self._instance_lock is not None:
            self._instance_lock.release()
            self._instance_lock = None

        self._live = None
        self._wake.set()

        session = None

        try:
            for record in analytics.session_records(self.data_dir, now=now):
                if record["id"] == session_id:
                    session = analytics.session_json(record)
                    break
        except Exception as error:
            self._log_error(error)

        if session is None:
            # The files could not be read back: use the rows written.
            records = analytics.build_records(
                manager.session_rows, manager.event_rows, now=now,
            )
            session = next(
                (analytics.session_json(r) for r in records if r["id"] == session_id),
                None,
            )

        self.last_session = session
        return session

    # ------------------------------------------------------------ thread
    def start_background(self):
        """
        Tick every tick_seconds on a daemon thread while the session
        runs; the thread ends with the session. True when one runs.
        """

        with self._lock:
            if not self.running:
                return False

            if self._thread is not None and self._thread.is_alive():
                return True

            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run, name="focus-engine", daemon=True
            )
            self._thread.start()
            return True

    def _run(self):
        interval = max(0.01, float(self.tick_seconds))
        next_at = time.monotonic() + interval

        while True:
            delay = next_at - time.monotonic()

            if delay > 0:
                self._wake.wait(delay)

            with self._lock:
                if not self.running:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return

                self._wake.clear()

            try:
                self.tick()
            except Exception as error:
                self._log_error(error)

            next_at += interval
            current = time.monotonic()

            if next_at < current:
                # A slow tick or a sleep: carry on from now, no burst.
                next_at = current + interval

    def join(self, timeout=None):
        """
        Wait for the background thread to end (after the session).
        """

        thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

        return not self.thread_alive
