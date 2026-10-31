"""
Desktop notifications for focus deviations.

notify(title, message) shows a system notification and returns at once:
the helper program runs in the background and is reaped by a daemon
thread. It never raises; it returns True when a helper was started.

    macOS    osascript `display notification` (sound "Basso")
    Windows  a PowerShell balloon tip
    Linux    notify-send

It also opens a popup window (one at a time) that stays on screen until
it is dismissed or POPUP_SECONDS pass, because banners are easy to miss
(and macOS hides them when Script Editor's notifications are off):

    macOS    osascript `display alert` (its process, "osascript", is a
             neutral window for the focus engine)
    Windows  a top-most MessageBox titled "Focus deviation" (the engine
             treats that title as its own window)
    Linux    zenity --warning, when installed

The texts never pass through a shell or get pasted into a script: they
reach the helper as separate argv items (or, for PowerShell, environment
variables), so a window title such as `"; do shell script "..."` stays
plain text. Set WORKSPACE_FOCUS_NOTIFY=0 to turn notifications off.
"""

import os
import shutil
import subprocess
import sys
import threading


DISABLE_ENV = "WORKSPACE_FOCUS_NOTIFY"

# WORKSPACE_FOCUS_POPUP=0 keeps the banners but drops the popup window.
POPUP_ENV = "WORKSPACE_FOCUS_POPUP"

# The popup closes itself after this long if nobody clicks it.
POPUP_SECONDS = 60

# Window title of the popup. The focus engine recognises it as one of its
# own windows, so looking at the popup is never a deviation.
POPUP_WINDOW_TITLE = "Focus deviation"

MAC_POPUP_SCRIPT = [
    "on run argv",
    'display alert (item 1 of argv) message (item 2 of argv) as critical '
    'buttons {"Back to work"} default button 1 '
    f"giving up after {POPUP_SECONDS}",
    "end run",
]

WINDOWS_POPUP_SCRIPT = "; ".join([
    "Add-Type -AssemblyName System.Windows.Forms",
    "$owner = New-Object System.Windows.Forms.Form",
    "$owner.TopMost = $true",
    "[void][System.Windows.Forms.MessageBox]::Show($owner, "
    "$env:WORKSPACE_NOTIFY_TITLE + [Environment]::NewLine + [Environment]::NewLine + "
    "$env:WORKSPACE_NOTIFY_MESSAGE, "
    f"'{POPUP_WINDOW_TITLE}', 'OK', 'Warning')",
    "$owner.Dispose()",
])

_popup_lock = threading.Lock()
_popup_process = None

# False while a focus engine shows the Focus Assistant's own popup
# (focus/overlay.py); then only the banner is sent from here.
_fallback_popup = True


def use_fallback_popup(enabled):
    global _fallback_popup
    _fallback_popup = bool(enabled)

MAX_TITLE_LENGTH = 120
MAX_MESSAGE_LENGTH = 400

# Helpers that hang (e.g. a notification daemon that never answers) are
# left alone after this long; the reaper thread just stops waiting.
REAP_TIMEOUT_SECONDS = 60

# AppleScript receives the texts as `argv`, never as script source.
MAC_SCRIPT = [
    "on run argv",
    'display notification (item 2 of argv) with title (item 1 of argv) '
    'sound name "Basso"',
    "end run",
]

WINDOWS_SCRIPT = "; ".join([
    "Add-Type -AssemblyName System.Windows.Forms",
    "Add-Type -AssemblyName System.Drawing",
    "$n = New-Object System.Windows.Forms.NotifyIcon",
    "$n.Icon = [System.Drawing.SystemIcons]::Warning",
    "$n.BalloonTipIcon = 'Warning'",
    "$n.BalloonTipTitle = $env:WORKSPACE_NOTIFY_TITLE",
    "$n.BalloonTipText = $env:WORKSPACE_NOTIFY_MESSAGE",
    "$n.Visible = $true",
    "$n.ShowBalloonTip(6000)",
    "Start-Sleep -Seconds 7",
    "$n.Dispose()",
])


