import sys
import time
import csv
import os
import ctypes
import subprocess

from pathlib import Path
from datetime import datetime

# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# THIRD-PARTY LIBRARIES
# ============================================================

import psutil

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:
    import win32gui
    import win32process


# ============================================================
# PROJECT CONFIGURATION
# ============================================================

from config import (
    RAW_DATA_FILE,
    COLLECTION_INTERVAL,
    IDLE_THRESHOLD_SECONDS
)

try:
    from config import RAW_COLUMNS
except ImportError:
    RAW_COLUMNS = [
        "timestamp",
        "application",
        "window_title",
        "activity_status",
        "idle_seconds",
        "cpu_percent",
        "memory_percent",
        "battery_percent",
        "charging"
    ]


# The lock screen and screen saver are reported as this "app",
# always with status Idle.
LOCKED_APPLICATION = "Locked"


# ============================================================
# WINDOWS API STRUCTURES
# ============================================================

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint)
    ]


# ============================================================
# ACTIVE APPLICATION
# ============================================================

def get_active_application():
    """
    Get the currently active application and window title.

    Returns:
        application_name, window_title
    """

    application_name, window_title, _ = get_active_process()

    return application_name, window_title


def get_active_process():
    """
    Get the currently active application, its window title and
    the id of the process that owns the window.

    Returns:
        application_name, window_title, process_id (None if unknown)
    """

    try:
        if IS_WINDOWS:
            application_name, window_title, process_id = (
                _active_window_windows()
            )
        elif IS_MAC:
            application_name, window_title, process_id = (
                _active_window_mac()
            )
        else:
            application_name, window_title, process_id = (
                _active_window_linux()
            )

    except Exception:
        return "Unknown", "Unknown", None

    application_name = clean_application_name(
        clean_text(application_name)
    )

    window_title = clean_text(window_title)

    if not window_title:
        window_title = "Unknown"

    return application_name, window_title, process_id


def clean_text(value):
    """
    Make a string valid Unicode. Windows can hand back a window
    title cut in the middle of an emoji (a lone UTF-16 surrogate),
    which cannot be written as UTF-8 to the raw CSV or stored by
    the team server; each such half becomes U+FFFD.
    """

    if not isinstance(value, str):
        return value

    return value.encode("utf-16-le", "surrogatepass").decode(
        "utf-16-le", "replace"
    )


def _active_window_windows():

    hwnd = win32gui.GetForegroundWindow()

    if not hwnd:
        return "Unknown", "Unknown", None

    window_title = win32gui.GetWindowText(hwnd)

    process_id = None

    try:
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)

        application_name = psutil.Process(process_id).name()

    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess
    ):
        application_name = "Unknown"

    return application_name, window_title, process_id


def _run(command, timeout=2):
    """
    Run a helper command and return its stripped stdout,
    or "" if it is missing, fails or times out.
    """

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    if result.returncode != 0:
        return ""

    return result.stdout.strip()


# One System Events query for the frontmost app's name, process id
# and window title. Reading the title needs the Accessibility
# permission; without it the title comes back empty.
MAC_FRONT_WINDOW_SCRIPT = [
    'tell application "System Events"',
    'set frontApp to first application process whose frontmost is true',
    'set appName to name of frontApp',
    'set appId to unix id of frontApp',
    'set windowTitle to ""',
    'try',
    'set windowTitle to name of front window of frontApp',
    'end try',
    'end tell',
    'if windowTitle is missing value then set windowTitle to ""',
    'return appName & tab & appId & tab & windowTitle',
]

MAC_PERMISSION_HINTS = {
    "app": (
        "Note: could not read the frontmost app. Allow the app running "
        "this collector (e.g. Terminal) to control System Events in "
        "System Settings > Privacy & Security > Automation."
    ),
    "title": (
        "Note: window titles are not available. If they keep showing "
        "as Unknown, allow the app running this collector (e.g. "
        "Terminal) in System Settings > Privacy & Security > "
        "Accessibility."
    ),
}

_hints_shown = set()


