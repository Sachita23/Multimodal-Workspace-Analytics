"""
`team_sync.py focus`: a timed focus session in the member's terminal,
shared with the team server through Flask's test client (the `sync`
fixture) instead of a network. A fake engine stands in for
focus/engine.py, so no real window is read and no notification is
shown; the last tests run the real engine with a fake sampler.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

from collector import team_sync
from team import db
from tests.test_team_sync import _join, _offline, sync  # noqa: F401  (fixture)


ALLOWED = ["Visual Studio Code", "Terminal"]
SERVER = "http://127.0.0.1:5050"


class FakeEngine:
    """
    The FocusEngine contract (start / tick / stop / live): each tick
    takes the next app from `script`; an empty script means time is
    up. KeyboardInterrupt in the script is raised from tick().
    """

    def __init__(self, script, notifier, deviation_seconds_per_tick=2):
        self.script = list(script)
        self.notifier = notifier
        self.step = deviation_seconds_per_tick
        self.state = None
        self.stopped_with = None
        self.started_with = None
        self.last_session = None

    def start(self, title, duration_minutes, allowed_apps, goal=None):
        self.started_with = (title, duration_minutes, list(allowed_apps), goal)
        now = datetime.now().replace(microsecond=0)
        self.state = {
            "running": True, "stale": False, "session_id": "fake-1",
            "source": "member", "title": title, "goal": goal or title,
            "started_at": now.isoformat(), "planned_s": duration_minutes * 60,
            "remaining_s": duration_minutes * 60, "allowed_apps": list(allowed_apps),
            "current_app": None, "current_window": None, "allowed": None,
            "state": None, "status": "Focus session running", "score": 100,
            "deviations": 0, "deviation_seconds": 0, "last_deviation": None,
            "recent_deviations": [], "updated_at": now.isoformat(), "pid": os.getpid(),
        }
        return dict(self.state)

    def live(self):
        return dict(self.state)

    def tick(self, now=None):
        live = self.state

        if not live["running"]:
            return dict(live)

        if not self.script:
            live["running"] = False
            self.stopped_with = "Completed"
            self.last_session = self._session_json("Completed")
            # Like the real engine: the idle heartbeat, no session fields.
            return {"running": False, "session_id": None, "status": "Session completed"}

        app = self.script.pop(0)

        if app is KeyboardInterrupt:
            raise KeyboardInterrupt

        allowed = app in live["allowed_apps"]

        if not allowed:
            live["deviation_seconds"] += self.step
            if live["allowed"] is not False:
                live["deviations"] += 1
                live["last_deviation"] = {
                    "app": app, "window_title": f"{app} window",
                    "at": f"2026-01-01T10:00:{live['deviations']:02d}", "penalty": 5,
                }
                self.notifier("Focus deviation", f"You switched to {app}.")

        live.update(
            current_app=app, allowed=allowed,
            state="allowed" if allowed else "deviation",
            remaining_s=max(0, live["remaining_s"] - 1),
            score=team_sync.focus_session_score(
                live["deviations"], live["deviation_seconds"]
            ),
        )
        return dict(live)

    def stop(self, status="Stopped"):
        if not self.state["running"]:
            return None
        self.state["running"] = False
        self.stopped_with = status
        return self._session_json(status)

    def _session_json(self, status):
        return {
            "id": self.state["session_id"], "title": self.state["title"],
            "goal": self.state["goal"], "start": self.state["started_at"],
            "end": datetime.now().replace(microsecond=0).isoformat(),
            "status": status, "deviations": self.state["deviations"],
            "score": self.state["score"],
        }


@pytest.fixture
def focus_env(sync, monkeypatch):
    """
    No waiting, an upload on every tick, and a fake engine whose
    script the test sets. Returns a dict with the engine, the
    notifications sent and the HTTP calls made.
    """

    env = {"script": [], "notifications": [], "engines": [], "calls": sync}

    def notify(title, message):
        env["notifications"].append((title, message))

    def create(data_dir, notifier):
        assert data_dir == team_sync.STATE_DIR / "focus"
        engine = FakeEngine(env["script"], notifier)
        env["engines"].append(engine)
        return engine

    monkeypatch.setattr(team_sync, "focus_sleep", lambda _seconds: None)
    monkeypatch.setattr(team_sync, "FOCUS_UPLOAD_SECONDS", 0)
    monkeypatch.setattr(team_sync, "desktop_notify", notify)
    monkeypatch.setattr(team_sync, "create_focus_engine", create)
    return env


def focus(*extra, apps="Visual Studio Code,Terminal", minutes="25"):
    return team_sync.main(["focus", "--minutes", minutes, "--apps", apps, *extra])


def focus_posts(calls):
    return [call for call in calls if call == ("POST", "/api/team/focus")]


def test_local_only_session_needs_no_team_and_sends_nothing(focus_env, capsys):
    focus_env["script"] += ["Visual Studio Code", "Slack", "Slack", "Terminal",
                            "Zoom", "Visual Studio Code"]

    assert focus("--no-share", "--title", "Report") == 0

    out = capsys.readouterr().out
    assert focus_env["calls"] == []
    assert not team_sync.CONFIG_FILE.exists()

    errors = [line for line in out.splitlines() if line.startswith("ERROR:")]
    assert len(errors) == 2
    assert "You switched to Slack" in errors[0]
    assert "-5 points" in errors[0]
    assert "Return to Visual Studio Code, Terminal" in errors[0]
    assert "You switched to Zoom" in errors[1]

    # One notification per deviation, none while it continues.
    assert len(focus_env["notifications"]) == 2

    # One status line per tick, with the score.
    assert out.count(" left | score ") == 7
    assert "Focus session completed: Report" in out
    assert focus_env["engines"][0].stopped_with == "Completed"
    assert focus_env["engines"][0].started_with == ("Report", 25, ALLOWED, "Report")


def test_sharing_needs_a_team(focus_env, capsys):
    assert focus("--yes") == 1

    assert "--no-share" in capsys.readouterr().out
    assert focus_env["engines"] == []


@pytest.mark.parametrize("minutes, apps", [
    ("0", "Code"), ("481", "Code"), ("25", ""), ("25", " , ,"),
    ("25", ",".join(f"App {n}" for n in range(31))),
])
def test_bad_minutes_or_apps_are_refused(focus_env, minutes, apps):
    assert focus("--no-share", apps=apps, minutes=minutes) == 1
    assert focus_env["engines"] == []


def test_apps_are_deduplicated():
    assert team_sync.parse_focus_apps(" Code, terminal ,code,,Terminal ") == [
        "Code", "terminal",
    ]


def test_shared_session_is_uploaded_while_running_and_at_the_end(
    focus_env, monkeypatch, invite, db_file, capsys
):
    _join(monkeypatch, invite("Asha"))
    focus_env["script"] += ["Visual Studio Code", "Slack", "Visual Studio Code"]

    assert focus("--yes", "--title", "Report", "--goal", "Section 2") == 0

    out = capsys.readouterr().out
    assert "Shared with the team." in out
    # One upload per tick while running, plus the final row.
    assert len(focus_posts(focus_env["calls"])) >= 2

    [row] = db.focus_sessions(db_file=db_file)
    assert row["session_id"] == "fake-1"
    assert row["status"] == "Completed"
    assert row["title"] == "Report" and row["goal"] == "Section 2"
    assert row["allowed_apps"] == ALLOWED
    assert row["deviations"] == 1
    assert row["score"] == team_sync.focus_session_score(1, 2)
    assert row["ended_at"] >= row["started_at"]
    assert row["current_app"] is None
    assert not team_sync.focus_pending_file().exists()


def test_running_rows_carry_the_current_app(focus_env, monkeypatch, invite):
    _join(monkeypatch, invite("Asha"))
    rows = []
    real_submit = team_sync.FocusUploader.submit

    # What the timer hands the uploader (which only sends the newest
    # row of a burst, so a slow server never builds a backlog).
    def recording(self, row):
        rows.append(row)
        return real_submit(self, row)

    monkeypatch.setattr(team_sync.FocusUploader, "submit", recording)
    focus_env["script"] += ["Visual Studio Code", "Slack"]

    focus("--yes")

    running = [row for row in rows if row["status"] == "running"]
    assert running and all(row["ended_at"] is None for row in running)
    assert {"current_app": "Slack", "current_allowed": False} in [
        {"current_app": r["current_app"], "current_allowed": r["current_allowed"]}
        for r in running
    ]
    assert rows[-1]["status"] == "Completed"


def test_member_sees_what_is_shared_and_can_decline(focus_env, monkeypatch, invite, capsys):
    _join(monkeypatch, invite("Asha"))
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    focus_env["script"] += ["Visual Studio Code"]

    assert focus("--title", "Report", "--goal", "Section 2") == 0

    out = capsys.readouterr().out
    assert prompts == [f"Share this session with {SERVER}? [Y/n] "]
    for shown in ("Report", "Section 2", "Visual Studio Code, Terminal",
                  "deviation", "score"):
        assert shown in out
    assert "Not shared" in out
    assert focus_posts(focus_env["calls"]) == []
    assert focus_env["engines"][0].stopped_with == "Completed"


@pytest.mark.parametrize("answer, shared", [("", True), ("y", True), ("no", False)])
def test_share_prompt_defaults_to_yes(focus_env, monkeypatch, invite, db_file, answer, shared):
    _join(monkeypatch, invite("Asha"))
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    focus()

    assert bool(db.focus_sessions(db_file=db_file)) is shared


def test_ctrl_c_stops_the_session_and_uploads_it(focus_env, monkeypatch, invite, db_file, capsys):
    _join(monkeypatch, invite("Asha"))
    focus_env["script"] += ["Visual Studio Code", "Slack", KeyboardInterrupt, "never"]

    assert focus("--yes") == 0

    assert focus_env["engines"][0].stopped_with == "Stopped"
    [row] = db.focus_sessions(db_file=db_file)
    assert row["status"] == "Stopped"
    assert row["deviations"] == 1
    assert "Focus session stopped" in capsys.readouterr().out


def test_busy_engine_is_reported(focus_env, monkeypatch, capsys):
    class Busy(RuntimeError):
        pass

    def create(data_dir, notifier):
        engine = FakeEngine([], notifier)

        def start(*_args, **_kwargs):
            raise Busy("The Focus Assistant is already open.")

        engine.start = start
        return engine

    monkeypatch.setattr(team_sync, "create_focus_engine", create)

    assert focus("--no-share") == 1
    assert "already open" in capsys.readouterr().out


def test_failed_uploads_are_kept_and_sent_by_run(focus_env, monkeypatch, invite, db_file, capsys):
    _join(monkeypatch, invite("Asha"))
    real = team_sync.request_json
    monkeypatch.setattr(team_sync, "request_json", _offline)
    focus_env["script"] += ["Visual Studio Code", "Slack"]

    assert focus("--yes") == 0
    assert "sent the next time" in capsys.readouterr().out

    pending = team_sync.focus_pending_file()
    saved = json.loads(pending.read_text())
    assert list(saved["sessions"]) == ["fake-1"]
    # Only the newest row of the session is kept: the final one.
    assert saved["sessions"]["fake-1"]["status"] == "Completed"
    if sys.platform != "win32":
        assert pending.stat().st_mode & 0o077 == 0

    assert db.focus_sessions(db_file=db_file) == []

    # `run` sends it before collecting anything.
    monkeypatch.setattr(team_sync, "request_json", real)

    def stop_at_once():
        raise KeyboardInterrupt

    monkeypatch.setattr(team_sync, "collect_data", stop_at_once)
    assert team_sync.main(["run", "--quiet"]) == 0

    assert "Uploaded 1 focus session" in capsys.readouterr().out
    assert [r["status"] for r in db.focus_sessions(db_file=db_file)] == ["Completed"]
    assert not pending.exists()


def test_next_focus_sends_what_was_kept(focus_env, monkeypatch, invite, db_file):
    _join(monkeypatch, invite("Asha"))
    real = team_sync.request_json
    monkeypatch.setattr(team_sync, "request_json", _offline)
    focus_env["script"] += ["Visual Studio Code"]
    focus("--yes")
    assert team_sync.focus_pending_file().exists()

    monkeypatch.setattr(team_sync, "request_json", real)
    focus_env["script"] += ["Visual Studio Code"]

    def second_engine(data_dir, notifier):
        engine = FakeEngine(focus_env["script"], notifier)
        engine_start = engine.start

        def start(*args, **kwargs):
            live = engine_start(*args, **kwargs)
            engine.state["session_id"] = live["session_id"] = "fake-2"
            return live

        engine.start = start
        return engine

    monkeypatch.setattr(team_sync, "create_focus_engine", second_engine)
    focus("--yes")

    assert sorted(r["session_id"] for r in db.focus_sessions(db_file=db_file)) == [
        "fake-1", "fake-2",
    ]
    assert not team_sync.focus_pending_file().exists()


def test_refused_row_is_dropped_not_retried_forever(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    bad = {"session_id": "bad", "title": "", "updated_at": "x"}

    result = team_sync.send_focus_rows(config, [bad])

    assert result.dropped == 1 and result.pending == 0
    assert not team_sync.focus_pending_file().exists()
    assert "refused" in capsys.readouterr().out


def test_rejected_token_stops_sharing_but_keeps_the_session(
    focus_env, monkeypatch, invite, admin, client, capsys
):
    member_id = None
    _join(monkeypatch, invite("Asha"))
    member_id = team_sync.load_config()["member_id"]
    client.delete(f"/api/team/members/{member_id}", headers=admin)
    focus_env["script"] += ["Visual Studio Code"]

    assert focus("--yes") == 0

    assert "no longer accepts" in capsys.readouterr().out
    assert team_sync.focus_pending_file().exists()


def test_pending_rows_of_another_membership_are_ignored(sync, monkeypatch, invite):
    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    team_sync.save_focus_pending({"x": {"session_id": "x"}}, {"member_id": 999})

    assert team_sync.load_focus_pending(config) == {}
    assert team_sync.load_focus_pending() == {"x": {"session_id": "x"}}


def test_leave_deletes_unsent_focus_sessions(sync, monkeypatch, invite):
    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    team_sync.save_focus_pending({"x": {"session_id": "x"}}, config)

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert team_sync.main(["leave"]) == 0

    assert not team_sync.focus_pending_file().exists()
    assert not team_sync.focus_pending_lock_file().exists()


def test_list_apps(monkeypatch, capsys):
    import focus.apps

    monkeypatch.setattr(focus.apps, "running_applications", lambda: ["Slack", "Terminal"])
    monkeypatch.setattr(focus.apps, "recent_applications",
                        lambda: ["Terminal", "Visual Studio Code"])

    assert team_sync.main(["focus", "--list-apps"]) == 0

    out = capsys.readouterr().out
    assert "Open now:  Slack, Terminal" in out
    assert "Recent:    Visual Studio Code" in out
    assert '--apps "Slack,Terminal"' in out


def test_final_row_prefers_the_recorded_session():
    live = {
        "session_id": "s", "title": "T", "goal": "G", "started_at": "2026-01-01T10:00:00",
        "planned_s": 1500, "allowed_apps": ALLOWED, "score": 99, "deviations": 0,
        "deviation_seconds": 0, "current_app": "Slack", "allowed": False,
        "remaining_s": 3,
    }
    record = {
        "id": "s", "status": "Completed", "deviations": 2, "deviation_seconds": 61,
        "allowed_seconds": 1400, "score": 84,
        "end": datetime(2026, 1, 1, 10, 25), "apps": ALLOWED,
    }

    row = team_sync.focus_row(live, record, finished={})

    assert row["status"] == "Completed"
    assert row["ended_at"] == "2026-01-01T10:25:00"
    assert (row["deviations"], row["deviation_seconds"], row["score"]) == (2, 61.0, 84)
    assert row["allowed_seconds"] == 1400.0
    assert row["planned_minutes"] == 25
    assert row["current_app"] is None and row["remaining_s"] is None

    running = team_sync.focus_row(live, record)
    assert running["status"] == "running" and running["ended_at"] is None
    # Live numbers win while it runs.
    assert (running["deviations"], running["score"]) == (0, 99)
    assert running["current_app"] == "Slack" and running["current_allowed"] is False
    assert running["remaining_s"] == 3


def test_every_row_the_cli_builds_passes_server_validation():
    from team.api import validate_focus_session

    now = datetime.now().replace(microsecond=0)
    live = {
        "session_id": "s", "title": "t" * 600, "goal": "", "allowed_apps": ["a" * 600],
        "started_at": (now - timedelta(minutes=1)).isoformat(), "planned_s": 60,
        "score": None, "deviations": None, "deviation_seconds": float("nan"),
        "current_app": "c" * 600, "allowed": None, "remaining_s": -4,
    }

    validate_focus_session(team_sync.focus_row(live))
    validate_focus_session(team_sync.focus_row(live, finished={}))
    validate_focus_session(team_sync.focus_row(live, finished={"status": "Weird"}))


# --------------------------------------------------
# The real engine with a fake sampler
# --------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = datetime.now().replace(microsecond=0) - timedelta(minutes=30)

    def __call__(self):
        return self.now


def _real_engine(monkeypatch, apps, notifications, stop_after=None):
    """
    focus/engine.py with a scripted sampler and a clock that moves one
    second per tick. With stop_after, Ctrl+C is pressed after that
    many ticks; otherwise the session runs until it is due.
    """

    engine_module = pytest.importorskip("focus.engine")
    clock = FakeClock()
    samples = list(apps)
    sleeps = []

    def sampler():
        app = samples.pop(0) if len(samples) > 1 else samples[0]
        return app, f"{app} window", None

    def create(data_dir, notifier):
        return engine_module.FocusEngine(
            data_dir=data_dir, sampler=sampler, idle_reader=lambda: 0.0,
            notifier=lambda title, message: notifications.append((title, message)),
            source="member", clock=clock,
        )

    def sleep(_seconds):
        sleeps.append(1)
        if stop_after is not None and len(sleeps) > stop_after:
            raise KeyboardInterrupt
        clock.now += timedelta(seconds=1)

    monkeypatch.setattr(team_sync, "create_focus_engine", create)
    monkeypatch.setattr(team_sync, "focus_sleep", sleep)
    monkeypatch.setattr(team_sync, "FOCUS_UPLOAD_SECONDS", 0)
    return sleeps


def _recorded(session_id):
    from focus import analytics

    [record] = [
        r for r in analytics.session_records(team_sync.focus_data_dir())
        if r["id"] == session_id
    ]
    return record


def test_real_engine_session_is_shared_with_its_recorded_score(
    sync, monkeypatch, invite, db_file, capsys
):
    from focus import scoring

    _join(monkeypatch, invite("Asha"))
    notifications = []
    _real_engine(monkeypatch, [
        "Visual Studio Code", "Visual Studio Code", "Slack", "Slack", "Slack",
        "Terminal", "Visual Studio Code",
    ], notifications, stop_after=8)

    assert focus("--yes") == 0

    out = capsys.readouterr().out
    errors = [line for line in out.splitlines() if line.startswith("ERROR:")]
    assert len(errors) == 1 and "You switched to Slack" in errors[0]
    assert len(notifications) == 1
    assert "Slack" in notifications[0][1]

    [row] = db.focus_sessions(db_file=db_file)
    record = _recorded(row["session_id"])

    assert row["status"] == record["status"] == "Stopped"
    assert row["deviations"] == record["deviations"] == 1
    assert row["deviation_seconds"] == record["deviation_seconds"] == 3
    assert row["allowed_seconds"] == record["allowed_seconds"]
    assert row["score"] == record["score"] == scoring.session_score(1, 3)
    assert row["allowed_apps"] == ALLOWED


def test_real_engine_session_completes_on_time(sync, monkeypatch, invite, db_file, capsys):
    _join(monkeypatch, invite("Asha"))
    sleeps = _real_engine(monkeypatch, ["Visual Studio Code"], [])

    assert focus("--yes", minutes="1") == 0

    assert 60 <= len(sleeps) <= 62
    [row] = db.focus_sessions(db_file=db_file)
    assert row["status"] == "Completed"
    assert row["score"] == 100
    assert row["ended_at"] > row["started_at"]
    assert "Focus session completed" in capsys.readouterr().out


# --------------------------------------------------
# Review fixes: pending file lock and merge, final row kept first,
# signals, Ctrl+C at the prompt
# --------------------------------------------------

def _row(status="running", updated_at="2026-01-01T10:00:00", **extra):
    now = datetime.now().replace(microsecond=0)
    row = {
        "session_id": "s1", "title": "Report", "goal": "", "planned_minutes": 25,
        "allowed_apps": ["Code"], "started_at": (now - timedelta(minutes=5)).isoformat(),
        "ended_at": None if status == "running" else now.isoformat(),
        "status": status, "deviations": 0, "deviation_seconds": 0.0,
        "allowed_seconds": 0.0, "score": 100, "current_app": None,
        "current_allowed": None, "remaining_s": None, "updated_at": updated_at,
    }
    row.update(extra)
    return row


def test_pending_keeps_the_finished_row_over_an_older_running_one(sync, monkeypatch, invite):
    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    final = _row("Completed", "2026-01-01T10:00:00")

    team_sync.keep_focus_rows(config, [final])
    team_sync.keep_focus_rows(config, [_row("running", "2026-01-01T10:00:05")])

    assert team_sync.load_focus_pending(config) == {"s1": final}


def test_row_kept_by_the_other_process_during_an_upload_is_not_lost(
    sync, monkeypatch, invite, db_file
):
    """
    `run` retries an old running row; meanwhile `focus` keeps the final
    row. The final row must stay pending (and be stored), and the old
    running row must not turn the stored session back to running.
    """

    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    now = datetime.now().replace(microsecond=0)
    running = _row("running", (now - timedelta(seconds=20)).isoformat())
    final = _row("Completed", now.isoformat(), score=80, deviations=4)
    team_sync.save_focus_pending({"s1": running}, config)

    real = team_sync.request_json
    raced = []

    def racing(method, url, body=None, token=None, timeout=15):
        if not raced:
            raced.append(True)
            # The `focus` process sends and removes its final row while
            # this (`run`) upload of the old row is in flight.
            other = team_sync.send_focus_rows(config, [final])
            assert other.sent == 1
        return real(method, url, body, token=token, timeout=timeout)

    monkeypatch.setattr(team_sync, "request_json", racing)
    team_sync.send_focus_rows(config)

    [stored] = db.focus_sessions(db_file=db_file)
    assert stored["status"] == "Completed" and stored["score"] == 80
    assert not team_sync.focus_pending_file().exists()


def test_final_row_is_on_disk_before_the_upload_finishes(
    focus_env, monkeypatch, invite, db_file, capsys
):
    """
    The process may exit at "Uploading the session..." (second Ctrl+C,
    a server that never answers): the final row must already be kept.
    """

    _join(monkeypatch, invite("Asha"))

    class StuckUploader:
        revoked = False
        pending = 0
        last_error = None
        uploads = 0

        def __init__(self, config):
            self.config = config

        def submit(self, row):
            pass

        def close(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(team_sync, "FocusUploader", StuckUploader)
    focus_env["script"] += ["Visual Studio Code", "Slack"]

    assert focus("--yes") == 0

    assert "sent the next time" in capsys.readouterr().out
    saved = json.loads(team_sync.focus_pending_file().read_text())
    assert saved["sessions"]["fake-1"]["status"] == "Completed"
    assert db.focus_sessions(db_file=db_file) == []


def test_sigterm_ends_the_session_as_interrupted_and_uploads_it(
    focus_env, monkeypatch, invite, db_file
):
    import signal

    if not hasattr(signal, "SIGTERM") or sys.platform == "win32":
        pytest.skip("POSIX signals only")

    _join(monkeypatch, invite("Asha"))
    focus_env["script"] += ["Visual Studio Code", "Slack", "Visual Studio Code"]
    real_create = team_sync.create_focus_engine
    previous = signal.getsignal(signal.SIGTERM)

    def create(data_dir, notifier):
        engine = real_create(data_dir, notifier)
        real_tick = engine.tick

        def tick(now=None):
            if len(engine.script) == 1:
                os.kill(os.getpid(), signal.SIGTERM)
            return real_tick(now)

        engine.tick = tick
        return engine

    monkeypatch.setattr(team_sync, "create_focus_engine", create)

    assert focus("--yes") == 0

    assert focus_env["engines"][0].stopped_with == "Interrupted"
    [row] = db.focus_sessions(db_file=db_file)
    assert row["status"] == "Interrupted"
    # The handler is put back afterwards.
    assert signal.getsignal(signal.SIGTERM) == previous


def test_ctrl_c_at_the_share_prompt_cancels_cleanly(focus_env, monkeypatch, invite, capsys):
    _join(monkeypatch, invite("Asha"))

    def interrupted(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)

    assert focus() == 130
    assert "Cancelled." in capsys.readouterr().out
    assert focus_env["engines"] == []


def test_pending_edits_wait_for_the_lock(sync, monkeypatch, invite):
    import threading

    if team_sync.fcntl is None:
        pytest.skip("flock only")

    _join(monkeypatch, invite("Asha"))
    config = team_sync.load_config()
    done = threading.Event()

    def other():
        team_sync.keep_focus_rows(config, [_row()])
        done.set()

    with team_sync.focus_pending_lock():
        thread = threading.Thread(target=other)
        thread.start()
        assert not done.wait(0.3)
        assert not team_sync.focus_pending_file().exists()

    thread.join(5)
    assert done.is_set()
    assert list(team_sync.load_focus_pending(config)) == ["s1"]
