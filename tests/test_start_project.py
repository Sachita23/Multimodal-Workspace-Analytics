"""
The launcher's options, its single-instance lock and what main() hands
each service.

Nothing is ever launched: main() runs with start_process replaced by a
recorder, and the lock lives in tmp_path. The only real processes are
short-lived `python -c` sleepers that stand in for other programs.
"""

import os
import subprocess
import sys

import pytest

import start_project as sp


# What main() passes to its services through the environment.
WATCHED_ENV = (
    "WORKSPACE_API_HOST",
    "WORKSPACE_API_PORT",
    "WORKSPACE_DATA_DIR",
    "WORKSPACE_TEAM_DB",
)


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / ".start_project.pid"
    monkeypatch.setattr(sp, "LOCK_FILE", path)
    monkeypatch.setattr(sp, "lock_held", False)
    monkeypatch.setattr(sp, "processes", [])
    return path


@pytest.fixture
def other_program():
    """
    A running process that is not a launcher.
    """

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield process

    process.kill()
    process.wait(timeout=10)


@pytest.fixture
def saved_environ():
    """
    main() sets variables for its services in os.environ; put the
    environment back afterwards.
    """

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def launch(lock, monkeypatch, saved_environ):
    """
    main(*flags) with every service recorded instead of started;
    returns {service name: {"command": [...], "env": {...}}}.
    """

    started = {}

    class Finished:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 0

    def record(name, command, cwd=None):
        started[name] = {
            "command": list(command),
            "env": {key: os.environ.get(key) for key in WATCHED_ENV},
        }
        process = Finished(4_100_000 + len(started))
        sp.processes.append((name, process))
        return process

    def stop_at_keep_alive(seconds):
        # The keep-alive loop sleeps 2 s: end there, like Ctrl+C.
        if seconds >= 2:
            raise sp.StopRequested("test")

    monkeypatch.setattr(sp, "start_process", record)
    monkeypatch.setattr(sp, "check_paths", lambda: None)
    monkeypatch.setattr(sp, "ensure_frontend_ready", lambda npm: False)
    monkeypatch.setattr(sp, "wait_for_url", lambda *args, **kwargs: True)
    monkeypatch.setattr(sp, "install_signal_handlers", lambda: [])
    monkeypatch.setattr(sp, "ignore_stop_signals", lambda: [])
    monkeypatch.setattr(sp.time, "sleep", stop_at_keep_alive)

    for key in WATCHED_ENV:
        os.environ.pop(key, None)

    def run(*flags):
        started.clear()
        sp.main(list(flags))
        return dict(started)

    return run


# ============================================================
# parse_args
# ============================================================

def test_parse_args_defaults():
    args = sp.parse_args([])

    for option in ("team", "behind_proxy", "demo", "focus", "sandbox", "no_browser"):
        assert getattr(args, option) is False


def test_parse_args_flags():
    args = sp.parse_args(["--team", "--demo", "--focus", "--sandbox", "--no-browser"])

    assert args.team and args.demo and args.focus and args.sandbox and args.no_browser
    assert not args.behind_proxy


def test_parse_args_team_and_behind_proxy_exclude_each_other():
    with pytest.raises(SystemExit) as stopped:
        sp.parse_args(["--team", "--behind-proxy"])

    assert stopped.value.code == 2


def test_parse_args_help_exits_without_starting(capsys):
    with pytest.raises(SystemExit) as stopped:
        sp.parse_args(["--help"])

    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "--sandbox" in out and "--demo" in out


# ============================================================
# acquire_lock
# ============================================================

def test_lock_with_a_dead_pid_is_stale(lock):
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)
    lock.write_text(f"{finished.pid}\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    assert sp.lock_held


def test_lock_with_a_live_program_that_is_not_a_launcher_is_stale(lock, other_program):
    lock.write_text(f"{other_program.pid}\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    # Not a service the lock recorded, so it is left alone.
    assert other_program.poll() is None


def test_lock_with_our_own_pid_is_simply_rewritten(lock):
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    assert sp.lock_held


def test_live_launcher_is_refused_and_the_lock_path_printed(lock, capsys):
    launcher = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "start_project.py"]
    )

    try:
        lock.write_text(f"{launcher.pid}\n", encoding="utf-8")

        with pytest.raises(SystemExit) as stopped:
            sp.acquire_lock()

        assert stopped.value.code == 1
        assert str(lock) in capsys.readouterr().out
        assert sp.read_lock()[0] == launcher.pid

    finally:
        launcher.kill()
        launcher.wait(timeout=10)


# ============================================================
# main(): WHAT THE SERVICES GET
# ============================================================

def test_main_sandbox(launch, lock):
    started = launch("--sandbox", "--no-browser")

    assert list(started) == ["Sandbox Activity Feed", "Live Analytics Pipeline", "Flask API"]
    assert started["Sandbox Activity Feed"]["command"][1:3] == ["-m", "collector.demo_feed"]

    for entry in started.values():
        assert entry["env"]["WORKSPACE_DATA_DIR"] == str(sp.SANDBOX_DATA_DIR)

    assert started["Flask API"]["command"] == [sys.executable, str(sp.API_SERVER)]
    assert not lock.exists()


def test_main_team(launch):
    started = launch("--team", "--no-browser")
    api = started["Flask API"]

    assert "Activity Collector" in started
    assert api["env"]["WORKSPACE_API_HOST"] == "0.0.0.0"
    assert api["command"][:2] == [sys.executable, "-c"]
    assert "serve_api('team')" in api["command"][2]


def test_main_demo(launch):
    os.environ["WORKSPACE_TEAM_DB"] = "data/team/team.db"

    started = launch("--demo", "--no-browser")
    demo = started["Live Demo Members"]

    assert demo["command"][1:3] == ["-m", "team.live_demo"]
    assert demo["command"][-2:] == ["--server", "http://127.0.0.1:5050"]

    # Demo members go into the database the server already uses.
    for entry in started.values():
        assert entry["env"]["WORKSPACE_TEAM_DB"] == "data/team/team.db"
        assert entry["env"]["WORKSPACE_API_PORT"] == "5050"
