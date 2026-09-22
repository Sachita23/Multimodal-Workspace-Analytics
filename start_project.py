"""
One-command launcher for the Multimodal Workspace Analytics Platform.

Run from the project root:
    python start_project.py

Starts:
    1. Windows activity collector
    2. Live analytics pipeline
    3. Flask API
    4. React/Vite frontend

Press Ctrl+C to stop all services.
"""

from pathlib import Path
import os
import signal
import subprocess
import sys
import time
import webbrowser


# ============================================================
# PROJECT PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

COLLECTOR = PROJECT_ROOT / "collector" / "collector.py"
PIPELINE = PROJECT_ROOT / "live_pipeline.py"
API_SERVER = PROJECT_ROOT / "api" / "server.py"

FRONTEND_DIR = (
    PROJECT_ROOT
    / "frontend"
    / "Multimodal-Workspace-Analytics"
    / "frontend"
)

API_URL = "http://127.0.0.1:5000"
DASHBOARD_URL = "http://localhost:5173"

processes = []


# ============================================================
# DISPLAY
# ============================================================

def print_header():
    print()
    print("=" * 70)
    print("       MULTIMODAL WORKSPACE ANALYTICS PLATFORM")
    print("=" * 70)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Frontend     : {FRONTEND_DIR}")
    print("=" * 70)


# ============================================================
# CHECK REQUIRED FILES
# ============================================================

def check_paths():
    required = {
        "Activity Collector": COLLECTOR,
        "Live Pipeline": PIPELINE,
        "Flask API": API_SERVER,
        "Frontend Folder": FRONTEND_DIR,
    }

    missing = []

    for name, path in required.items():
        if not path.exists():
            missing.append(f"{name}: {path}")

    if missing:
        print()
        print("ERROR: Required paths were not found:")
        print()

        for item in missing:
            print(f"  - {item}")

        print()
        sys.exit(1)


# ============================================================
# START PROCESS
# ============================================================

def start_process(name, command, cwd=None):
    print(f"[STARTING] {name}...")

    creationflags = 0

    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else str(PROJECT_ROOT),
        creationflags=creationflags,
    )

    processes.append((name, process))

    print(f"[RUNNING]  {name} (PID {process.pid})")

    return process


# ============================================================
# STOP PROCESS
# ============================================================

def stop_process(name, process):
    if process.poll() is not None:
        return

    print(f"[STOPPING] {name}...")

    try:
        if os.name == "nt":
            # Kill the complete process tree on Windows.
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
            process.wait(timeout=5)

        print(f"[STOPPED]  {name}")

    except Exception as exc:
        print(f"[WARNING] Could not stop {name}: {exc}")


# ============================================================
# STOP EVERYTHING
# ============================================================

def stop_all():
    print()
    print("=" * 70)
    print("Stopping Workspace Analytics Platform...")
    print("=" * 70)

    for name, process in reversed(processes):
        stop_process(name, process)

    print()
    print("All services stopped.")
    print()


# ============================================================
# MAIN
# ============================================================

def main():

    print_header()

    # --------------------------------------------------------
    # Check project structure
    # --------------------------------------------------------

    check_paths()

    try:

        # ----------------------------------------------------
        # 1. Activity Collector
        # ----------------------------------------------------

        start_process(
            "Activity Collector",
            [
                sys.executable,
                str(COLLECTOR),
            ],
            cwd=PROJECT_ROOT,
        )

        time.sleep(1)

        # ----------------------------------------------------
        # 2. Live Analytics Pipeline
        # ----------------------------------------------------

        start_process(
            "Live Analytics Pipeline",
            [
                sys.executable,
                str(PIPELINE),
            ],
            cwd=PROJECT_ROOT,
        )

        time.sleep(1)

        # ----------------------------------------------------
        # 3. Flask API
        # ----------------------------------------------------

        start_process(
            "Flask API",
            [
                sys.executable,
                str(API_SERVER),
            ],
            cwd=PROJECT_ROOT,
        )

        time.sleep(2)

        # ----------------------------------------------------
        # 4. React / Vite Frontend
        # ----------------------------------------------------

        npm_command = "npm.cmd" if os.name == "nt" else "npm"

        start_process(
            "React Dashboard",
            [
                npm_command,
                "run",
                "dev",
            ],
            cwd=FRONTEND_DIR,
        )

        # ----------------------------------------------------
        # Everything started
        # ----------------------------------------------------

        print()
        print("=" * 70)
        print("ALL SERVICES STARTED")
        print("=" * 70)
        print()
        print("Activity Collector : RUNNING")
        print("Live Pipeline      : RUNNING")
        print(f"Flask API          : {API_URL}")
        print(f"React Dashboard    : {DASHBOARD_URL}")
        print()
        print("=" * 70)
        print("Opening dashboard in your browser...")
        print("Press Ctrl+C in this terminal to stop everything.")
        print("=" * 70)
        print()

        # Give Vite a few seconds to start.
        time.sleep(4)

        webbrowser.open(DASHBOARD_URL)

        # ----------------------------------------------------
        # Keep launcher alive
        # ----------------------------------------------------

        while True:

            time.sleep(2)

            # Check whether any service stopped unexpectedly.
            for name, process in processes:

                exit_code = process.poll()

                if exit_code is not None:

                    print()
                    print(
                        f"[WARNING] {name} stopped "
                        f"with exit code {exit_code}."
                    )

    except KeyboardInterrupt:

        print()
        print("Ctrl+C received.")

    except Exception as exc:

        print()
        print(f"[ERROR] Launcher failed: {exc}")

    finally:

        stop_all()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()