def _show_hint_once(kind):

    if kind in _hints_shown:
        return

    _hints_shown.add(kind)

    print(MAC_PERMISSION_HINTS[kind], flush=True)


def _active_window_mac():
    """
    Window titles need the Accessibility permission for the
    terminal running the collector (System Settings > Privacy &
    Security > Accessibility). Without it only the app name is
    available.
    """

    command = ["osascript"]

    for line in MAC_FRONT_WINDOW_SCRIPT:
        command += ["-e", line]

    output = _run(command)

    if not output:
        _show_hint_once("app")
        return "Unknown", "", None

    return _parse_mac_front_window(output)


def _parse_mac_front_window(output):
    """
    Split osascript's "name<TAB>pid<TAB>title" answer.
    """

    parts = output.split("\t", 2)

    while len(parts) < 3:
        parts.append("")

    application_name, process_id, window_title = parts

    try:
        process_id = int(process_id)
    except ValueError:
        process_id = None

    window_title = window_title.strip()

    if not window_title:
        _show_hint_once("title")

    return application_name.strip() or "Unknown", window_title, process_id


def _active_window_linux():
    """
    X11 only; needs xdotool. Wayland sessions report Unknown.
    """

    window_id = _run(["xdotool", "getactivewindow"])

    if not window_id:
        return "Unknown", "Unknown", None

    window_title = _run(["xdotool", "getwindowname", window_id])

    try:
        process_id = int(_run(["xdotool", "getwindowpid", window_id]))
    except ValueError:
        return "Unknown", window_title, None

    try:
        application_name = psutil.Process(process_id).name()
    except psutil.Error:
        application_name = "Unknown"

    return application_name, window_title, process_id


# ============================================================
# APPLICATION NAME CLEANING
# ============================================================

# Keys are lower-case process names. Windows executables first,
# then the names macOS (and Linux) report for the same apps, so a
# mixed team sees one name per app.
APPLICATION_NAMES = {
    "code.exe": "Visual Studio Code",
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "explorer.exe": "File Explorer",
    "winword.exe": "Microsoft Word",
    "excel.exe": "Microsoft Excel",
    "powerpnt.exe": "Microsoft PowerPoint",
    "outlook.exe": "Microsoft Outlook",
    "olk.exe": "Microsoft Outlook",
    "onenote.exe": "Microsoft OneNote",
    "msaccess.exe": "Microsoft Access",
    "mspub.exe": "Microsoft Publisher",
    "visio.exe": "Microsoft Visio",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "notepad.exe": "Notepad",
    "devenv.exe": "Visual Studio",
    "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "whatsapp.exe": "WhatsApp",
    "telegram.exe": "Telegram",
    "zoom.exe": "Zoom",
    "obs64.exe": "OBS Studio",
    "taskmgr.exe": "Task Manager",
    "powershell.exe": "PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "cmd.exe": "Command Prompt",

    # macOS / Linux
    "code": "Visual Studio Code",
    "zoom.us": "Zoom",
    "iterm2": "iTerm2",
    "firefox": "Mozilla Firefox",
}

# Lock screen and screen saver processes.
LOCKED_PROCESSES = {
    "loginwindow",
    "screensaverengine",
    "lockapp.exe",
}


def clean_application_name(application_name):
    """
    Convert executable names into readable application names.

    Known apps get one name on every OS. Otherwise ".exe" is
    dropped and all-lower-case names are title-cased, so the OS
    casing of names like "WhatsApp" or "Microsoft PowerPoint" is
    kept. Windows executables shipped in capitals (ONENOTE.EXE,
    WINPROJ.EXE) are title-cased too, as they always were; a
    mixed-case one such as SnippingTool.exe keeps its casing.
    """

    if not application_name or not application_name.strip():
        return "Unknown"

    name = application_name.strip()

    key = name.lower()

    if key in LOCKED_PROCESSES:
        return LOCKED_APPLICATION

    if key in APPLICATION_NAMES:
        return APPLICATION_NAMES[key]

    # Remove .exe from unknown applications
    is_executable = key.endswith(".exe")

    if is_executable:
        name = name[:-4]

    if name == name.lower() or (is_executable and name == name.upper()):
        name = name.title()

    return name


