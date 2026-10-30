"""
Focus Assistant accounting.

This module has no tkinter dependency, so the Flask API (api/focus.py),
the tests and the desktop GUI all share one implementation.

A focus session is a START row (normally followed by an END row) in
focus_sessions.csv plus a timeline in focus_events.csv. The assistant
writes a timeline row whenever the frontmost window or its state changes,
so a row lasts until the next row of the same session, and the last row
lasts until the session ends.

Every timeline row is in one of four states:

    allowed    an application inside the declared focus zone
    deviation  an application outside it
    idle       no keyboard or mouse input for IDLE_THRESHOLD_SECONDS
    neutral    the assistant's own windows, OS shell windows (Alt+Tab,
               the desktop, Spotlight, the lock screen) and markers such
               as the completion prompt

Idle and neutral time count as neither focus nor deviation, so zone
adherence is allowed / (allowed + deviation), and "focus time" is the
allowed (in-zone) time only.
"""

import csv
import io
import json
import math
import os
import sys

from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from focus import scoring  # noqa: E402


# ---------------------------------------------------------
# Files and formats
# ---------------------------------------------------------

SESSIONS_FILE_NAME = "focus_sessions.csv"
EVENTS_FILE_NAME = "focus_events.csv"
LIVE_FILE_NAME = "live.json"

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# /api/focus/live reports the heartbeat as stale after this long.
LIVE_STALE_SECONDS = 5

# A session without an END row still counts as running while the
# assistant's heartbeat for it is at most this old. The margin covers a
# GUI that is briefly blocked (e.g. a slow osascript call).
ACTIVE_SESSION_GRACE_SECONDS = 60

STATUS_IN_PROGRESS = "In progress"
STATUS_INTERRUPTED = "Interrupted"
STATUS_COMPLETED = "Completed"

# An idle stretch at least this long ends a deviation episode: leaving
# YouTube, an hour away and then Reddit are two deviations, while a
# short pause in the middle of one distraction is not a new one.
EPISODE_IDLE_BREAK_SECONDS = getattr(config, "SESSION_IDLE_BREAK_SECONDS", 300)

ALLOWED = "allowed"
DEVIATION = "deviation"
IDLE = "idle"
NEUTRAL = "neutral"


# ---------------------------------------------------------
# Window classification
# ---------------------------------------------------------

# Titles of the assistant's own windows.
INTERNAL_TITLE_MARKERS = ("focus assistant", "focus timer", "focus deviation")

# Tk dialogs the assistant opens (they belong to the Python process).
INTERNAL_DIALOG_TITLES = {
    "add application",
    "continue focus",
    "end focus session?",
    "focus session",
    "focus time completed",
    "invalid time",
    "unable to continue",
}

# OS shell windows: switching through them is neither focus nor a
# deviation (and never raises the deviation overlay).
NEUTRAL_APPLICATIONS = {
    # Windows
    "searchhost",
    "searchapp",
    "searchui",
    "shellexperiencehost",
    "startmenuexperiencehost",
    "lockapp",
    # macOS
    "loginwindow",
    "locked",
    "dock",
    "spotlight",
    "control center",
    "controlcenter",
    "notification center",
    "notificationcenter",
    "screensaverengine",
    # The deviation popup itself (focus/notify.py, `display alert`).
    "osascript",
}

# Windows Alt+Tab switcher and the desktop (both owned by explorer.exe).
NEUTRAL_WINDOW_TITLES = {"task switching", "program manager"}

EXPLORER_APPLICATIONS = {"file explorer", "explorer"}

# Other explorer.exe shell windows (Win+Tab).
EXPLORER_NEUTRAL_TITLES = {"task view"}

# On macOS, clicking the desktop makes Finder frontmost with no window.
DESKTOP_APPLICATIONS = {"finder"}

# Rows that are not part of the timeline (older versions logged allowed
# applications as a timeline row with an empty title).
NON_TIMELINE_STATUSES = {"application added to focus zone"}


def _clean(value):
    return str(value or "").strip().lower()


def normalize_application(value):
    """
    Lower-case application name without a trailing .exe.
    """

    name = _clean(value)

    if name.endswith(".exe"):
        name = name[:-4]

    return name


def is_unknown_application(application):
    return normalize_application(application) in ("", "unknown")


