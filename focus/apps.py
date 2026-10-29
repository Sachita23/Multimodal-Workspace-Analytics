"""
Applications to offer when a focus session is set up.

    running_applications()  the GUI applications open right now
    recent_applications()   the applications used most in the last days,
                            from the collector's raw activity log

Names are normalised like the collector's (collector.clean_application_name),
so they match what the focus engine sees in the frontmost window. Both
functions are best effort and return [] rather than raising.
"""

import csv
import io
import subprocess
import sys
import threading
import time

from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from focus import analytics  # noqa: E402


COMMAND_TIMEOUT_SECONDS = 4

# Only the end of the raw log is read: at one row every few seconds a
# week is a few tens of megabytes.
RECENT_TAIL_BYTES = 32 * 1024 * 1024

# recent_applications() reuses its answer for this long.
RECENT_CACHE_SECONDS = 60

# Never worth offering as a focus application. "osascript" is the helper
# that lists the open apps on macOS: it is open while it asks.
EXCLUDED_APPLICATIONS = (
    {"unknown", "locked", "osascript"} | set(analytics.NEUTRAL_APPLICATIONS)
)

MAC_RUNNING_SCRIPT = [
    'tell application "System Events" to set appNames to name of every '
    'application process whose background only is false',
    "set AppleScript's text item delimiters to linefeed",
    "return appNames as text",
]

_recent_cache = {}
_recent_lock = threading.Lock()


def clean_name(name):
    """
    The collector's display name for a process or application name.
    """

    try:
        from collector.collector import clean_application_name
    except Exception:  # the collector's platform libraries are missing
        text = str(name or "").strip()
        return text[:-4] if text.lower().endswith(".exe") else (text or "Unknown")

    return clean_application_name(str(name or ""))


def _normalised(names):
    """
    Cleaned, de-duplicated (case-insensitively) and sorted names.
    """

    seen = {}

    for name in names:
        cleaned = clean_name(name).strip()

        if not cleaned or analytics.normalize_application(cleaned) in EXCLUDED_APPLICATIONS:
            continue

        seen.setdefault(cleaned.lower(), cleaned)

    return sorted(seen.values(), key=str.lower)


def _run(argv):
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    return result.stdout if result.returncode == 0 else ""


def _running_mac():
    argv = ["osascript"]

    for line in MAC_RUNNING_SCRIPT:
        argv += ["-e", line]

    return [line for line in _run(argv).splitlines() if line.strip()]


def _running_windows():
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return []

    pids = set()

    def collect(hwnd, _extra):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).strip():
                pids.add(win32process.GetWindowThreadProcessId(hwnd)[1])
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    names = []

    for pid in pids:
        try:
            names.append(psutil.Process(pid).name())
        except Exception:
            continue

    return names


def _running_linux():
    try:
        import psutil
    except ImportError:
        return []

    pids = set()
    output = _run(["wmctrl", "-lp"])

    if output:
        for line in output.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 3 and parts[2].isdigit() and parts[2] != "0":
                pids.add(int(parts[2]))
    else:
        windows = _run(["xdotool", "search", "--onlyvisible", "--name", "."]).split()
        for window in windows[:60]:
            pid = _run(["xdotool", "getwindowpid", window]).strip()
            if pid.isdigit():
                pids.add(int(pid))

    names = []

    for pid in pids:
        try:
            names.append(psutil.Process(pid).name())
        except Exception:
            continue

    return names


def running_applications():
    """
    Sorted names of the GUI applications that are open; [] on failure.
    """

    try:
        if sys.platform == "darwin":
            names = _running_mac()
        elif sys.platform.startswith("win"):
            names = _running_windows()
        elif sys.platform.startswith("linux"):
            names = _running_linux()
        else:
            names = []
    except Exception:
        return []

    return _normalised(names)


# ---------------------------------------------------------
# Recently used applications
# ---------------------------------------------------------

def _tail_rows(raw_file, max_bytes=RECENT_TAIL_BYTES):
    """
    The rows (dicts) in the last `max_bytes` of the raw activity log.
    """

    with open(raw_file, "rb") as file:
        header_line = file.readline()
        file.seek(0, 2)
        size = file.tell()
        start = max(len(header_line), size - max_bytes)
        file.seek(start)
        data = file.read()

    if start > len(header_line):
        # Drop the partial line the cut landed in.
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""

    header = next(csv.reader([analytics.decode_line(header_line).lstrip("﻿")]), [])
    text = data.decode("utf-8", errors="replace")

    for record in csv.reader(io.StringIO(text, newline="")):
        if record:
            yield dict(zip(header, record))


def _recent_seconds(rows, since):
    """
    Active seconds per application since `since`; each row lasts until
    the next one, at most MAX_OBSERVATION_GAP_SECONDS.
    """

    max_gap = getattr(config, "MAX_OBSERVATION_GAP_SECONDS", 15)
    totals = {}
    previous = None

    for row in rows:
        timestamp = analytics.parse_dt(row.get("timestamp"))

        if timestamp is None:
            continue

        if previous is not None:
            before, application, active = previous
            gap = (timestamp - before).total_seconds()

            if active and 0 < gap and before >= since:
                totals[application] = totals.get(application, 0.0) + min(gap, max_gap)

        status = str(row.get("activity_status") or "").strip().lower()
        previous = (timestamp, (row.get("application") or "").strip(), status == "active")

    return totals


def recent_applications(days=7, limit=15, raw_file=None, now=None):
    """
    Up to `limit` applications with the most active time in the last
    `days` days of the raw activity log, most used first; [] when there
    is no log.
    """

    raw_file = Path(raw_file or config.RAW_DATA_FILE)
    use_cache = now is None
    now = now or datetime.now()

    if not raw_file.is_file():
        return []

    key = (str(raw_file), days, limit)

    with _recent_lock:
        cached = _recent_cache.get(key)

        if use_cache and cached and time.monotonic() - cached[0] < RECENT_CACHE_SECONDS:
            return list(cached[1])

    try:
        totals = _recent_seconds(_tail_rows(raw_file), now - timedelta(days=days))
    except (OSError, csv.Error, ValueError):
        return []

    merged = {}

    for application, seconds in totals.items():
        cleaned = clean_name(application)

        if analytics.normalize_application(cleaned) in EXCLUDED_APPLICATIONS:
            continue

        merged[cleaned] = merged.get(cleaned, 0.0) + seconds

    ranked = sorted(merged.items(), key=lambda item: (-item[1], item[0].lower()))
    result = [name for name, seconds in ranked if seconds > 0][:limit]

    if use_cache:
        with _recent_lock:
            _recent_cache[key] = (time.monotonic(), list(result))

    return result