def _clean(value, limit):
    """
    One line of printable text, at most `limit` characters.
    """

    text = "".join(
        char if char.isprintable() else " "
        for char in str(value if value is not None else "")
    )
    text = " ".join(text.split())

    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"

    return text


def enabled():
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def command(title, message, platform=None):
    """
    (argv, extra environment) for this platform, or None when there is
    no notification helper.
    """

    platform = platform or sys.platform
    title = _clean(title, MAX_TITLE_LENGTH) or "Workspace Analytics"
    message = _clean(message, MAX_MESSAGE_LENGTH) or " "

    if platform == "darwin":
        if not shutil.which("osascript"):
            return None

        argv = ["osascript"]

        for line in MAC_SCRIPT:
            argv += ["-e", line]

        return argv + [title, message], {}

    if platform.startswith("win"):
        executable = shutil.which("powershell") or shutil.which("pwsh")

        if not executable:
            return None

        return [
            executable, "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden", "-Command", WINDOWS_SCRIPT,
        ], {
            "WORKSPACE_NOTIFY_TITLE": title,
            "WORKSPACE_NOTIFY_MESSAGE": message,
        }

    if platform.startswith("linux"):
        executable = shutil.which("notify-send")

        if not executable:
            return None

        # "--" ends the options, so a title starting with "-" is text.
        return [
            executable, "--urgency=critical", "--app-name=Workspace Analytics",
            "--", title, message,
        ], {}

    return None


def popup_enabled():
    return os.environ.get(POPUP_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def popup_command(title, message, platform=None):
    """
    (argv, extra environment) for a popup window that stays until it is
    dismissed, or None when this platform has no popup helper.
    """

    platform = platform or sys.platform
    title = _clean(title, MAX_TITLE_LENGTH) or "Workspace Analytics"
    message = _clean(message, MAX_MESSAGE_LENGTH) or " "

    if platform == "darwin":
        if not shutil.which("osascript"):
            return None

        argv = ["osascript"]

        for line in MAC_POPUP_SCRIPT:
            argv += ["-e", line]

        return argv + [title, message], {}

    if platform.startswith("win"):
        executable = shutil.which("powershell") or shutil.which("pwsh")

        if not executable:
            return None

        return [
            executable, "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden", "-Command", WINDOWS_POPUP_SCRIPT,
        ], {
            "WORKSPACE_NOTIFY_TITLE": title,
            "WORKSPACE_NOTIFY_MESSAGE": message,
        }

    if platform.startswith("linux"):
        executable = shutil.which("zenity")

        if not executable:
            return None

        return [
            executable, "--warning", f"--title={POPUP_WINDOW_TITLE}",
            f"--timeout={POPUP_SECONDS}", "--no-markup",
            f"--text={title}\n\n{message}",
        ], {}

    return None


def _spawn(argv, extra_env):
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    if extra_env:
        options["env"] = {**os.environ, **extra_env}

    if sys.platform.startswith("win"):
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = subprocess.Popen(argv, **options)
    threading.Thread(target=_reap, args=(process,), daemon=True).start()
    return process


def popup(title, message):
    """
    Open the popup window unless one is still showing. Never raises;
    returns True when a new popup was opened.
    """

    global _popup_process

    try:
        if not (enabled() and popup_enabled()):
            return False

        with _popup_lock:
            if _popup_process is not None and _popup_process.poll() is None:
                return False

            prepared = popup_command(title, message)

            if prepared is None:
                return False

            _popup_process = _spawn(*prepared)
            return True

    except Exception:
        return False


def _reap(process):
    try:
        process.wait(timeout=REAP_TIMEOUT_SECONDS)
    except Exception:
        pass


def notify(title, message):
    """
    Show a desktop notification without waiting for it. Never raises.
    """

    try:
        if not enabled():
            return False

        prepared = command(title, message)

        if prepared is None:
            return False

        argv, extra_env = prepared
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }

        if extra_env:
            options["env"] = {**os.environ, **extra_env}

        if sys.platform.startswith("win"):
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(argv, **options)
        threading.Thread(target=_reap, args=(process,), daemon=True).start()

        if _fallback_popup:
            popup(title, message)
        return True

    except Exception:
        return False
