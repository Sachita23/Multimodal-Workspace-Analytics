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

    try:
        if IS_WINDOWS:
            application_name, window_title = _active_window_windows()
        elif IS_MAC:
            application_name, window_title = _active_window_mac()
        else:
            application_name, window_title = _active_window_linux()

    except Exception:
        return "Unknown", "Unknown"

    application_name = clean_application_name(
        application_name
    )

    if not window_title:
        window_title = "Unknown"

    return application_name, window_title


def _active_window_windows():

    hwnd = win32gui.GetForegroundWindow()

    if not hwnd:
        return "Unknown", "Unknown"

    window_title = win32gui.GetWindowText(hwnd)

    try:
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)

        application_name = psutil.Process(process_id).name()

    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess
    ):
        application_name = "Unknown"

    return application_name, window_title


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


def _active_window_mac():
    """
    Window titles need the Accessibility permission for the
    terminal running the collector (System Settings > Privacy &
    Security > Accessibility). Without it only the app name is
    available.
    """

    application_name = _run([
        "osascript", "-e",
        'tell application "System Events" to get name of '
        'first application process whose frontmost is true'
    ]) or "Unknown"

    window_title = _run([
        "osascript", "-e",
        'tell application "System Events" to tell '
        '(first application process whose frontmost is true) '
        'to get name of front window'
    ])

    return application_name, window_title


def _active_window_linux():
    """
    X11 only; needs xdotool. Wayland sessions report Unknown.
    """

    window_id = _run(["xdotool", "getactivewindow"])

    if not window_id:
        return "Unknown", "Unknown"

    window_title = _run(["xdotool", "getwindowname", window_id])
    pid = _run(["xdotool", "getwindowpid", window_id])

    try:
        application_name = psutil.Process(int(pid)).name()
    except (ValueError, psutil.Error):
        application_name = "Unknown"

    return application_name, window_title


# ============================================================
# APPLICATION NAME CLEANING
# ============================================================

def clean_application_name(application_name):
    """
    Convert executable names into readable application names.
    """

    if not application_name:
        return "Unknown"

    name = application_name.lower()

    application_map = {
        "code.exe": "Visual Studio Code",
        "chrome.exe": "Google Chrome",
        "msedge.exe": "Microsoft Edge",
        "firefox.exe": "Mozilla Firefox",
        "explorer.exe": "File Explorer",
        "winword.exe": "Microsoft Word",
        "excel.exe": "Microsoft Excel",
        "powerpnt.exe": "Microsoft PowerPoint",
        "outlook.exe": "Microsoft Outlook",
        "teams.exe": "Microsoft Teams",
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
    }

    if name in application_map:
        return application_map[name]

    # Remove .exe from unknown applications
    if name.endswith(".exe"):
        name = name[:-4]

    return name.title()


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

    current_tick = ctypes.windll.kernel32.GetTickCount()

    idle_time_ms = (
        current_tick -
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

def get_activity_status(idle_seconds):
    """
    Determine whether the user is Active or Idle.
    """

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
        idle_seconds
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

    file_exists = raw_file.exists()

    fieldnames = [
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

    try:
        with open(
            raw_file,
            mode="a",
            newline="",
            encoding="utf-8"
        ) as csv_file:

            writer = csv.DictWriter(
                csv_file,
                fieldnames=fieldnames
            )

            if not file_exists:
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
# MAIN COLLECTOR
# ============================================================

def main():

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

    print()
    print("Collector started.")
    print("Press CTRL+C to stop.")
    print("-" * 65)

    try:

        while True:

            data = collect_data()

            save_data(data)

            display_data(data)

            time.sleep(
                COLLECTION_INTERVAL
            )

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