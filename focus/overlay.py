"""
The Focus Assistant's deviation popup for headless focus sessions.

The dashboard's FocusEngine and `team_sync.py focus` have no window of
their own, so they show the original popup (focus_gui.DeviationPopup:
the red "FOCUS DEVIATION" box in the bottom-right corner) through a small
helper process:

    macOS    a native Cocoa panel (focus/macos_popup.m, compiled once with
             clang into ~/.workspace-analytics/bin): same look, but it shows
             over every app, Space and full-screen app and never takes focus.
             A Tk window on macOS hides whenever its app is not active and
             never appears over full-screen apps.
    others   `python -m focus.overlay` (Tk), and on macOS without clang

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

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

MACOS_SOURCE = Path(__file__).resolve().parent / "macos_popup.m"
HELPER_DIR = Path.home() / ".workspace-analytics" / "bin"

# WORKSPACE_FOCUS_POPUP=0 turns every popup off (the test suite does).
POPUP_ENV = "WORKSPACE_FOCUS_POPUP"


def popup_enabled():
    return os.environ.get(POPUP_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def native_helper(source=MACOS_SOURCE, helper_dir=None, platform=None):
    """
    Path of the compiled macOS popup, building it on first use (one
    binary per source version), or None when not on macOS or it cannot
    be built.
    """

    if (platform or sys.platform) != "darwin":
        return None

    try:
        code = Path(source).read_bytes()
    except OSError:
        return None

    helper_dir = Path(helper_dir) if helper_dir else HELPER_DIR
    binary = helper_dir / f"focus_popup-{hashlib.sha256(code).hexdigest()[:12]}"

    if binary.exists() and os.access(binary, os.X_OK):
        return binary

    compiler = shutil.which("clang")

    if compiler is None:
        return None

    try:
        helper_dir.mkdir(parents=True, exist_ok=True)
        temp = binary.with_name(binary.name + f".{os.getpid()}.tmp")
        result = subprocess.run(
            [compiler, "-fobjc-arc", "-O2", "-framework", "Cocoa", "-o", str(temp), str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120, check=False,
        )

        if result.returncode != 0 or not temp.exists():
            temp.unlink(missing_ok=True)
            return None

        os.replace(temp, binary)
        return binary

    except (OSError, subprocess.SubprocessError):
        return None


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

    def __init__(self, on_action=None, python=None, command=None):
        self.on_action = on_action
        self.python = python or sys.executable
        # The helper to run: the native macOS popup, or the Tk one.
        self.command = list(command) if command else [self.python, "-m", "focus.overlay"]
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
            self.command,
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
    An OverlayController when popups are on and a popup helper exists
    (native on macOS, else Tk), else None (the caller then falls back to
    focus.notify's simple alert window).
    """

    if not popup_enabled():
        return None

    native = native_helper()

    if native is not None:
        return OverlayController(on_action, command=[str(native)])

    if not tk_available():
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
