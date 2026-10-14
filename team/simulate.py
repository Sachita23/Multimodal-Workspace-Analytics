"""
Fill the team database with simulated members, for demos and tests.

    python -m team.simulate            # 5 members, 3 days each
    python -m team.simulate --reset    # wipe the team DB first

Every simulated member goes through the real invite -> consent ->
token flow, so the data looks exactly like collector uploads.
"""

import argparse
import random
from datetime import datetime, timedelta

from config import TEAM_CONSENT_VERSION, TEAM_DB_FILE, TEAM_SHARED_FIELDS
from team import db


# name: (apps with weights, mean seconds between switches, idle share)
PROFILES = {
    "Asha (focused dev)": (
        {"Visual Studio Code": 6, "Windows Terminal": 2, "Google Chrome": 1},
        420, 0.08,
    ),
    "Ben (focused dev)": (
        {"PyCharm": 6, "PowerShell": 2, "Google Chrome": 1},
        360, 0.10,
    ),
    "Chen (analyst)": (
        {"Microsoft Excel": 5, "Google Chrome": 3, "Microsoft Teams": 1},
        240, 0.15,
    ),
    "Dana (analyst)": (
        {"Microsoft Excel": 4, "Microsoft Edge": 3, "Microsoft Word": 2},
        260, 0.14,
    ),
    "Eli (context switcher)": (
        {"Slack": 3, "Google Chrome": 3, "Microsoft Outlook": 2,
         "Zoom": 2, "Microsoft Word": 1, "Spotify": 1},
        25, 0.45,
    ),
}

WINDOW_TITLES = {
    "Visual Studio Code": ["etl/session_processor.py - VS Code", "README.md - VS Code"],
    "PyCharm": ["ml/clustering.py - PyCharm", "tests - PyCharm"],
    "Google Chrome": ["Pandas docs - Google Chrome", "Stack Overflow - Google Chrome"],
    "Microsoft Edge": ["Quarterly report - Microsoft Edge"],
    "Microsoft Excel": ["Q3 budget.xlsx - Excel", "Survey results.xlsx - Excel"],
    "Microsoft Word": ["Project proposal.docx - Word"],
    "Microsoft Teams": ["Standup | Microsoft Teams"],
    "Microsoft Outlook": ["Inbox - Outlook"],
    "Slack": ["#general - Slack", "#random - Slack"],
    "Zoom": ["Zoom Meeting"],
    "Spotify": ["Spotify Premium"],
    "Windows Terminal": ["Windows Terminal"],
    "PowerShell": ["Windows PowerShell"],
}


def generate_member_rows(profile, days=3, interval=5, seed=0, start=None):
    apps, mean_dwell, idle_share = profile
    rng = random.Random(seed)
    names, weights = zip(*apps.items())

    start = start or (datetime.now() - timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )

    rows = []

    for day in range(days):
        # Two work blocks per day with a lunch break between them.
        for block_start, hours in ((9, 3), (13, 3.5)):
            t = start + timedelta(days=day, hours=block_start - 9,
                                  minutes=rng.randint(0, 20))
            end = t + timedelta(hours=hours)
            app = rng.choices(names, weights)[0]
            next_switch = t + timedelta(seconds=rng.expovariate(1 / mean_dwell))
            idle = 0.0

            while t < end:
                if t >= next_switch:
                    app = rng.choices(names, weights)[0]
                    next_switch = t + timedelta(
                        seconds=rng.expovariate(1 / mean_dwell)
                    )

                if idle > 0 or rng.random() < idle_share / 12:
                    idle = idle + interval if rng.random() > 0.08 else 0.0
                status = "Idle" if idle >= 60 else "Active"

                rows.append({
                    "timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                    "application": app,
                    "window_title": rng.choice(WINDOW_TITLES[app]),
                    "activity_status": status,
                    "idle_seconds": float(idle),
                    "cpu_percent": round(rng.uniform(5, 45), 1),
                    "memory_percent": round(rng.uniform(40, 80), 1),
                    "battery_percent": 80.0,
                    "charging": True,
                })
                t += timedelta(seconds=interval)

    return rows


def add_simulated_member(name, profile, days=3, seed=0, db_file=None):
    _, code = db.create_invite(name, db_file)
    member_id, _, token = db.redeem_invite(
        code, TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS, db_file
    )
    rows = generate_member_rows(profile, days=days, seed=seed)
    db.insert_observations(member_id, rows, db_file)
    return member_id, token


def main():
    parser = argparse.ArgumentParser(description="Simulate team members.")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--reset", action="store_true",
                        help="delete the team database first")
    args = parser.parse_args()

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            TEAM_DB_FILE.with_name(TEAM_DB_FILE.name + suffix).unlink(missing_ok=True)

    db.init_db()

    for seed, (name, profile) in enumerate(PROFILES.items()):
        member_id, _ = add_simulated_member(name, profile, args.days, seed)
        print(f"Added {name} (member {member_id})")

    print(f"\nTeam database: {TEAM_DB_FILE}")


if __name__ == "__main__":
    main()
