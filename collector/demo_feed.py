"""
Single-user live demo feed: writes realistic collector rows to
config.RAW_DATA_FILE, so the dashboard has live data to show
without recording your own activity.

    WORKSPACE_DATA_DIR=data/demo python -m collector.demo_feed --backfill-hours 6

It refuses to run against the default data/ directory, so it can
never mix invented rows into your real activity. Point the
pipeline and API at the same WORKSPACE_DATA_DIR to see the result:

    WORKSPACE_DATA_DIR=data/demo python live_pipeline.py
    WORKSPACE_DATA_DIR=data/demo python api/server.py

The simulated user alternates work blocks (focused coding,
communication, spreadsheet analysis) with 5-30 minute breaks
during which idle time keeps climbing, so sessions split the same
way they do for a real collector.
"""

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    COLLECTION_INTERVAL,
    DATA_DIR,
    IDLE_THRESHOLD_SECONDS,
    RAW_COLUMNS,
    RAW_DATA_FILE,
)


DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_RAW_FILE = DEFAULT_DATA_DIR / "raw" / "workspace_activity.csv"

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


# ============================================================
# SIMULATED BEHAVIOUR
# ============================================================

# name: (apps with weights, mean seconds in an app, CPU range)
WORK_MODES = {
    "focused": (
        {"Visual Studio Code": 6, "Terminal": 2, "Google Chrome": 2},
        300, (15, 45),
    ),
    "communication": (
        {"Slack": 3, "Microsoft Outlook": 2, "Zoom": 2,
         "Google Chrome": 2, "Microsoft Teams": 1},
        45, (10, 30),
    ),
    "analysis": (
        {"Microsoft Excel": 4, "Google Chrome": 2,
         "Microsoft Word": 1, "Microsoft PowerPoint": 1},
        150, (20, 55),
    ),
}

MODE_WEIGHTS = {"focused": 5, "communication": 3, "analysis": 3}

WINDOW_TITLES = {
    "Visual Studio Code": ["session_processor.py - Visual Studio Code",
                           "clustering.py - Visual Studio Code",
                           "README.md - Visual Studio Code"],
    "Terminal": ["zsh - Terminal", "pytest - Terminal"],
    "Google Chrome": ["pandas documentation - Google Chrome",
                      "Stack Overflow - Google Chrome",
                      "Pull request #42 - Google Chrome"],
    "Slack": ["#general - Slack", "#data-team - Slack", "Direct message - Slack"],
    "Microsoft Outlook": ["Inbox - Outlook", "Weekly sync - Outlook"],
    "Zoom": ["Zoom Meeting"],
    "Microsoft Teams": ["Standup | Microsoft Teams"],
    "Microsoft Excel": ["Q3 budget.xlsx - Excel", "Survey results.xlsx - Excel"],
    "Microsoft Word": ["Project proposal.docx - Word"],
    "Microsoft PowerPoint": ["Quarterly review.pptx - PowerPoint"],
}

# Work blocks and breaks, in minutes. A break of at least
# SESSION_IDLE_BREAK_SECONDS (5 min) plus one sample splits sessions.
WORK_MINUTES = (20, 70)
BREAK_MINUTES = (5.25, 30)

# Short pauses while working (reading, thinking), in seconds.
# They stay well under the 5-minute session break.
PAUSE_PER_SAMPLE = 1 / 120
PAUSE_SECONDS = (20, 200)


class DemoUser:
    """
    Produces one collector row per call to sample(t), with t
    increasing.
    """

    def __init__(self, start, seed=None):
        self.rng = random.Random(seed)

        self.idle = 0.0
        self.memory = self.rng.uniform(55, 70)
        self.battery = self.rng.uniform(60, 95)
        self.charging = False

        self.breaks = 0
        self._start_work(start)

    # --------------------------------------------------------

    def _start_work(self, t):
        self.on_break = False
        self.mode = self.rng.choices(
            list(MODE_WEIGHTS), list(MODE_WEIGHTS.values())
        )[0]
        self.phase_end = t + timedelta(minutes=self.rng.uniform(*WORK_MINUTES))
        self.pause_until = t
        self._switch_app(t)

    def _start_break(self, t):
        self.on_break = True
        self.breaks += 1
        self.phase_end = t + timedelta(minutes=self.rng.uniform(*BREAK_MINUTES))

    def _switch_app(self, t):
        apps, mean_dwell, _ = WORK_MODES[self.mode]
        names, weights = zip(*apps.items())

        self.app = self.rng.choices(names, weights)[0]
        self.window = self.rng.choice(WINDOW_TITLES[self.app])
        self.next_switch = t + timedelta(
            seconds=self.rng.expovariate(1 / mean_dwell)
        )

    # --------------------------------------------------------

    def sample(self, t, interval=COLLECTION_INTERVAL):

        if t >= self.phase_end:
            if self.on_break:
                self._start_work(t)
                self.idle = 0.0
            else:
                self._start_break(t)

        if self.on_break:
            # Away from the keyboard: the screen stays on the last app.
            self.idle += interval

        else:
            if t >= self.pause_until and self.rng.random() < PAUSE_PER_SAMPLE:
                self.idle = 0.0
                self.pause_until = t + timedelta(
                    seconds=self.rng.uniform(*PAUSE_SECONDS)
                )

            if t < self.pause_until:
                self.idle += interval
            else:
                self.idle = round(self.rng.uniform(0, 3), 2)

                if t >= self.next_switch:
                    self._switch_app(t)
                elif self.rng.random() < 0.05:
                    self.window = self.rng.choice(WINDOW_TITLES[self.app])

        idle_now = self.idle >= IDLE_THRESHOLD_SECONDS

        cpu_low, cpu_high = (2, 10) if idle_now else WORK_MODES[self.mode][2]

        self.memory = min(88.0, max(45.0, self.memory + self.rng.gauss(0, 0.4)))

        if self.charging:
            self.battery = min(100.0, self.battery + 0.08 * interval / 5)
            if self.battery >= 100:
                self.charging = False
        else:
            self.battery = max(5.0, self.battery - 0.02 * interval / 5)
            if self.battery < 25:
                self.charging = True

        return {
            "timestamp": t.strftime(TIMESTAMP_FORMAT),
            "application": self.app,
            "window_title": self.window,
            "activity_status": "Idle" if idle_now else "Active",
            "idle_seconds": round(float(self.idle), 2),
            "cpu_percent": round(self.rng.uniform(cpu_low, cpu_high), 1),
            "memory_percent": round(self.memory, 1),
            "battery_percent": round(self.battery, 1),
            "charging": self.charging,
        }