def is_python_application(application):
    return normalize_application(application).startswith("python")


def has_internal_title(window_title):
    title = _clean(window_title)
    return any(marker in title for marker in INTERNAL_TITLE_MARKERS)


def is_internal_window(application, window_title):
    """
    True for one of the assistant's own windows, judged by app and title.

    Used for rows logged before the assistant identified itself by
    process id, and as the fallback when no pid is available.
    """

    if not is_python_application(application):
        return False

    return (
        has_internal_title(window_title)
        or _clean(window_title) in INTERNAL_DIALOG_TITLES
    )


def is_neutral_window(application, window_title):
    app = normalize_application(application)
    title = _clean(window_title)

    if title in NEUTRAL_WINDOW_TITLES:
        return True

    if app in NEUTRAL_APPLICATIONS:
        return True

    if app in EXPLORER_APPLICATIONS:
        # The taskbar has no window title.
        return title in ("", "unknown") or title in EXPLORER_NEUTRAL_TITLES

    # The macOS desktop: Finder without a front window. (Without the
    # Accessibility permission no title is readable, so a Finder window
    # then also counts as neutral rather than as a deviation.)
    return app in DESKTOP_APPLICATIONS and title in ("", "unknown")


def event_state(row):
    """
    The accounting state of one focus_events.csv row, or None for rows
    that are not part of the timeline.
    """

    status = _clean(row.get("status"))

    if status in NON_TIMELINE_STATUSES:
        return None

    if status == "idle":
        return IDLE

    application = row.get("application")
    window_title = row.get("window_title")

    if (
        is_unknown_application(application)
        or is_internal_window(application, window_title)
        or is_neutral_window(application, window_title)
    ):
        return NEUTRAL

    allowed = _clean(row.get("allowed"))

    if allowed == "true":
        return ALLOWED

    if allowed == "false":
        return DEVIATION

    return NEUTRAL


# ---------------------------------------------------------
# Small helpers
# ---------------------------------------------------------

def focus_data_dir(data_dir=None):
    return Path(data_dir) if data_dir else Path(config.FOCUS_DATA_DIR)


def parse_dt(value):
    if isinstance(value, datetime):
        return value

    text = str(value or "").strip()

    if not text:
        return None

    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    # Heartbeats are naive local time; drop an offset if one was added.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)

    return parsed


def format_ts(value):
    return value.strftime(TIMESTAMP_FORMAT) if value else ""


def iso_ts(value):
    return value.isoformat(timespec="seconds") if value else None


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt_seconds(seconds):
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours}h {minutes}m"

    if minutes:
        return f"{minutes}m {secs}s"

    return f"{secs}s"


UTF8_BOM = b"\xef\xbb\xbf"


def decode_line(raw):
    """
    One line of bytes as text: UTF-8, else Windows-1252 (what Excel on
    Windows saves a CSV as), with bytes that have no character replaced.
    """

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _text_lines(data, strict=False):
    """
    The file's lines, split on \\n, \\r or \\r\\n and keeping the line
    ends (like reading with newline=""). strict=True raises
    UnicodeDecodeError instead of decoding a line any other way.
    """

    if data.startswith(UTF8_BOM):
        data = data[len(UTF8_BOM):]

    try:
        return list(io.StringIO(data.decode("utf-8"), newline=""))
    except UnicodeDecodeError:
        if strict:
            raise

    # Only lines that are not UTF-8 fall back; a 0x0A byte is never
    # part of a multi-byte UTF-8 character, so splitting first is safe.
    return [decode_line(raw) for raw in data.splitlines(keepends=True)]


def _csv_records(lines, strict=False):
    """
    The non-empty CSV records in `lines`, as lists.

    A record that cannot be parsed (a csv.Error such as an oversized
    field, or a quote that is never closed and swallows the lines after
    it) is dropped from its first line, and parsing carries on with the
    next line, so one damaged row never hides the rest of the file.
    strict=True raises ValueError instead.
    """

    records = []
    width = None
    position = 0

    while position < len(lines):
        reader = csv.reader(lines[position:])
        consumed = 0
        resume = None
        problem = ""

        try:
            for record in reader:
                first = position + consumed
                spanned = reader.line_num - consumed
                consumed = reader.line_num

                if not record:
                    continue

                if width is None:
                    width = len(record)
                elif spanned > 1 and len(record) < width:
                    # A quoted field ran over several lines and the row
                    # still came up short: an unterminated quote.
                    resume = first + 1
                    problem = "unterminated quoted field"
                    break

                records.append(record)
        except csv.Error as error:
            resume = position + consumed + 1
            problem = str(error)

        if resume is None:
            break

        if strict:
            raise ValueError(f"line {resume} cannot be read ({problem})")

        position = resume

    return records


