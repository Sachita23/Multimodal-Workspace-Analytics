"""
Stream realistic, clearly labelled demo members into a running team server.

    python -m team.live_demo                 # start (or resume) the demo
    python -m team.live_demo --remove        # delete every demo member and their data

Each demo member goes through the real flow a teammate uses: an invite is
created with the admin key, the member joins with recorded consent, and
activity is uploaded over HTTP to /api/team/ingest. On first start each
member gets a few days of history, then new samples are generated every
few seconds in real time. Members take short breaks, so the Team page shows
people going offline and coming back.

Demo members are named "Demo · ...", flagged as simulated (the Team page
shows a Demo badge), and their tokens are kept in data/team/live_demo.json
so the demo can be stopped and resumed. Real members are never touched:
--remove deletes only members flagged as simulated and the members this
demo created (by their own tokens), never a member just for their name.
Unflagged members named "Demo · ..." are only listed; add
--include-legacy-names to delete them too, after a confirmation.
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    API_PORT,
    TEAM_ADMIN_KEY_FILE,
    TEAM_CONSENT_VERSION,
    TEAM_DATA_DIR,
    TEAM_MAX_BATCH,
    TEAM_SHARED_FIELDS,
    _env,
    _project_path,
)
from collector.team_sync import ServerError, request_json
from team.api import MIN_ADMIN_KEY_LENGTH, _usable_key
from team.simulate import PROFILES, WINDOW_TITLES, generate_member_rows


def _state_file():
    """
    WORKSPACE_TEAM_DEMO_STATE, or data/team/live_demo.json when it is
    unset or blank. Relative paths are taken from the project root.
    """

    return _project_path(
        _env("WORKSPACE_TEAM_DEMO_STATE", TEAM_DATA_DIR / "live_demo.json")
    )


STATE_FILE = _state_file()
NAME_PREFIX = "Demo · "
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class AdminKeyError(Exception):
    """
    No admin key on this machine, or the server rejected it.
    """


# --------------------------------------------------
# Realtime activity for one member
# --------------------------------------------------

class DemoMember:
    """
    Generates one member's samples as time passes: dwelling in an app,
    switching, drifting idle, and taking breaks between work blocks.
    """

    def __init__(self, name, profile, seed, now):
        self.name = name
        self.apps, self.mean_dwell, self.idle_share = profile
        self.rng = random.Random(seed)
        self.app_names, self.app_weights = zip(*self.apps.items())

        self.app = self._pick_app()
        self.next_switch = now + self._dwell()
        self.idle = 0.0

        # Stagger the first break so members don't all leave together.
        self.working = True
        self.phase_end = now + timedelta(minutes=self.rng.uniform(8, 35))

    def _pick_app(self):
        return self.rng.choices(self.app_names, self.app_weights)[0]

    def _dwell(self):
        return timedelta(seconds=self.rng.expovariate(1 / self.mean_dwell))

    def sample(self, now, interval):
        """
        Return the sample for `now`, or None while on a break.
        """

        if now >= self.phase_end:
            self.working = not self.working
            minutes = (
                self.rng.uniform(25, 50) if self.working
                else self.rng.uniform(3, 8)
            )
            self.phase_end = now + timedelta(minutes=minutes)
            self.idle = 0.0

        if not self.working:
            return None

        if now >= self.next_switch:
            self.app = self._pick_app()
            self.next_switch = now + self._dwell()

        if self.idle > 0 or self.rng.random() < self.idle_share / 12:
            self.idle = self.idle + interval if self.rng.random() > 0.08 else 0.0

        return {
            "timestamp": now.strftime(TIMESTAMP_FORMAT),
            "application": self.app,
            "window_title": self.rng.choice(WINDOW_TITLES[self.app]),
            "activity_status": "Idle" if self.idle >= 60 else "Active",
            "idle_seconds": float(self.idle),
            "cpu_percent": round(self.rng.uniform(5, 45), 1),
            "memory_percent": round(self.rng.uniform(40, 80), 1),
            "battery_percent": 80.0,
            "charging": True,
        }


# --------------------------------------------------
# State
# --------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {"members": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


# --------------------------------------------------
# Server calls
# --------------------------------------------------

def read_admin_key():
    """
    The admin key as the server on this machine reads it:
    WORKSPACE_TEAM_ADMIN_KEY, else data/team/admin_key.

    Unlike team.api.load_admin_key this never creates or replaces the
    key file; that is the server's job, and a key made up here would
    only be rejected.
    """

    key = _usable_key(os.environ.get("WORKSPACE_TEAM_ADMIN_KEY"))

    if key:
        return key

    try:
        key = _usable_key(TEAM_ADMIN_KEY_FILE.read_text(encoding="utf-8"))
    except OSError:
        key = None

    if key:
        return key

    raise AdminKeyError(
        f"No admin key found: WORKSPACE_TEAM_ADMIN_KEY is not set (or is "
        f"shorter than {MIN_ADMIN_KEY_LENGTH} characters) and "
        f"{TEAM_ADMIN_KEY_FILE} is missing or unusable."
    )


def admin_request(method, url, body=None):
    """
    request_json with the admin key header instead of a member token.
    Raises AdminKeyError when the server rejects the key (401).
    """

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Admin-Key": read_admin_key(),
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise AdminKeyError("The team server rejected the admin key.") from None
        raise ServerError(error.code, error.reason) from None


def print_admin_key_help(server, error):
    print(f"{error}")
    print(f"Set WORKSPACE_TEAM_ADMIN_KEY to the admin key the team server at "
          f"{server} printed at startup, then try again.")


def upload(server, token, rows):
    accepted = 0
    for start in range(0, len(rows), TEAM_MAX_BATCH):
        result = request_json(
            "POST",
            f"{server}/api/team/ingest",
            {"observations": rows[start:start + TEAM_MAX_BATCH]},
            token=token,
        )
        accepted += result["accepted"]
    return accepted


def member_is_valid(server, token):
    try:
        request_json("GET", f"{server}/api/team/me", token=token)
        return True
    except ServerError as error:
        if error.status == 401:
            return False
        raise


def create_member(server, name):
    invite = admin_request(
        "POST", f"{server}/api/team/invites", {"name": name, "simulated": True}
    )
    joined = request_json("POST", f"{server}/api/team/join", {
        "invite_code": invite["invite_code"],
        "consent": {
            "accepted": True,
            "version": TEAM_CONSENT_VERSION,
            "fields": TEAM_SHARED_FIELDS,
        },
    })
    return joined["member_id"], joined["token"]


# --------------------------------------------------
# Commands
# --------------------------------------------------

def ensure_members(server, history_days):
    """
    Reuse demo members from a previous run, recreating any that were
    removed, and backfill history for new ones.
    """

    state = load_state()
    known = {m["name"]: m for m in state.get("members", [])}
    members = []
    now = datetime.now().replace(microsecond=0)

    for seed, (profile_name, profile) in enumerate(PROFILES.items()):
        name = NAME_PREFIX + profile_name
        saved = known.get(name)

        if saved and saved.get("server") == server and member_is_valid(server, saved["token"]):
            members.append(saved)
            continue

        member_id, token = create_member(server, name)

        history = [
            row for row in generate_member_rows(
                profile, days=history_days, seed=seed,
                start=(now - timedelta(days=history_days)).replace(
                    hour=9, minute=0, second=0
                ),
            )
            if row["timestamp"] < (now - timedelta(minutes=1)).strftime(TIMESTAMP_FORMAT)
        ]
        accepted = upload(server, token, history)
        print(f"Created {name}: {accepted} history samples over {history_days} days")

        saved = {"name": name, "member_id": member_id, "token": token,
                 "server": server, "seed": seed}
        members.append(saved)

    state["members"] = members
    save_state(state)
    return members


def cmd_run(args):
    server = args.server.rstrip("/")

    try:
        members = ensure_members(server, args.history_days)
    except AdminKeyError as error:
        print_admin_key_help(server, error)
        return 1
    except (ServerError, OSError) as error:
        print(f"Could not reach the team server at {server}: {error}")
        print("Start it first, e.g.: python start_project.py --team")
        return 1

    now = datetime.now().replace(microsecond=0)
    streams = {
        m["name"]: DemoMember(
            m["name"], PROFILES[m["name"][len(NAME_PREFIX):]],
            seed=m["seed"] + int(now.timestamp()), now=now,
        )
        for m in members
    }
    tokens = {m["name"]: m["token"] for m in members}
    pending = {name: [] for name in streams}

    print(f"\nStreaming {len(streams)} demo members to {server} "
          f"(a sample every {args.interval}s, uploads every {args.upload_every}s).")
    print("Open the Team page to watch. Ctrl+C to stop; "
          "`python -m team.live_demo --remove` deletes the demo members.\n")

    last_upload = time.monotonic()

    try:
        while streams:
            now = datetime.now().replace(microsecond=0)

            for name, stream in streams.items():
                row = stream.sample(now, args.interval)
                if row:
                    pending[name].append(row)

            if time.monotonic() - last_upload >= args.upload_every:
                flush(server, streams, tokens, pending)
                last_upload = time.monotonic()

            time.sleep(args.interval)

    except KeyboardInterrupt:
        flush(server, streams, tokens, pending)
        print("\nDemo stopped. Members will show offline in about 2 minutes.")

    return 0


def flush(server, streams, tokens, pending):
    status = []

    for name in list(streams):
        rows, pending[name] = pending[name], []

        if not rows:
            status.append(f"{name[len(NAME_PREFIX):].split(' (')[0]}: break")
            continue

        try:
            upload(server, tokens[name], rows)
            status.append(f"{name[len(NAME_PREFIX):].split(' (')[0]}: {rows[-1]['application']}")
        except ServerError as error:
            if error.status == 401:
                print(f"{name} was removed on the server; no longer streaming it.")
                del streams[name]
                continue
            pending[name] = rows + pending[name]
        except OSError as error:
            pending[name] = rows + pending[name]
            print(f"Server unreachable ({error}); will retry.")
            return

    print(f"[{datetime.now():%H:%M:%S}] " + " | ".join(status))


def _delete_own_members(server, state):
    """
    Delete the members this demo created on `server`, each with its
    own token (DELETE /api/team/me), so only a member whose secret
    token we hold can go. Returns how many were deleted.
    """

    deleted = 0

    for saved in state.get("members", []):
        if saved.get("server") != server or not saved.get("token"):
            continue

        try:
            request_json("DELETE", f"{server}/api/team/me", token=saved["token"])
        except ServerError as error:
            if error.status == 401:
                continue  # already gone, or a different database
            raise

        deleted += 1

    return deleted


def _admin_delete(server, member_id):
    """
    Delete one member with the admin key. False when it was
    already gone.
    """

    try:
        admin_request("DELETE", f"{server}/api/team/members/{member_id}")
    except ServerError as error:
        if error.status == 404:
            return False
        raise

    return True


def _confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_remove(args):
    """
    Delete the demo's own members (by token) and every member flagged
    as simulated. Unflagged members whose name merely starts with
    "Demo · " are listed and kept unless --include-legacy-names is
    given and the deletion is confirmed.
    """

    server = args.server.rstrip("/")
    state = load_state()

    try:
        removed = _delete_own_members(server, state)

        # /live lists every member with name and simulated flag
        # straight from SQL (no team analysis).
        members = admin_request("GET", f"{server}/api/team/live")["members"]

        for member in members:
            if member.get("simulated"):
                removed += _admin_delete(server, member["id"])

        legacy = [
            member for member in members
            if not member.get("simulated")
            and member["name"].startswith(NAME_PREFIX)
        ]

        if legacy:
            print(f"{len(legacy)} member(s) named \"{NAME_PREFIX}...\" are not "
                  "flagged as demo members:")
            for member in legacy:
                print(f"  #{member['id']}  {member['name']}  ({member['status']})")

            if not args.include_legacy_names:
                print("They were kept. If they are left over from an older demo, "
                      "run again with --include-legacy-names to delete them.")
            elif args.yes or _confirm(
                "Delete these members and all their data? [y/N] "
            ):
                for member in legacy:
                    removed += _admin_delete(server, member["id"])
            else:
                print("Kept them.")

    except AdminKeyError as error:
        print_admin_key_help(server, error)
        return 1
    except (ServerError, OSError) as error:
        print(f"Could not reach the team server at {server}: {error}")
        return 1

    state["members"] = [
        saved for saved in state.get("members", [])
        if saved.get("server") != server
    ]
    save_state(state)
    print(f"Removed {removed} demo members and all their data.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stream live demo members to the team server.")
    parser.add_argument("--server", default=f"http://127.0.0.1:{API_PORT}",
                        help="team server URL (default: this machine)")
    parser.add_argument("--history-days", type=int, default=3,
                        help="days of history to create for new demo members")
    parser.add_argument("--interval", type=int, default=5, help="seconds between samples")
    parser.add_argument("--upload-every", type=int, default=10, help="seconds between uploads")
    parser.add_argument("--remove", action="store_true",
                        help="delete all demo members and their data, then exit")
    parser.add_argument("--include-legacy-names", action="store_true",
                        help="with --remove: also delete members that are not flagged "
                             f"as demo members but are named \"{NAME_PREFIX}...\" "
                             "(asks first)")
    parser.add_argument("--yes", action="store_true",
                        help="with --include-legacy-names: do not ask for confirmation")
    args = parser.parse_args(argv)

    return cmd_remove(args) if args.remove else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
