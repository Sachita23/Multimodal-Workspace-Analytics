import csv
import errno
import logging
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from focus import analytics  # noqa: E402

LOGGER = logging.getLogger(__name__)

FOCUS_DATA_DIR = Path(config.FOCUS_DATA_DIR)
FOCUS_SESSIONS_FILE = FOCUS_DATA_DIR / analytics.SESSIONS_FILE_NAME
FOCUS_EVENTS_FILE = FOCUS_DATA_DIR / analytics.EVENTS_FILE_NAME
FOCUS_LIVE_FILE = FOCUS_DATA_DIR / analytics.LIVE_FILE_NAME

# Held by the running Focus Assistant, so only one assistant writes to a
# data directory at a time.
LOCK_FILE_NAME = ".focus_assistant.lock"

# More than this between two monitor ticks means the computer slept (or
# the loop was blocked): nothing was observed, so it counts as idle. A
# normal tick, including slow window queries, takes a few seconds.
MAX_TICK_GAP_SECONDS = 15

SESSION_FIELDS = [
    "session_id", "event_type", "timestamp", "title", "goal",
    "planned_duration_minutes", "allowed_apps", "completion_status"
]
EVENT_FIELDS = [
    "timestamp", "focus_session_id", "title", "goal", "application",
    "window_title", "allowed", "status", "remaining_seconds"
]

# Timeline statuses (focus_events.csv). Rows with an empty `allowed`
# column count as neither focus nor deviation (see focus/analytics.py).
STATUS_ALLOWED = "Within focus zone"
STATUS_DEVIATION = "Focus deviation"
STATUS_IDLE = "Idle"
STATUS_NEUTRAL = "Neutral"
STATUS_SELF = "Focus Assistant"
STATUS_PROMPT = "Completion prompt"
STATUS_UNKNOWN = "Active window unknown"

# What classify() returns.
STATE_ALLOWED = "allowed"
STATE_DEVIATION = "deviation"
STATE_IDLE = "idle"
STATE_NEUTRAL = "neutral"
STATE_SELF = "self"
STATE_UNKNOWN = "unknown"

# Rows that could not be saved (e.g. the CSV is open in Excel on Windows,
# or the disk is full) wait in memory, oldest first, and are written as
# soon as the file can be written again, so the timeline keeps them. Past
# this many, new rows are refused (and retried) instead of queued.
MAX_PENDING_EVENTS = 3600

# Other names of one application, as the user may type them or another
# OS reports them (lower case, .exe removed) -> one canonical name. An
# application matches the focus zone only by its canonical name, never
# by a part of it, so "Code" does not let Xcode in, nor "Mail" Gmail.
APPLICATION_ALIASES = {
    "code": "visual studio code",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "chrome": "google chrome",
    "edge": "microsoft edge",
    "msedge": "microsoft edge",
    "firefox": "mozilla firefox",
    "word": "microsoft word",
    "winword": "microsoft word",
    "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint",
    "powerpnt": "microsoft powerpoint",
    "outlook": "microsoft outlook",
    "olk": "microsoft outlook",
    "onenote": "microsoft onenote",
    "teams": "microsoft teams",
    "ms-teams": "microsoft teams",
    "iterm": "iterm2",
    "zoom.us": "zoom",
    "pycharm64": "pycharm",
    "idea": "intellij idea",
    "idea64": "intellij idea",
    "obs": "obs studio",
    "obs64": "obs studio",
    "windowsterminal": "windows terminal",
    "explorer": "file explorer",
    "devenv": "visual studio",
}


def canonical_app_name(value):
    """Lower-case application name with .exe removed, runs of spaces
    collapsed and known aliases resolved ("" for no name)."""
    name = " ".join(analytics.normalize_application(value).split())
    return APPLICATION_ALIASES.get(name, name)


STATE_STATUS = {
    STATE_ALLOWED: STATUS_ALLOWED,
    STATE_DEVIATION: STATUS_DEVIATION,
    STATE_IDLE: STATUS_IDLE,
    STATE_NEUTRAL: STATUS_NEUTRAL,
    STATE_SELF: STATUS_SELF,
    STATE_UNKNOWN: STATUS_UNKNOWN,
}