# ============================================================
# RAW FILE
# ============================================================

def _same_path(first, second):
    """
    True when two paths name the same file or folder.

    Compared by identity (os.path.samefile) when both exist, so other
    spellings of the same folder count: "Data" on a case-insensitive
    disk (macOS, Windows), symlinks, "~/desktop/...". When one does not
    exist yet, the resolved paths are compared ignoring case, which
    errs on the side of refusing.
    """

    first, second = Path(first), Path(second)

    try:
        return os.path.samefile(first, second)

    except OSError:
        pass

    def spelled(path):
        return os.path.normcase(str(path.expanduser().resolve())).casefold()

    return spelled(first) == spelled(second)


def uses_default_data_dir(data_dir=None, raw_file=None):
    """
    True when the feed would write into the owner's own data/: the
    data folder, or the raw file itself, is the default one.
    """

    data_dir = Path(data_dir or DATA_DIR)
    raw_file = Path(raw_file or data_dir / "raw" / "workspace_activity.csv")

    return (
        _same_path(data_dir, DEFAULT_DATA_DIR)
        or _same_path(raw_file, DEFAULT_RAW_FILE)
    )


def _has_rows(raw_file):
    return raw_file.exists() and raw_file.stat().st_size > 0


def _last_timestamp(raw_file):
    """
    Timestamp of the newest row in raw_file, or None.
    """

    if not _has_rows(raw_file):
        return None

    last = None

    with raw_file.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            last = row.get("timestamp") or last

    try:
        return datetime.strptime(last, TIMESTAMP_FORMAT) if last else None
    except ValueError:
        return None


def append_rows(raw_file, rows):

    raw_file.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _has_rows(raw_file)

    with raw_file.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=RAW_COLUMNS)

        if write_header:
            writer.writeheader()

        writer.writerows(rows)


def backfill(user, start, end, interval=COLLECTION_INTERVAL):
    """
    Rows every `interval` seconds from start up to (not incl.) end.
    """

    rows = []
    t = start

    while t < end:
        rows.append(user.sample(t, interval))
        t += timedelta(seconds=interval)

    return rows


# ============================================================
# MAIN
# ============================================================

def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Stream simulated single-user activity into "
                    "WORKSPACE_DATA_DIR (never the default data/)."
    )
    parser.add_argument("--backfill-hours", type=float, default=0,
                        help="first write this many hours of history "
                             "(only when the demo raw file is empty)")
    parser.add_argument("--backfill-only", action="store_true",
                        help="write the backfill and exit instead of streaming")
    parser.add_argument("--reset", action="store_true",
                        help="delete the demo raw file first")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed, for repeatable demos")
    args = parser.parse_args(argv)

    if uses_default_data_dir(DATA_DIR, RAW_DATA_FILE):
        print(
            "Refusing to write demo rows into your real data directory:\n"
            f"  {DATA_DIR}\n"
            "Set WORKSPACE_DATA_DIR to a separate folder, for example:\n"
            "  WORKSPACE_DATA_DIR=data/demo python -m collector.demo_feed "
            "--backfill-hours 6",
            file=sys.stderr,
        )
        return 2

    raw_file = Path(RAW_DATA_FILE)

    if args.reset:
        raw_file.unlink(missing_ok=True)

    interval = COLLECTION_INTERVAL
    now = datetime.now().replace(microsecond=0)
    last = _last_timestamp(raw_file)

    if last is None and args.backfill_hours > 0:
        start = now - timedelta(hours=args.backfill_hours)
        user = DemoUser(start, seed=args.seed)
        rows = backfill(user, start, now, interval)
        append_rows(raw_file, rows)

        print(f"Backfilled {len(rows)} rows ({args.backfill_hours:g} h, "
              f"{user.breaks} idle breaks) into {raw_file}", flush=True)

    else:
        if last is not None and args.backfill_hours > 0:
            print(f"{raw_file} already has rows up to {last}; "
                  "skipping the backfill (use --reset to start over).")

        user = DemoUser(now, seed=args.seed)

    if args.backfill_only:
        return 0

    print(f"Streaming a demo row every {interval}s into {raw_file}. "
          "Press CTRL+C to stop.", flush=True)

    deadline = time.monotonic()

    try:
        while True:
            row = user.sample(datetime.now().replace(microsecond=0), interval)
            append_rows(raw_file, [row])

            deadline += interval
            time.sleep(max(0.0, deadline - time.monotonic()))

    except KeyboardInterrupt:
        print("\nDemo feed stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