def _row_dict(header, record):
    # The same dict csv.DictReader builds (restkey and restval None).
    row = dict(zip(header, record))

    if len(record) > len(header):
        row[None] = record[len(header):]
    else:
        for key in header[len(record):]:
            row[key] = None

    return row


def read_csv(path, strict=False):
    """
    All rows of a CSV as dicts; [] when the file is missing.

    By default a damaged file still gives every row that can be read: a
    line that is not UTF-8 is decoded as Windows-1252, and a row that
    cannot be parsed is skipped. strict=True instead raises (OSError,
    ValueError) on anything it would otherwise repair or skip; use it
    before rewriting a file, so no row is ever dropped from it.
    """

    path = Path(path)

    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []

    records = _csv_records(_text_lines(data, strict), strict)

    if not records:
        return []

    header = records[0]
    return [_row_dict(header, record) for record in records[1:]]


def _overlap_seconds(start, end, window_start, window_end):
    if not start or not end:
        return 0.0

    begin = max(start, window_start)
    finish = min(end, window_end)

    return max(0.0, (finish - begin).total_seconds())


# ---------------------------------------------------------
# Live heartbeat (data/focus/live.json)
# ---------------------------------------------------------

def live_file(data_dir=None):
    return focus_data_dir(data_dir) / LIVE_FILE_NAME


def write_live(payload, data_dir=None):
    """
    Atomically replace live.json. Returns False instead of raising.
    """

    path = live_file(data_dir)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, path)
    except (OSError, TypeError, ValueError):
        try:
            temp.unlink()
        except OSError:
            pass
        return False

    return True


