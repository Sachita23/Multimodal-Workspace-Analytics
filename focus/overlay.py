"""
The Focus Assistant's deviation popup for headless focus sessions.

The dashboard's FocusEngine and `team_sync.py focus` have no window of
their own, so they show the original popup (focus_gui.DeviationPopup:
the red "FOCUS DEVIATION" box in the bottom-right corner) through a small
helper process:

    controller = default_overlay(on_action)   # None without Tk
    controller.show(app, window_title, goal, allowed_apps)
    controller.hide()                          # back in the focus zone
    controller.close()                         # session over

Parent -> helper: one JSON object per line on stdin
    {"cmd": "show", "app", "window_title", "goal", "allowed_apps"}
    {"cmd": "hide"} | {"cmd": "quit"}
Helper -> parent: one JSON object per line on stdout
    {"action": "allow", "app": "..."} | {"action": "snooze"}

The helper exits when its stdin closes, so it never outlives the session.
This module imports tkinter only inside the helper (`python -m focus.overlay`).
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# WORKSPACE_FOCUS_POPUP=0 turns every popup off (the test suite does).
POPUP_ENV = "WORKSPACE_FOCUS_POPUP"


def popup_enabled():
    return os.environ.get(POPUP_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def tk_available():
    try:
        return importlib.util.find_spec("_tkinter") is not None
    except (ImportError, ValueError):
        return False


class OverlayController:
    """
    Runs the popup helper and talks to it; every method returns at once
    and never raises.
    """

    def __init__(self, on_action=None, python=None):
        self.on_action = on_action
        self.python = python or sys.executable
        self._process = None
        self._lock = threading.Lock()
        self.showing = False

    # ------------------------------------------------------------ helper
    def _start(self):
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        options = {}

        if sys.platform.startswith("win"):
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(
            [self.python, "-m", "focus.overlay"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **options,
        )
        threading.Thread(target=self._read_actions, args=(process,), daemon=True).start()
        return process

    def _read_actions(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue

            action = message.get("action")

            if action in ("allow", "snooze") and self.on_action is not None:
                try:
                    self.on_action(action, message.get("app"))
                except Exception:
                    pass

    def _send(self, message, start=False):
        with self._lock:
            process = self._process

            if process is None or process.poll() is not None:
                if not start:
                    return False
                try:
                    process = self._process = self._start()
                except Exception:
                    self._process = None
                    return False

            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                return True
            except Exception:
                return False

    # ------------------------------------------------------------ API
    def pids(self):
        process = self._process
        return {process.pid} if process is not None and process.poll() is None else set()

    def show(self, app, window_title, goal, allowed_apps):
        self.showing = self._send({
            "cmd": "show",
            "app": str(app or ""),
            "window_title": str(window_title or ""),
            "goal": str(goal or ""),
            "allowed_apps": [str(item) for item in (allowed_apps or [])],
        }, start=True)
        return self.showing

    def hide(self):
        if self.showing:
            self._send({"cmd": "hide"})
        self.showing = False

    def close(self):
        self.showing = False

        with self._lock:
            process, self._process = self._process, None

        if process is None:
            return

        try:
            process.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            process.stdin.flush()
            process.stdin.close()
        except Exception:
            pass

        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def default_overlay(on_action=None):
    """
    An OverlayController when popups are on and Tk is installed, else None
    (the caller then falls back to focus.notify's simple alert window).
    """

    if not popup_enabled() or not tk_available():
        return None

    return OverlayController(on_action)


# ---------------------------------------------------------------- helper
def main():
    import queue
    import tkinter as tk

    from focus.focus_gui import DeviationPopup

    commands = queue.Queue()

    def read_stdin():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except ValueError:
                continue
        commands.put({"cmd": "quit"})

    def emit(message):
        try:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()
    popup = DeviationPopup(
        root,
        on_allow=lambda app: emit({"action": "allow", "app": app}),
        on_snooze=lambda: emit({"action": "snooze"}),
    )

    def poll():
        try:
            while True:
                message = commands.get_nowait()
                cmd = message.get("cmd")

                if cmd == "show":
                    popup.show_deviation(
                        message.get("app"), message.get("window_title"),
                        message.get("goal"), message.get("allowed_apps") or [],
                    )
                elif cmd == "hide":
                    popup.hide_deviation()
                elif cmd == "quit":
                    root.destroy()
                    return
        except queue.Empty:
            pass

        root.after(150, poll)

    threading.Thread(target=read_stdin, daemon=True).start()
    root.after(150, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