# ============================================================
# SYSTEM METRICS
# ============================================================

def get_system_metrics():
    """
    Collect CPU, memory and battery information.

    Returns:
        cpu_percent,
        memory_percent,
        battery_percent,
        charging
    """

    try:
        cpu_percent = psutil.cpu_percent(
            interval=None
        )
    except Exception:
        cpu_percent = 0.0

    try:
        memory_percent = psutil.virtual_memory().percent
    except Exception:
        memory_percent = 0.0

    try:
        battery = psutil.sensors_battery()

        if battery is None:
            battery_percent = 100.0
            charging = False
        else:
            battery_percent = float(
                battery.percent
            )

            charging = bool(
                battery.power_plugged
            )

    except Exception:
        battery_percent = 0.0
        charging = False

    return (
        cpu_percent,
        memory_percent,
        battery_percent,
        charging
    )


# ============================================================
# IDLE TIME
# ============================================================

def get_idle_time():
    """
    Get the number of seconds since the user's
    last keyboard or mouse input.
    """

    try:
        if IS_WINDOWS:
            return _idle_time_windows()
        if IS_MAC:
            return _idle_time_mac()
        return _idle_time_linux()

    except Exception:
        return 0.0


def _elapsed_ticks_ms(current_tick, last_input_tick):
    """
    Milliseconds between two GetTickCount values. Both are 32-bit
    counters that wrap every ~49.7 days, so the difference is taken
    modulo 2**32 (it stays correct across the wrap and whether or
    not a value arrived sign-extended).

    A last input stamped slightly after the tick count was read
    (input injected with its own timestamp, e.g. by remote-desktop
    tools) would wrap to ~49.7 days; a difference in the upper half
    of the range is such a negative one and counts as 0.
    """

    elapsed = (current_tick - last_input_tick) & 0xFFFFFFFF

    if elapsed >= 0x80000000:
        return 0

    return elapsed


def _idle_time_windows():

    last_input_info = LASTINPUTINFO()

    last_input_info.cbSize = ctypes.sizeof(
        LASTINPUTINFO
    )

    result = ctypes.windll.user32.GetLastInputInfo(
        ctypes.byref(last_input_info)
    )

    if result == 0:
        return 0.0

    # GetTickCount returns an unsigned DWORD; ctypes would read it
    # as a signed int, which goes negative after ~24.8 days uptime.
    get_tick_count = ctypes.windll.kernel32.GetTickCount
    get_tick_count.restype = ctypes.c_uint32

    idle_time_ms = _elapsed_ticks_ms(
        get_tick_count(),
        last_input_info.dwTime
    )

    return max(
        0.0,
        idle_time_ms / 1000.0
    )


def _idle_time_mac():

    output = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])

    for line in output.splitlines():
        if "HIDIdleTime" in line:
            nanoseconds = int(line.split("=")[-1].strip())
            return max(0.0, nanoseconds / 1e9)

    return 0.0


def _idle_time_linux():
    """
    Needs xprintidle (X11).
    """

    output = _run(["xprintidle"])

    return max(0.0, int(output) / 1000.0) if output else 0.0


# ============================================================
# ACTIVITY STATUS
# ============================================================

def get_activity_status(idle_seconds, application=None):
    """
    Determine whether the user is Active or Idle.
    A locked screen always counts as Idle.
    """

    if application == LOCKED_APPLICATION:
        return "Idle"

    if idle_seconds >= IDLE_THRESHOLD_SECONDS:
        return "Idle"

    return "Active"


# ============================================================
# COLLECT ONE OBSERVATION
# ============================================================

