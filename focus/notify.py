"""
Desktop notifications for focus deviations.

notify(title, message) shows a system notification and returns at once:
the helper program runs in the background and is reaped by a daemon
thread. It never raises; it returns True when a helper was started.

    macOS    osascript `display notification` (sound "Basso")
    Windows  a PowerShell balloon tip
    Linux    notify-send

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
        return True

    except Exception:
        return False