def read_live_file(data_dir=None):
    """
    The raw heartbeat dict, or None when missing or unreadable.
    """

    try:
        payload = json.loads(live_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return payload if isinstance(payload, dict) else None


def heartbeat_age(heartbeat, now=None):
    """
    Seconds since the heartbeat was written, or None if it has no time.
    """

    if not heartbeat:
        return None

    updated = parse_dt(heartbeat.get("updated_at"))

    if updated is None:
        return None

    return ((now or datetime.now()) - updated).total_seconds()


def heartbeat_process_alive(heartbeat):
    """
    False only when the process that wrote the heartbeat is certainly
    gone (it crashed or was killed); True when it lives, is this process,
    or cannot be checked.
    """

    try:
        pid = int((heartbeat or {}).get("pid"))
    except (TypeError, ValueError):
        return True

    if pid <= 0 or pid == os.getpid():
        return True

    try:
        import psutil
    except ImportError:
        return True

    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001 - unsure means alive
        return True


def running_session_id(heartbeat, now=None, max_age=ACTIVE_SESSION_GRACE_SECONDS):
    """
    The session id a recent heartbeat reports as running, else None
    (also when the process that wrote it has died).
    """

    age = heartbeat_age(heartbeat, now)

    if age is None or abs(age) > max_age:
        return None

    if not heartbeat.get("running"):
        return None

    if not heartbeat_process_alive(heartbeat):
        return None

    return heartbeat.get("session_id") or None


LIVE_SOURCES = ("dashboard", "assistant", "member")
LIVE_STATES = ("allowed", "deviation", "idle", "neutral", "self", "unknown")

# The live payload lists at most this many deviations, newest first.
MAX_RECENT_DEVIATIONS = 10


def empty_live(status="Focus Assistant not running", stale=True, updated_at=None):
    """
    The /api/focus/live payload when no session runs. Every key of the
    live contract is present (see focus/engine.py for a running one).
    """

    return {
        "running": False,
        "stale": stale,
        "session_id": None,
        "source": None,
        "title": None,
        "goal": None,
        "started_at": None,
        "planned_s": None,
        "remaining_s": None,
        "allowed_apps": [],
        "current_app": None,
        "current_window": None,
        "allowed": None,
        "state": None,
        "status": status,
        "score": None,
        "deviations": 0,
        "deviation_seconds": 0,
        "last_deviation": None,
        "recent_deviations": [],
        "updated_at": updated_at,
        "pid": None,
    }


def _optional_int(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return int(number) if math.isfinite(number) else None


def _optional_str(value):
    return value if isinstance(value, str) else None


def _str_list(value):
    if not isinstance(value, list):
        return []

    return [item for item in value if isinstance(item, str)]


def _last_deviation(value):
    if not isinstance(value, dict) or not isinstance(value.get("app"), str):
        return None

    return {
        "app": value["app"],
        "window_title": _optional_str(value.get("window_title")) or "",
        "at": _optional_str(value.get("at")),
        "penalty": _optional_int(value.get("penalty")) or 0,
    }


def _recent_deviations(value):
    if not isinstance(value, list):
        return []

    items = []

    for item in value:
        if isinstance(item, dict) and isinstance(item.get("app"), str):
            items.append({
                "app": item["app"],
                "at": _optional_str(item.get("at")),
                "seconds": max(0, _optional_int(item.get("seconds")) or 0),
            })

    return items[:MAX_RECENT_DEVIATIONS]


def live_status(data_dir=None, now=None, stale_after=LIVE_STALE_SECONDS):
    """
    The /api/focus/live payload, from the heartbeat in live.json.

    Heartbeats written before the focus engine existed (or by the Tk
    assistant) lack the newer fields; those get their defaults (score
    None, no deviations).
    """

    heartbeat = read_live_file(data_dir)
    age = heartbeat_age(heartbeat, now)
    stale = age is None or abs(age) > stale_after

    result = empty_live(
        stale=stale,
        updated_at=heartbeat.get("updated_at") if heartbeat else None,
    )

    if stale:
        return result

    running = bool(heartbeat.get("running"))
    result["pid"] = _optional_int(heartbeat.get("pid"))

    if running and not heartbeat_process_alive(heartbeat):
        # The session's process ended without stopping it: nothing runs
        # (and nothing can be stopped) until the next start records it
        # as interrupted.
        result["status"] = "Focus session interrupted"
        return result

    result["running"] = running
    result["status"] = heartbeat.get("status") or (
        "Focus session running" if running else "No active session"
    )

    if running:
        allowed = heartbeat.get("allowed")
        remaining = heartbeat.get("remaining_s")
        source = heartbeat.get("source")
        state = heartbeat.get("state")
        score = _optional_int(heartbeat.get("score"))

        result.update({
            "session_id": _optional_str(heartbeat.get("session_id")),
            "source": source if source in LIVE_SOURCES else None,
            "title": heartbeat.get("title"),
            "goal": heartbeat.get("goal"),
            "started_at": _optional_str(heartbeat.get("started_at")),
            "planned_s": _optional_int(heartbeat.get("planned_s")),
            "remaining_s": None if remaining is None else safe_int(remaining),
            "allowed_apps": _str_list(heartbeat.get("allowed_apps")),
            "current_app": heartbeat.get("current_app"),
            "current_window": _optional_str(heartbeat.get("current_window")),
            "allowed": allowed if isinstance(allowed, bool) else None,
            "state": state if state in LIVE_STATES else None,
            "score": score,
            "deviations": max(0, _optional_int(heartbeat.get("deviations")) or 0),
            "deviation_seconds": max(
                0, _optional_int(heartbeat.get("deviation_seconds")) or 0
            ),
            "last_deviation": _last_deviation(heartbeat.get("last_deviation")),
            "recent_deviations": _recent_deviations(heartbeat.get("recent_deviations")),
        })

    return result


# ---------------------------------------------------------
# Session records
# ---------------------------------------------------------

def index_sessions(session_rows):
    """
    Split focus_sessions.csv rows into (starts, ends, latest) by session id.

    starts and ends keep the first START / END row of each session, except
    that an 'Interrupted' END gives way to a later END the session wrote
    itself (start-up recovery by a second assistant could close a session
    that was in fact still running); latest keeps its newest row of any
    type (EXTEND and ALLOW rows carry the current planned duration and
    allowed apps).
    """

    starts, ends, latest = {}, {}, {}
    interrupted = _clean(STATUS_INTERRUPTED)

    for row in session_rows:
        sid = (row.get("session_id") or "").strip()

        if not sid:
            continue

        latest[sid] = row
        event_type = (row.get("event_type") or "").strip().upper()

        if event_type == "START":
            starts.setdefault(sid, row)
        elif event_type == "END":
            current = ends.get(sid)

            if current is None or (
                _clean(current.get("completion_status")) == interrupted
                and _clean(row.get("completion_status")) != interrupted
            ):
                ends[sid] = row

    return starts, ends, latest


def _timelines(event_rows):
    grouped = {}

    for row in event_rows:
        sid = (row.get("focus_session_id") or "").strip()
        state = event_state(row)
        timestamp = parse_dt(row.get("timestamp"))

        if not sid or state is None or timestamp is None:
            continue

        application = (row.get("application") or "").strip() or "Unknown"
        grouped.setdefault(sid, []).append((timestamp, state, application))

    for timeline in grouped.values():
        # Stable: rows logged in the same second keep their file order.
        timeline.sort(key=lambda item: item[0])

    return grouped


def account_timeline(timeline, start_dt, end_dt, count_final_deviation=False):
    """
    Seconds per state, deviation episodes, per-app deviation seconds,
    application switches and the longest uninterrupted in-zone run for
    one session's [(timestamp, state, application), ...] timeline.

    This is the one deviation-episode rule: recorded sessions and the live
    engine (focus/engine.py, through deviation_starts_episode) both use it.
    "episode_list" holds {"at", "app", "seconds"} per episode, oldest first;
    "in_excursion" says whether the last counted row left the user outside
    the zone (a deviation that a new deviation row would continue).

    count_final_deviation: a deviation row logged at end_dt itself (the
    switch the live engine has just seen and announced, or one in the
    second a session was stopped) is an episode, of 0 s so far, so the
    switch penalty shows at once. Off for interrupted sessions, whose
    end is their last row.
    """

    seconds = {ALLOWED: 0.0, DEVIATION: 0.0, IDLE: 0.0, NEUTRAL: 0.0}
    deviation_apps = {}
    allowed_spans = []
    episode_list = []
    episodes = 0
    switches = 0
    longest_run = 0.0
    current_run = 0.0
    idle_stretch = 0.0
    previous_state = None
    previous_app = None

    rows = [item for item in timeline if item[0] <= end_dt]

    for index, (t0, state, application) in enumerate(rows):
        t1 = rows[index + 1][0] if index + 1 < len(rows) else end_dt
        duration = _overlap_seconds(t0, t1, start_dt, end_dt)
        seconds[state] += duration

        final_switch = (
            count_final_deviation
            and state == DEVIATION
            and index == len(rows) - 1
            and start_dt <= t0 == end_dt
        )

        if duration <= 0 and not final_switch:
            # A row that lasted no time inside the session (e.g. the last
            # row of an interrupted session, which ends at that row) is
            # neither a deviation episode nor an application switch.
            continue

        if state == IDLE:
            idle_stretch += duration

            # A long break ends the current excursion.
            if idle_stretch >= EPISODE_IDLE_BREAK_SECONDS:
                previous_state = None

        if state == DEVIATION:
            deviation_apps[application] = (
                deviation_apps.get(application, 0.0) + duration
            )

            # One continuous excursion is one deviation, however many
            # windows or tabs it passes through.
            if previous_state != DEVIATION:
                episodes += 1
                episode_list.append({
                    "at": max(t0, start_dt),
                    "app": application,
                    "seconds": 0.0,
                })

            episode_list[-1]["seconds"] += duration

        if state in (ALLOWED, DEVIATION):
            idle_stretch = 0.0

            if previous_app is not None and application != previous_app:
                switches += 1

            previous_app = application
            previous_state = state

        if state == ALLOWED:
            allowed_spans.append((max(t0, start_dt), min(t1, end_dt)))
            current_run += duration
            longest_run = max(longest_run, current_run)
        elif state in (DEVIATION, IDLE):
            current_run = 0.0

    return {
        "seconds": seconds,
        "deviation_apps": deviation_apps,
        "allowed_spans": allowed_spans,
        "episodes": episodes,
        "episode_list": episode_list,
        "in_excursion": previous_state == DEVIATION,
        "switches": switches,
        "longest_run": longest_run,
    }


def session_timeline(event_rows, session_id):
    """
    One session's [(timestamp, state, application), ...] timeline from
    focus_events.csv rows, in the order account_timeline reads it.
    """

    return _timelines(event_rows).get(session_id, [])


def deviation_starts_episode(timeline, start_dt):
    """
    True when the timeline's last row is a deviation that starts a new
    deviation episode (rather than continuing the current one), judged
    by account_timeline over the rows before it.
    """

    if not timeline or timeline[-1][1] != DEVIATION:
        return False

    at = timeline[-1][0]
    before = account_timeline(timeline[:-1], start_dt, max(at, start_dt))

    return not before["in_excursion"]


def build_records(session_rows, event_rows, now=None, active_session_id=None,
                  heartbeat=None):
    """
    One record per session, newest first.

    A session without an END row is "In progress" (ending now) when it is
    active_session_id; otherwise it was interrupted and ends at its last
    sign of life (last timeline row, or the heartbeat if that was for it).
    """

    now = now or datetime.now()
    starts, ends, latest = index_sessions(session_rows)
    timelines = _timelines(event_rows)
    heartbeat_time = parse_dt(heartbeat.get("updated_at")) if heartbeat else None
    heartbeat_sid = heartbeat.get("session_id") if heartbeat else None

    records = []

    for sid, start in starts.items():
        start_dt = parse_dt(start.get("timestamp"))

        if start_dt is None:
            continue

        timeline = timelines.get(sid, [])
        end_row = ends.get(sid)
        running = False

        if end_row is not None:
            end_dt = parse_dt(end_row.get("timestamp")) or start_dt
            status = (end_row.get("completion_status") or "").strip() or "Stopped"
        elif sid == active_session_id:
            end_dt = max(now, start_dt)
            status = STATUS_IN_PROGRESS
            running = True
        else:
            candidates = [start_dt]

            if timeline:
                candidates.append(timeline[-1][0])

            if heartbeat_time and heartbeat_sid == sid:
                candidates.append(heartbeat_time)

            end_dt = max(candidates)
            status = STATUS_INTERRUPTED

        end_dt = max(end_dt, start_dt)
        latest_row = latest.get(sid, start)
        planned = safe_int(
            latest_row.get("planned_duration_minutes"),
            safe_int(start.get("planned_duration_minutes"), 0),
        )
        allowed_apps = latest_row.get("allowed_apps") or start.get("allowed_apps") or ""

        result = account_timeline(
            timeline, start_dt, end_dt,
            count_final_deviation=status != STATUS_INTERRUPTED,
        )
        allowed_seconds = result["seconds"][ALLOWED]
        deviation_seconds = result["seconds"][DEVIATION]
        tracked = allowed_seconds + deviation_seconds
        deviation_apps = {
            app: int(round(value))
            for app, value in result["deviation_apps"].items()
            if value > 0
        }
        top_app = max(deviation_apps, key=deviation_apps.get) if deviation_apps else None
        deviation_seconds_int = int(round(deviation_seconds))

        records.append({
            "id": sid,
            "title": start.get("title") or start.get("goal") or "Untitled session",
            "goal": start.get("goal") or "",
            "apps": [app.strip() for app in allowed_apps.split("|") if app.strip()],
            "start": start_dt,
            "end": end_dt,
            "running": running,
            "status": status,
            "planned": planned,
            "actual_seconds": int((end_dt - start_dt).total_seconds()),
            "allowed_seconds": int(round(allowed_seconds)),
            # (begin, end) of each in-zone stretch, to split focus time
            # by day.
            "allowed_spans": result["allowed_spans"],
            "deviation_seconds": deviation_seconds_int,
            "idle_seconds": int(round(result["seconds"][IDLE])),
            "neutral_seconds": int(round(result["seconds"][NEUTRAL])),
            "adherence": (allowed_seconds / tracked * 100) if tracked > 0 else None,
            "deviations": result["episodes"],
            # {"at", "app", "seconds"} per deviation episode, oldest first.
            "deviation_episodes": result["episode_list"],
            "score": scoring.session_score(result["episodes"], deviation_seconds_int),
            "deviation_apps": deviation_apps,
            "switches": result["switches"],
            "longest_focus_seconds": int(round(result["longest_run"])),
            "top_deviation_app": top_app,
        })

    records.sort(key=lambda record: record["start"], reverse=True)
    return records


def session_records(data_dir=None, now=None, active_session_id=None):
    """
    Records for every session stored in data_dir, newest first.

    Without active_session_id, the running session is taken from a recent
    heartbeat in live.json (the API's view of a session the GUI owns).
    """

    data_dir = focus_data_dir(data_dir)
    now = now or datetime.now()
    heartbeat = read_live_file(data_dir)

    if active_session_id is None:
        active_session_id = running_session_id(heartbeat, now)

    return build_records(
        read_csv(data_dir / SESSIONS_FILE_NAME),
        read_csv(data_dir / EVENTS_FILE_NAME),
        now=now,
        active_session_id=active_session_id,
        heartbeat=heartbeat,
    )


def recent_records(records, limit=8):
    """
    The newest `limit` sessions (records are already newest first).
    """

    return list(records[:limit])


# ---------------------------------------------------------
# Summaries
# ---------------------------------------------------------

def deviation_sources(records):
    totals = {}

    for record in records:
        for app, seconds in record["deviation_apps"].items():
            totals[app] = totals.get(app, 0) + seconds

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [{"app": app, "seconds": int(seconds)} for app, seconds in ranked if seconds > 0]


def adherence_pct(records):
    """
    Time-weighted zone adherence over finished sessions, or None.
    """

    finished = [record for record in records if not record["running"]]
    allowed = sum(record["allowed_seconds"] for record in finished)
    tracked = allowed + sum(record["deviation_seconds"] for record in finished)

    return round(allowed / tracked * 100, 1) if tracked > 0 else None


def _midnight(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def focus_seconds_between(records, window_start, window_end):
    """
    In-zone (allowed) seconds that fall inside [window_start, window_end].
    """

    return int(round(sum(
        _overlap_seconds(begin, end, window_start, window_end)
        for record in records
        for begin, end in record.get("allowed_spans", ())
    )))


def summarize(records, now=None):
    """
    The /api/focus/summary payload, plus session_seconds_* (wall-clock
    time in sessions) for the desktop GUI.

    focus_seconds_* is in-zone time only: idle, neutral and deviation
    time are not focus. Today means since local midnight, so a session
    that crosses midnight is split.
    """

    now = now or datetime.now()
    midnight = _midnight(now)

    return {
        "sessions": len(records),
        "completed": sum(record["status"] == STATUS_COMPLETED for record in records),
        "adherence_pct": adherence_pct(records),
        "focus_seconds_today": focus_seconds_between(records, midnight, now),
        "focus_seconds_total": int(sum(record["allowed_seconds"] for record in records)),
        "session_seconds_today": int(sum(
            _overlap_seconds(record["start"], record["end"], midnight, now)
            for record in records
        )),
        "session_seconds_total": int(sum(record["actual_seconds"] for record in records)),
        "deviation_count": sum(record["deviations"] for record in records),
        "deviation_sources": deviation_sources(records),
        "focus_score": scoring.overall_score(records, now),
    }


def today_summary(records, now=None):
    """
    Sessions that ran today (since local midnight): count, in-zone focus
    time since midnight (the same rule as summarize), adherence and
    deviations.
    """

    now = now or datetime.now()
    midnight = _midnight(now)
    today = [record for record in records if record["end"] >= midnight]

    return {
        "sessions": len(today),
        "focus_seconds": focus_seconds_between(today, midnight, now),
        "adherence_pct": adherence_pct(today),
        "deviations": sum(record["deviations"] for record in today),
    }


def session_json(record):
    """
    One entry of the /api/focus/sessions payload.
    """

    adherence = record["adherence"]

    return {
        "id": record["id"],
        "title": record["title"],
        "goal": record["goal"],
        "start": iso_ts(record["start"]),
        "end": None if record["running"] else iso_ts(record["end"]),
        "status": record["status"],
        "planned_min": record["planned"],
        "actual_s": record["actual_seconds"],
        "adherence_pct": None if adherence is None else round(adherence, 1),
        "deviations": record["deviations"],
        "switches": record["switches"],
        "longest_focus_s": record["longest_focus_seconds"],
        "top_deviation_app": record["top_deviation_app"],
        "score": record.get("score"),
    }