def collect_data():
    """
    Collect one workspace observation.
    """

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    application, window_title = (
        get_active_application()
    )

    (
        cpu_percent,
        memory_percent,
        battery_percent,
        charging
    ) = get_system_metrics()

    idle_seconds = get_idle_time()

    activity_status = get_activity_status(
        idle_seconds,
        application
    )

    data = {
        "timestamp": timestamp,
        "application": application,
        "window_title": window_title,
        "activity_status": activity_status,
        "idle_seconds": round(
            idle_seconds,
            2
        ),
        "cpu_percent": round(
            cpu_percent,
            2
        ),
        "memory_percent": round(
            memory_percent,
            2
        ),
        "battery_percent": round(
            battery_percent,
            2
        ),
        "charging": charging
    }

    return data


# ============================================================
# RAW FILE PREPARATION
# ============================================================

# Raw files already checked by save_data in this process.
_prepared_files = set()


def _read_header(raw_file):
    """
    The raw file's first row. OSError (file locked or unreadable)
    is passed on: a file that cannot be read right now must not be
    mistaken for one with a wrong header and moved aside.
    """

    with open(
        raw_file,
        newline="",
        encoding="utf-8-sig",
        errors="replace"
    ) as csv_file:

        try:
            return next(csv.reader(csv_file), None)
        except csv.Error:
            return None


def _end_of_last_complete_row(raw_file):
    """
    Byte offset just after the last complete row: the last newline
    that is not inside a quoted field. The csv module doubles any
    quote inside a quoted field, so a newline is outside quotes
    exactly when an even number of quotes comes before it.
    """

    end = 0
    offset = 0
    inside_quotes = False

    with open(raw_file, "rb") as handle:

        for chunk in iter(lambda: handle.read(1 << 20), b""):

            position = offset

            for index, part in enumerate(chunk.split(b'"')):

                if index:
                    inside_quotes = not inside_quotes
                    position += 1

                if not inside_quotes:
                    newline = part.rfind(b"\n")

                    if newline != -1:
                        end = position + newline + 1

                position += len(part)

            offset += len(chunk)

    return end


def _rotated_name(raw_file):
    """
    workspace_activity.csv -> workspace_activity.<date>.csv
    (with -2, -3, ... if that name is taken).
    """

    date = datetime.now().strftime("%Y-%m-%d")

    candidate = raw_file.with_name(
        f"{raw_file.stem}.{date}{raw_file.suffix}"
    )

    number = 2

    while candidate.exists():
        candidate = raw_file.with_name(
            f"{raw_file.stem}.{date}-{number}{raw_file.suffix}"
        )
        number += 1

    return candidate