def _other_process_alive(pid):
    """Whether `pid` is another live process (assume yes when unsure)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid == os.getpid():
        return False
    try:
        import psutil
    except ImportError:
        return True
    return psutil.pid_exists(pid)


# ---------------------------------------------------------------- one instance
# The OS releases these locks when the process ends, even after a crash,
# so a lock file left behind never blocks the next start.

# Windows locks this byte; the pid is written before it, where another
# process can still read it.
_LOCK_BYTE = 64
_PID_WIDTH = 20

# Lock files held by this process (realpath -> InstanceLock).
_HELD_LOCKS = {}


def _try_lock(fd):
    """True: locked. False: another process holds it. None: unsupported."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        held = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                getattr(errno, "EDEADLK", None), getattr(errno, "EDEADLOCK", None)}
        held.discard(None)
        return False if error.errno in held else None
    return True


def _unlock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def lock_file_path(data_dir=None):
    return analytics.focus_data_dir(data_dir) / LOCK_FILE_NAME


class InstanceLock:
    """Only one Focus Assistant per data directory.

    Two assistants on one directory would overwrite each other's
    heartbeat in live.json, and the second one's start-up recovery would
    close the first one's running session as 'Interrupted'.
    """

    def __init__(self, data_dir=None):
        self.path = lock_file_path(data_dir)
        self.error = None
        self._fd = None

    @property
    def held(self):
        return self._fd is not None

    def acquire(self):
        """Take the lock without waiting.

        False when another process holds it. True when this process now
        holds it, or when locking is impossible here (then `error` says
        why and the assistant runs unguarded rather than not at all).
        """
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as error:
            self.error = f"Could not create {self.path}: {error}"
            return True
        locked = _try_lock(fd)
        if not locked:
            os.close(fd)
            if locked is None:
                self.error = f"Could not lock {self.path}"
                return True
            return False
        self._fd = fd
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid():<{_PID_WIDTH}}\n".encode("ascii"))
        except OSError:
            pass
        _HELD_LOCKS[os.path.realpath(self.path)] = self
        return True

    def release(self):
        if self._fd is None:
            return
        _HELD_LOCKS.pop(os.path.realpath(self.path), None)
        _unlock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def holder_pid(self):
        """The pid written by the process holding the lock, if readable."""
        try:
            with open(self.path, "rb") as file:
                return int(file.read(_PID_WIDTH).strip())
        except (OSError, ValueError):
            return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def assistant_running_elsewhere(data_dir=None):
    """True while another process holds the data directory's lock."""
    path = lock_file_path(data_dir)
    if os.path.realpath(path) in _HELD_LOCKS:
        return False
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False  # no lock file: no assistant has run with locking
    try:
        locked = _try_lock(fd)
        if locked:
            _unlock(fd)
        return locked is False
    finally:
        os.close(fd)


