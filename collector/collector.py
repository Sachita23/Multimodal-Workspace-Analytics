import time
import csv
import ctypes
from datetime import datetime

import psutil
import win32gui
import win32process

from config import (
    RAW_DATA_FILE,
    COLLECTION_INTERVAL,
    IDLE_THRESHOLD_SECONDS
)


# --------------------------------------------------
# Application Detection
# --------------------------------------------------

def get_active_application():
    """
    Returns:
        application process name
        active window title
    """

    try:
        hwnd = win32gui.GetForegroundWindow()

        if hwnd == 0:
            return "Unknown", "Unknown"

        window_title = win32gui.GetWindowText(hwnd)

        _, process_id = (
            win32process.GetWindowThreadProcessId(hwnd)
        )

        process = psutil.Process(process_id)

        return process.name(), window_title

    except Exception:
        return "Unknown", "Unknown"


# --------------------------------------------------
# Application Name Cleaning
# --------------------------------------------------

def clean_application_name(process_name):
    """
    Converts Windows process names into readable
    application names.
    """

    application_map = {

        "Code.exe": "Visual Studio Code",

        "chrome.exe": "Google Chrome",

        "msedge.exe": "Microsoft Edge",

        "explorer.exe": "File Explorer",

        "WINWORD.EXE": "Microsoft Word",

        "EXCEL.EXE": "Microsoft Excel",

        "POWERPNT.EXE": "Microsoft PowerPoint",

        "notepad.exe": "Notepad",

        "devenv.exe": "Visual Studio",

        "pycharm64.exe": "PyCharm",

        "idea64.exe": "IntelliJ IDEA",

        "Teams.exe": "Microsoft Teams",

        "OUTLOOK.EXE": "Microsoft Outlook",
    }

    return application_map.get(
        process_name,
        process_name
    )


# --------------------------------------------------
# System Metrics
# --------------------------------------------------

def get_system_metrics():
    """
    Collect CPU, memory and battery information.
    """

    cpu_percent = psutil.cpu_percent(
        interval=1
    )

    memory_percent = (
        psutil.virtual_memory().percent
    )

    battery = psutil.sensors_battery()

    if battery is not None:

        battery_percent = battery.percent

        charging = battery.power_plugged

    else:

        battery_percent = None

        charging = None

    return (
        cpu_percent,
        memory_percent,
        battery_percent,
        charging
    )


# --------------------------------------------------
# Idle Detection
# --------------------------------------------------

def get_idle_time():
    """
    Returns the number of seconds since the
    last keyboard or mouse interaction.
    """

    class LASTINPUTINFO(ctypes.Structure):

        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint)
        ]

    last_input = LASTINPUTINFO()

    last_input.cbSize = (
        ctypes.sizeof(LASTINPUTINFO)
    )

    ctypes.windll.user32.GetLastInputInfo(
        ctypes.byref(last_input)
    )

    current_tick = (
        ctypes.windll.kernel32.GetTickCount()
    )

    idle_milliseconds = (
        current_tick - last_input.dwTime
    )

    return idle_milliseconds / 1000


def get_activity_status():
    """
    Determines whether the user is Active or Idle.
    """

    idle_seconds = get_idle_time()

    if idle_seconds >= IDLE_THRESHOLD_SECONDS:

        status = "Idle"

    else:

        status = "Active"

    return status, idle_seconds


# --------------------------------------------------
# Collect One Record
# --------------------------------------------------

def collect_data():
    """
    Collects one raw workspace activity record.
    """

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    process_name, window_title = (
        get_active_application()
    )

    application = clean_application_name(
        process_name
    )

    (
        cpu_percent,
        memory_percent,
        battery_percent,
        charging
    ) = get_system_metrics()

    activity_status, idle_seconds = (
        get_activity_status()
    )

    return {

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

        "battery_percent": battery_percent,

        "charging": charging
    }


# --------------------------------------------------
# Save Record
# --------------------------------------------------

def save_data(data):
    """
    Appends one record to the raw CSV file.
    """

    RAW_DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    file_exists = RAW_DATA_FILE.exists()

    with open(
        RAW_DATA_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=data.keys()
        )

        if not file_exists:

            writer.writeheader()

        writer.writerow(data)


# --------------------------------------------------
# Main Collector
# --------------------------------------------------

def main():

    print("=" * 65)

    print(
        "MULTIMODAL WORKSPACE ANALYTICS"
    )

    print(
        "Workspace Data Collector"
    )

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

    print(
        "\nPress CTRL+C to stop.\n"
    )

    try:

        while True:

            data = collect_data()

            save_data(data)

            print(
                f"[{data['timestamp']}] "
                f"{data['application']} | "
                f"{data['window_title']} | "
                f"{data['activity_status']} | "
                f"CPU {data['cpu_percent']}% | "
                f"RAM {data['memory_percent']}%"
            )

            time.sleep(
                COLLECTION_INTERVAL
            )

    except KeyboardInterrupt:

        print(
            "\n\nCollector stopped."
        )


if __name__ == "__main__":
    main()