def prepare_raw_file(raw_file=None):
    """
    Make the raw CSV safe to append to.

    If its header is not RAW_COLUMNS (an older layout, or a file
    whose header was never written), the file is moved aside to
    workspace_activity.<date>.csv so a fresh one starts with the
    right header. A last row cut off mid-write (collector killed,
    power lost) is dropped, so neither the next row nor an open
    quote in a cut-off window title can break the rows after it.
    A file that needs neither is only read, never opened for
    writing.

    Returns the path the old file was moved to, or None. Raises
    OSError when the file cannot be read, moved or repaired right
    now (another program has it open, it is read-only).
    """

    raw_file = Path(raw_file or RAW_DATA_FILE)

    if not raw_file.exists() or raw_file.stat().st_size == 0:
        return None

    if _read_header(raw_file) != list(RAW_COLUMNS):
        target = _rotated_name(raw_file)

        try:
            os.replace(raw_file, target)
        except OSError as error:
            raise OSError(
                f"{raw_file.name} has an unexpected header and could "
                f"not be moved aside ({error})"
            ) from error

        print(
            f"{raw_file.name} had a different header; moved it to "
            f"{target.name} and started a new file."
        )

        return target

    with open(raw_file, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        complete = handle.read(1) == b"\n"

    if not complete:
        end = _end_of_last_complete_row(raw_file)

        with open(raw_file, "r+b") as handle:
            handle.truncate(end)

        print(
            f"{raw_file.name}: dropped an unfinished last row "
            "(the collector was stopped while writing it)."
        )

    return None


def _prepare_once(raw_file):
    """
    prepare_raw_file the first time this process writes to
    raw_file. If that fails it raises OSError and is tried again
    before the next row, so the collector recovers once the file is
    free (e.g. closed in Excel) instead of appending to a file it
    could not check.
    """

    key = str(raw_file)

    if key not in _prepared_files:
        prepare_raw_file(raw_file)
        _prepared_files.add(key)


# ============================================================
# SAVE DATA
# ============================================================

def save_data(data):
    """
    Append one observation to the raw CSV file.
    """

    raw_file = Path(RAW_DATA_FILE)

    # Make sure the directory exists
    raw_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    try:
        _prepare_once(raw_file)

        # A missing or empty file needs the header first
        write_header = (
            not raw_file.exists()
            or raw_file.stat().st_size == 0
        )

        with open(
            raw_file,
            mode="a",
            newline="",
            encoding="utf-8"
        ) as csv_file:

            writer = csv.DictWriter(
                csv_file,
                fieldnames=RAW_COLUMNS,
                extrasaction="ignore"
            )

            if write_header:
                writer.writeheader()

            writer.writerow(data)

    except Exception as error:
        print(
            f"Error saving data: {error}"
        )


# ============================================================
# DISPLAY OBSERVATION
# ============================================================

def display_data(data):
    """
    Display the latest observation in the terminal.
    """

    print(
        f"[{data['timestamp']}] "
        f"App: {data['application']} | "
        f"Status: {data['activity_status']} | "
        f"Idle: {data['idle_seconds']:.1f}s | "
        f"CPU: {data['cpu_percent']:.1f}% | "
        f"Memory: {data['memory_percent']:.1f}% | "
        f"Battery: {data['battery_percent']:.1f}% | "
        f"Charging: {data['charging']}"
    )


# ============================================================
# SAMPLE SCHEDULING
# ============================================================

def next_deadline(previous_deadline, interval, now=None):
    """
    When the next sample is due (time.monotonic() clock).

    Samples stay on a fixed grid, so time spent collecting or
    uploading does not stretch the interval. When the loop has
    fallen behind (a slow step, or the laptop slept), the next
    sample is taken straight away instead of bursting to catch up.
    """

    if now is None:
        now = time.monotonic()

    return max(previous_deadline + interval, now)


def sleep_until(deadline):

    remaining = deadline - time.monotonic()

    if remaining > 0:
        time.sleep(remaining)


# ============================================================
# MAIN COLLECTOR
# ============================================================

def main(argv=None):

    import argparse

    argparse.ArgumentParser(
        description="Record this computer's foreground app, window title, "
                    f"idle time and CPU/memory/battery every {COLLECTION_INTERVAL} s "
                    f"into {RAW_DATA_FILE}. Stop with Ctrl+C. "
                    "Set WORKSPACE_DATA_DIR to record somewhere else."
    ).parse_args(argv)

    print("=" * 65)
    print("MULTIMODAL WORKSPACE ANALYTICS")
    print("Workspace Data Collector")
    print("=" * 65)

    print(
        f"Collection interval: "
        f"{COLLECTION_INTERVAL} seconds"
    )

    print(
        f"Idle threshold: "
        f"{IDLE_THRESHOLD_SECONDS} seconds"
    )

    print(
        f"Raw data file: "
        f"{RAW_DATA_FILE}"
    )

    RAW_DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # A raw file that is open in another program (Excel locks it on
    # Windows) or read-only must not stop the collector: it is
    # checked again before each row until it can be written.
    try:
        _prepare_once(Path(RAW_DATA_FILE))
    except OSError as error:
        print(
            f"Warning: {error}. Samples are saved once the file can "
            "be written (close it in other programs)."
        )

    print()
    print("Collector started.")
    print("Press CTRL+C to stop.")
    print("-" * 65)

    next_sample = time.monotonic()

    try:

        while True:

            data = collect_data()

            save_data(data)

            display_data(data)

            next_sample = next_deadline(
                next_sample,
                COLLECTION_INTERVAL
            )

            sleep_until(next_sample)

    except KeyboardInterrupt:

        print()
        print("-" * 65)
        print("Collector stopped.")
        print("=" * 65)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