class FocusManager:
    """Stores focus-session intent and app-level deviations.

    Sessions go to focus_sessions.csv, the window timeline to
    focus_events.csv and a once-per-tick heartbeat to live.json, all under
    config.FOCUS_DATA_DIR (or `data_dir`). A failed write is kept in
    `last_write_error` and never raised into the monitor loop.
    """

    # Written into the heartbeat's "source" (the focus engine overrides it).
    source = "assistant"

    def __init__(self, data_dir=None, idle_threshold=None, clock=None):
        # clock() -> the current local time; datetime.now unless a caller
        # (the focus engine, tests) supplies its own.
        self.clock = clock
        self.data_dir = Path(data_dir) if data_dir else Path(config.FOCUS_DATA_DIR)
        self.sessions_file = self.data_dir / analytics.SESSIONS_FILE_NAME
        self.events_file = self.data_dir / analytics.EVENTS_FILE_NAME
        self.live_file = self.data_dir / analytics.LIVE_FILE_NAME
        self.idle_threshold = (
            idle_threshold if idle_threshold is not None
            else getattr(config, "IDLE_THRESHOLD_SECONDS", 60)
        )

        self.session_id = None
        self.title = ""
        self.goal = ""
        self.duration_minutes = 0
        self.allowed_apps = []
        self.started_at = None
        self.expected_end_at = None
        self.running = False
        self.last_application = None
        self.last_window_title = None
        self.last_compliance_status = None
        self.last_event_key = None
        self.last_write_error = None
        # Wall-clock time of the last monitor tick, and of the newest
        # timeline row written, while a session runs.
        self.last_tick_at = None
        self.previous_tick_at = None
        self.last_event_at = None
        # The tick before the current deviation row was logged: the
        # window came to the front some time after it.
        self.deviation_since = None
        # Timeline rows not saved yet (see MAX_PENDING_EVENTS).
        self.pending_events = []

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.last_write_error = f"Could not create {self.data_dir}: {error}"

        # Older files may lack columns; add them once here, never per append.
        self._migrate_schema(self.sessions_file, SESSION_FIELDS)
        self._migrate_schema(self.events_file, EVENT_FIELDS)
        self.recovered_sessions = self.recover_orphaned_sessions()

    def _now(self):
        return self.clock() if self.clock is not None else datetime.now()

    @staticmethod
    def normalize_app_name(value):
        if not value:
            return ""
        return str(value).strip().lower()

    def start_session(self, title, goal, duration_minutes, allowed_apps):
        if self.running:
            raise RuntimeError("A focus session is already running.")

        title = str(title).strip()
        goal = str(goal).strip()
        if not title:
            raise ValueError("Please enter a focus session title.")
        if not goal:
            raise ValueError("Please enter what you want to focus on.")

        duration_minutes = int(duration_minutes)
        if duration_minutes <= 0:
            raise ValueError("Focus duration must be greater than zero.")

        cleaned_apps = []
        existing = set()
        for app in allowed_apps:
            app = str(app).strip()
            normalized = self.normalize_app_name(app)
            if app and normalized not in existing:
                cleaned_apps.append(app)
                existing.add(normalized)

        if not cleaned_apps:
            raise ValueError("Please select at least one application.")

        self.session_id = str(uuid.uuid4())
        self.title = title
        self.goal = goal
        self.duration_minutes = duration_minutes
        self.allowed_apps = cleaned_apps
        self.started_at = self._now()
        self.expected_end_at = self.started_at + timedelta(minutes=duration_minutes)
        self.running = True
        self.last_application = None
        self.last_window_title = None
        self.last_compliance_status = None
        self.last_event_key = None
        self.last_tick_at = None
        self.previous_tick_at = None
        self.last_event_at = None
        self.deviation_since = None

        self._write_session_start()
        return self.session_id

    def remaining_seconds(self):
        if not self.running or not self.expected_end_at:
            return 0
        remaining = (self.expected_end_at - self._now()).total_seconds()
        return max(0, int(remaining))

    # ------------------------------------------------------------ classify
    @staticmethod
    def is_own_window(application, window_title="", pid=None):
        """True when the frontmost window belongs to this process.

        The process id decides when the collector can report it. Otherwise
        fall back to the assistant's window titles (on macOS the process
        shows up as 'Python', so a bare 'Python' is not enough).
        """
        if pid is not None:
            try:
                return int(pid) == os.getpid()
            except (TypeError, ValueError):
                pass
        return (
            analytics.has_internal_title(window_title)
            or analytics.is_internal_window(application, window_title)
        )

    def _matches_allowed(self, application):
        """Whether `application` is one of the allowed applications: the
        same name once case, .exe and aliases are set aside. A name that
        merely contains (or is contained in) an allowed one does not match.
        """
        app_name = canonical_app_name(application)
        if not app_name:
            return False
        return any(canonical_app_name(allowed) == app_name for allowed in self.allowed_apps)

    def classify(self, application, window_title="", pid=None, idle_seconds=0.0):
        """One of: idle, self, unknown, neutral, allowed, deviation."""
        try:
            idle = float(idle_seconds or 0) >= self.idle_threshold
        except (TypeError, ValueError):
            idle = False
        if idle:
            return STATE_IDLE
        if self.is_own_window(application, window_title, pid):
            return STATE_SELF
        if analytics.is_unknown_application(application):
            return STATE_UNKNOWN
        # Without a pid, a 'Python' window with no readable title may be the
        # assistant itself (macOS without the Accessibility permission).
        if (
            pid is None
            and analytics.is_python_application(application)
            and str(window_title or "").strip().lower() in ("", "unknown")
        ):
            return STATE_UNKNOWN
        if analytics.is_neutral_window(application, window_title):
            return STATE_NEUTRAL
        return STATE_ALLOWED if self._matches_allowed(application) else STATE_DEVIATION

    def is_application_allowed(self, application, window_title=""):
        if not self.running:
            return True
        if self.is_own_window(application, window_title):
            return True
        return self._matches_allowed(application)

    def record_tick_gap(self, now=None):
        """Note a monitor tick; log the time since the last one as idle
        when it is longer than MAX_TICK_GAP_SECONDS.

        While the computer sleeps no tick runs, and on wake the first
        input resets the OS idle time, so without this the window in
        front before the sleep would get all of the time asleep. The
        Idle row goes at the last tick, the last moment anything was
        observed. Returns True when it logged one.
        """
        now = now or self._now()
        if not self.running:
            self.last_tick_at = None
            self.previous_tick_at = None
            return False
        if self.last_tick_at is not None and now == self.last_tick_at:
            return False  # this tick was already noted (the engine notes it first)
        last, self.last_tick_at = self.last_tick_at, now
        self.previous_tick_at = last
        if last is None or (now - last).total_seconds() <= MAX_TICK_GAP_SECONDS:
            return False
        if self.last_event_key == (STATE_IDLE,):
            return False  # already idle since before the gap
        if self._write_focus_event(self.last_application or "", self.last_window_title or "",
                                   None, STATUS_IDLE, timestamp=last):
            self.last_event_key = (STATE_IDLE,)
            return True
        return False

    def _idle_started_at(self, now, idle_seconds):
        """When input stopped: now - idle_seconds, but never before the
        last row written (or the start of the session).

        When the last input came after the current deviation window
        appeared (the user switched to it, e.g. clicked into a video and
        then sat watching), idle starts at least a second after the
        deviation row, so the switch stays an episode, as announced.
        Back-dating it onto the deviation row would erase the deviation
        and its penalty. A window that came to the front after the last
        input (a pop-up while the user was away) was never a deviation.
        """
        try:
            started = now - timedelta(seconds=max(0.0, float(idle_seconds or 0)))
        except (TypeError, ValueError, OverflowError):
            return now
        deviation_floor = None
        if (
            self.deviation_since is not None
            and self.last_event_at is not None
            and self.last_event_key
            and self.last_event_key[0] == STATE_DEVIATION
            and started >= self.deviation_since
        ):
            deviation_floor = self.last_event_at + timedelta(seconds=1)
        for floor in (self.started_at, self.last_event_at, deviation_floor):
            if floor is not None and started < floor:
                started = floor
        return min(started, now)

    def evaluate_application(self, application, window_title="", pid=None, idle_seconds=0.0):
        """Classify the frontmost window and log a timeline row when it changed.

        `allowed` is True/False for zone apps and deviations, None when the
        window counts as neither (idle, our own windows, OS shell windows)
        or cannot be identified ('unknown': logged once as a neutral row,
        so its time is not credited to the previous window, and never a
        reminder).
        """
        if not self.running:
            return {"allowed": True, "state": "inactive", "status": "No active focus session",
                    "application": application, "window_title": window_title, "logged": False}

        now = self._now()
        if self.pending_events:
            self.flush_pending_events()
        self.record_tick_gap(now)
        previous_tick = self.previous_tick_at

        state = self.classify(application, window_title, pid, idle_seconds)
        result = {
            "allowed": None,
            "state": state,
            "status": STATE_STATUS.get(state, STATUS_UNKNOWN),
            "application": application,
            "window_title": window_title,
            "logged": False,
        }

        if state == STATE_ALLOWED:
            result["allowed"] = True
        elif state == STATE_DEVIATION:
            result["allowed"] = False

        # Idle, our own windows and unknown windows are one stretch each,
        # whatever window titles pass by meanwhile.
        if state in (STATE_IDLE, STATE_SELF, STATE_UNKNOWN):
            key = (state,)
        else:
            key = (state, application, window_title)

        if key != self.last_event_key:
            # Idle began when input stopped, not when the threshold passed.
            timestamp = self._idle_started_at(now, idle_seconds) if state == STATE_IDLE else now
            was_deviation = bool(self.last_event_key) and self.last_event_key[0] == STATE_DEVIATION
            if self._write_focus_event(application, window_title, result["allowed"],
                                       result["status"], timestamp=timestamp):
                self.last_event_key = key
                result["logged"] = True
                if state == STATE_DEVIATION:
                    # Only a switch from outside a deviation can be erased
                    # by back-dated idle (see _idle_started_at); another
                    # window of the same excursion changes no episode.
                    self.deviation_since = None if was_deviation else (previous_tick or now)

        if state != STATE_UNKNOWN:
            self.last_application = application
            self.last_window_title = window_title
        self.last_compliance_status = result["status"]
        return result

    # ------------------------------------------------------------ changes
    def allow_application(self, application):
        """Add an application to the running session's focus zone.

        Recorded as an ALLOW row in focus_sessions.csv rather than a
        timeline row; the next tick logs the window as in-zone.
        """
        application = str(application).strip()
        if not application or not self.running:
            return False
        normalized = self.normalize_app_name(application)
        existing = {self.normalize_app_name(app) for app in self.allowed_apps}
        if normalized in existing:
            return False
        self.allowed_apps.append(application)
        self.last_event_key = None
        self._write_session_event("ALLOW", completion_status=f"Allowed {application}")
        return True

    def mark_due(self):
        """Log the planned end, so the time the completion prompt stays open
        counts as neither focus nor deviation."""
        if not self.running:
            return
        now = self._now()
        due = min(self.expected_end_at or now, now)
        self.last_event_key = None
        # The marker covers the time until the prompt is answered (no
        # ticks run meanwhile), so that is not a gap to log as idle.
        self.last_tick_at = None
        self._write_focus_event("Focus Assistant", "Focus time completed", None,
                                STATUS_PROMPT, timestamp=due)

    def stop_session(self, completion_status="Stopped", ended_at=None):
        if not self.running:
            return
        stopped_at = ended_at or self._now()
        if self.started_at and stopped_at < self.started_at:
            stopped_at = self.started_at
        self.flush_pending_events()
        self._write_session_end(stopped_at, completion_status)
        self.running = False
        self.last_event_key = None
        self.last_tick_at = None

    def is_due(self):
        return bool(self.running and self.remaining_seconds() <= 0)

    def extend_session(self, additional_minutes):
        if not self.running:
            raise RuntimeError("There is no active focus session to extend.")
        additional_minutes = int(additional_minutes)
        if additional_minutes <= 0:
            raise ValueError("Additional focus time must be greater than zero.")

        # Extending never shortens: add to the planned end, or to now once
        # the planned end has passed.
        now = self._now()
        base = max(self.expected_end_at, now) if self.expected_end_at else now
        self.duration_minutes += additional_minutes
        self.expected_end_at = base + timedelta(minutes=additional_minutes)
        self.last_event_key = None
        self._write_session_event(
            "EXTEND",
            completion_status=f"+{additional_minutes} minutes",
        )
        return self.remaining_seconds()

    # ------------------------------------------------------------ recovery
    def recover_orphaned_sessions(self):
        """Write an END 'Interrupted' row for every START without an END.

        The END goes at the session's last sign of life: its last timeline
        row, or the heartbeat if that was for it. Nothing is recovered while
        another assistant holds this data directory's lock (one of the
        open sessions is its running session), and a session another
        assistant still reports in live.json is left alone.
        """
        try:
            rows = analytics.read_csv(self.sessions_file)
            starts, ends, latest = analytics.index_sessions(rows)
            orphans = [sid for sid in starts if sid not in ends]
            if not orphans:
                return []

            if assistant_running_elsewhere(self.data_dir):
                # Its sessions are not orphans; until it ends they still
                # read as interrupted only if it stops heartbeating.
                return []

            heartbeat = analytics.read_live_file(self.data_dir)
            still_running = analytics.running_session_id(heartbeat)
            if still_running and not _other_process_alive(heartbeat.get("pid")):
                still_running = None
            heartbeat_time = analytics.parse_dt(heartbeat.get("updated_at")) if heartbeat else None

            last_seen = {}
            for event in analytics.read_csv(self.events_file):
                sid = (event.get("focus_session_id") or "").strip()
                timestamp = analytics.parse_dt(event.get("timestamp"))
                if sid in starts and timestamp and (sid not in last_seen or timestamp > last_seen[sid]):
                    last_seen[sid] = timestamp

            recovered = []
            for sid in orphans:
                if sid == still_running:
                    continue
                start = starts[sid]
                start_dt = analytics.parse_dt(start.get("timestamp"))
                if start_dt is None:
                    continue
                candidates = [start_dt]
                if sid in last_seen:
                    candidates.append(last_seen[sid])
                if heartbeat_time and heartbeat.get("session_id") == sid:
                    candidates.append(heartbeat_time)
                row = latest.get(sid, start)
                written = self._safe_append(self.sessions_file, SESSION_FIELDS, {
                    "session_id": sid,
                    "event_type": "END",
                    "timestamp": analytics.format_ts(max(candidates)),
                    "title": start.get("title", ""),
                    "goal": start.get("goal", ""),
                    "planned_duration_minutes": row.get("planned_duration_minutes", ""),
                    "allowed_apps": row.get("allowed_apps", ""),
                    "completion_status": analytics.STATUS_INTERRUPTED,
                })
                if written:
                    recovered.append(sid)
            return recovered
        except Exception as error:  # recovery must never stop the assistant
            self.last_write_error = f"Could not recover interrupted sessions: {error}"
            return []

    # ------------------------------------------------------------ heartbeat
    def heartbeat_payload(self, current_app=None, allowed=None, status=None):
        running = self.running
        return {
            "running": running,
            "session_id": self.session_id if running else None,
            "title": self.title if running else None,
            "goal": self.goal if running else None,
            "source": self.source if running else None,
            "started_at": (
                self.started_at.isoformat(timespec="seconds")
                if running and self.started_at else None
            ),
            "planned_s": int(self.duration_minutes) * 60 if running else None,
            "remaining_s": self.remaining_seconds() if running else None,
            "allowed_apps": list(self.allowed_apps) if running else [],
            "current_app": current_app if running else None,
            "allowed": allowed if running and isinstance(allowed, bool) else None,
            "status": status or ("Focus session running" if running else "No active session"),
            "updated_at": self._now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }

    def write_heartbeat(self, current_app=None, allowed=None, status=None):
        """Atomically replace live.json; returns False if it could not."""
        return analytics.write_live(
            self.heartbeat_payload(current_app, allowed, status), self.data_dir
        )

    # ------------------------------------------------------------ CSV files
    @staticmethod
    def _read_header(file_path):
        """The header of a CSV (first non-blank line), or [] if there is none.

        Read as bytes, so a byte that is not UTF-8 anywhere in the file
        (e.g. after a save from Excel) never blocks appending to it.
        """
        try:
            with open(file_path, "rb") as file:
                for raw in file:
                    for piece in raw.splitlines():
                        line = analytics.decode_line(piece).lstrip("﻿")
                        if line.strip():
                            return next(csv.reader([line]), [])
        except FileNotFoundError:
            return []
        return []

    @staticmethod
    def _unfinished_line(file_path):
        """(ends without a line break, last line has an open quote).

        A row cut short (e.g. a crash or a full disk mid-write) can end
        inside a quoted field; the next row appended after it would then
        be read as part of that field, and so would every row after it.
        """
        with open(file_path, "rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            if size == 0:
                return False, False
            file.seek(max(0, size - 65536))
            tail = file.read()
        if tail[-1:] in (b"\n", b"\r"):
            return False, False
        start = max(tail.rfind(b"\n"), tail.rfind(b"\r")) + 1
        return True, tail[start:].count(b'"') % 2 == 1

    @staticmethod
    def _append_csv(file_path, fieldnames, row):
        """Append one row, in the column order the file already uses.

        Only the header line (and the end of the file) is read. A file
        with no header yet gets one.
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)
        header = FocusManager._read_header(file_path)

        if not header:
            with open(file_path, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(row)
            return

        needs_newline, open_quote = FocusManager._unfinished_line(file_path)
        with open(file_path, "a", newline="", encoding="utf-8") as file:
            if needs_newline:
                # Close a cut-off quoted field so it ends with its own row.
                file.write('"\r\n' if open_quote else "\r\n")
            csv.DictWriter(file, fieldnames=header, extrasaction="ignore",
                           restval="").writerow(row)

    def _migrate_schema(self, file_path, fieldnames):
        """Add missing columns to an existing file (one rewrite, at start-up).

        The file is rewritten only when every row in it can be read as
        it is. A file that is not UTF-8 or has a damaged row is left
        exactly as it is (rows are still appended in its own column
        order), so a rewrite can never drop rows.
        """
        temp_file = file_path.with_suffix(file_path.suffix + ".tmp")
        try:
            header = self._read_header(file_path)
            if not header or set(fieldnames) <= set(header):
                return
            merged = list(header) + [field for field in fieldnames if field not in header]
            rows = analytics.read_csv(file_path, strict=True)
            with open(temp_file, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=merged, extrasaction="ignore", restval="")
                writer.writeheader()
                for old_row in rows:
                    writer.writerow(old_row)
            os.replace(temp_file, file_path)
        except (OSError, csv.Error, ValueError) as error:
            try:
                temp_file.unlink()
            except OSError:
                pass
            self.last_write_error = (
                f"{file_path.name} was left unchanged; it could not be updated "
                f"to the current columns: {error}"
            )
            LOGGER.warning("%s", self.last_write_error)

    def _safe_append(self, file_path, fieldnames, row):
        try:
            self._append_csv(file_path, fieldnames, row)
        except (OSError, csv.Error, ValueError) as error:
            self.last_write_error = f"Could not save to {file_path.name}: {error}"
            return False
        self.last_write_error = None
        return True

    def _session_row(self, event_type, timestamp, completion_status=""):
        return {
            "session_id": self.session_id,
            "event_type": event_type,
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "title": self.title,
            "goal": self.goal,
            "planned_duration_minutes": self.duration_minutes,
            "allowed_apps": " | ".join(self.allowed_apps),
            "completion_status": completion_status,
        }

    def _write_session_event(self, event_type, completion_status=""):
        return self._safe_append(
            self.sessions_file, SESSION_FIELDS,
            self._session_row(event_type, self._now(), completion_status),
        )

    def _write_session_start(self):
        return self._safe_append(
            self.sessions_file, SESSION_FIELDS,
            self._session_row("START", self.started_at),
        )

    def _write_session_end(self, stopped_at, completion_status):
        return self._safe_append(
            self.sessions_file, SESSION_FIELDS,
            self._session_row("END", stopped_at, completion_status),
        )

    def flush_pending_events(self):
        """Write the queued timeline rows, oldest first; True when none
        is left waiting."""
        while self.pending_events:
            if not self._safe_append(self.events_file, EVENT_FIELDS, self.pending_events[0]):
                return False
            self.pending_events.pop(0)
        return True

    def _queue_focus_event(self, row):
        """Accept a timeline row: written now, or queued until the file
        can be written. False only when the queue is full."""
        if len(self.pending_events) >= MAX_PENDING_EVENTS and not self.flush_pending_events():
            return False
        self.pending_events.append(row)
        self.flush_pending_events()
        return True

    def _write_focus_event(self, application, window_title, allowed, status, timestamp=None):
        """Log a timeline row. True when it is part of the timeline: saved,
        or (while the file cannot be written) queued to be saved later,
        with `last_write_error` saying why it waits."""
        timestamp = timestamp or self._now()
        accepted = self._queue_focus_event({
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "focus_session_id": self.session_id,
            "title": self.title,
            "goal": self.goal,
            "application": application,
            "window_title": window_title,
            "allowed": "" if allowed is None else bool(allowed),
            "status": status,
            "remaining_seconds": self.remaining_seconds(),
        })
        if accepted:
            self.last_event_at = timestamp.replace(microsecond=0)
        return accepted
