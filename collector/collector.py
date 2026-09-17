import sys
import time
import csv
import os
import ctypes

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
import pygetwindow as gw
import win32gui
import win32process


# ============================================================
# PROJECT CONFIGURATION
# ============================================================

from config import (
    RAW_DATA_FILE,
    COLLECTION_INTERVAL
)


# ============================================================
# COLLECTOR SETTINGS
# ============================================================

# User is considered idle after this many seconds
IDLE_THRESHOLD_SECONDS = 60


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
        hwnd = win32gui.GetForegroundWindow()

        if not hwnd:
            return "Unknown", "Unknown"

        window_title = win32gui.GetWindowText(hwnd)

        try:
            _, process_id = win32process.GetWindowThreadProcessId(hwnd)

            process = psutil.Process(process_id)

            application_name = process.name()

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess
        ):
            application_name = "Unknown"

        application_name = clean_application_name(
            application_name
        )

        if not window_title:
            window_title = "Unknown"

        return application_name, window_title

    except Exception:
        return "Unknown", "Unknown"


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

    Windows-specific implementation.
    """

    try:
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

    except Exception:
        return 0.0


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