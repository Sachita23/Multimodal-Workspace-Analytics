"""
Regression tests for the launcher (start_project.py), the README and CI.

Nothing here runs the real launcher or a real collector: main() runs
with its services replaced by records, and the signal test drives a
copy of main() whose services are sleeping Python processes. All files
go to pytest's tmp_path.
"""

import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

import config
import start_project as sp
from api import server


PROJECT_ROOT = Path(sp.__file__).resolve().parent
README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
CI = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

ADMIN_KEY = "docs-devops-admin-key-0123456789"

# What main() hands its services through the environment.
WATCHED_ENV = (
    "WORKSPACE_API_HOST",
    "WORKSPACE_API_PORT",
    "WORKSPACE_DATA_DIR",
    "WORKSPACE_TEAM_DB",
)

SLEEP = "import time; time.sleep(120)"


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / ".start_project.pid"
    monkeypatch.setattr(sp, "LOCK_FILE", path)
    monkeypatch.setattr(sp, "lock_held", False)
    monkeypatch.setattr(sp, "processes", [])
    return path


@pytest.fixture
def sleeper():
    """
    Start a sleeping Python process (extra args end up in its command
    line); every one is killed after the test.
    """

    started = []

    def start(*extra):
        process = subprocess.Popen([sys.executable, "-c", SLEEP, *extra])
        started.append(process)
        return process

    yield start

    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def _wait_gone(pid, timeout=10):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)

    return False


@pytest.fixture
def launched(lock, monkeypatch, restore_environ):
    """
    Run start_project.main(*flags) with every service replaced by a
    record of its name, command and environment.
    """

    started = []

    class FinishedProcess:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 0

    def fake_start(name, command, cwd=None):
        started.append({
            "name": name,
            "command": list(command),
            "env": {key: os.environ.get(key) for key in WATCHED_ENV},
        })
        process = FinishedProcess(4_000_000 + len(started))
        sp.processes.append((name, process))
        return process

    def fake_sleep(seconds):
        # The keep-alive loop sleeps 2 s: stop there, like Ctrl+C.
        if seconds >= 2:
            raise sp.StopRequested("test")

    monkeypatch.setattr(sp, "start_process", fake_start)
    monkeypatch.setattr(sp, "check_paths", lambda: None)
    monkeypatch.setattr(sp, "ensure_frontend_ready", lambda npm: False)
    monkeypatch.setattr(sp, "wait_for_url", lambda *args, **kwargs: True)
    # Never change pytest's own signal handling.
    monkeypatch.setattr(sp, "install_signal_handlers", lambda: [])
    monkeypatch.setattr(sp, "ignore_stop_signals", lambda: [])
    monkeypatch.setattr(sp.time, "sleep", fake_sleep)

    for key in WATCHED_ENV:
        os.environ.pop(key, None)

    def run(*flags, **env):
        os.environ.update(env)
        started.clear()
        sp.main(list(flags))
        return {entry["name"]: entry for entry in started}

    return run


@pytest.fixture
def api_files(tmp_path, monkeypatch):
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    processed.parent.mkdir(parents=True)
    processed.write_text(
        "timestamp,application,window_title\n"
        "2026-10-01 09:00:00,Mail,Re: medical results - Inbox\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "PROCESSED_DATA_FILE", processed)
    monkeypatch.setattr(server, "RAW_DATA_FILE", tmp_path / "raw" / "workspace_activity.csv")
    monkeypatch.setattr(server, "PIPELINE_STATUS_FILE", tmp_path / "processed" / "status.json")

    return processed


@pytest.fixture
def guarded_app(tmp_path, api_files):
    app = server.create_app(db_file=tmp_path / "team.db", admin_key=ADMIN_KEY)
    app.wsgi_app = sp.MemberEndpointsOnly(app.wsgi_app)
    return app


@pytest.fixture
def served(tmp_path, api_files, monkeypatch):
    """
    Run serve_api() with waitress.serve replaced by a recorder and the
    app built on scratch storage; returns the recorded call.
    """

    import waitress

    calls = {}
    original_create_app = server.create_app

    def fake_serve(app, **options):
        calls["app"] = app
        calls.update(options)

    monkeypatch.setattr(waitress, "serve", fake_serve)
    monkeypatch.setattr(
        server,
        "create_app",
        lambda: original_create_app(db_file=tmp_path / "team.db", admin_key=ADMIN_KEY),
    )

    def run(mode):
        calls.clear()
        sp.serve_api(mode)
        return calls

    return run


# ============================================================
# --team AND --behind-proxy: OTHER COMPUTERS GET MEMBER ENDPOINTS ONLY
# ============================================================

@pytest.mark.parametrize("path", [
    "/api/analytics.csv",
    "/api/status",
    "/api/focus/live",
    "/api/focus/summary",
    "/api/focus/sessions",
    "/api/team/overview",
    "/api/team/live",
    "/api/team/members/1/activity",
    "/api/team/me/",
])
def test_other_computers_cannot_read_the_owners_data(guarded_app, path):
    client = guarded_app.test_client()

    with client.get(path, headers={"X-Admin-Key": ADMIN_KEY},
                    environ_base={"REMOTE_ADDR": "192.168.1.77"}) as response:
        assert response.status_code == 403
        assert b"medical results" not in response.data


def test_members_can_still_join_upload_and_check(guarded_app):
    client = guarded_app.test_client()
    lan = {"REMOTE_ADDR": "192.168.1.77"}

    assert client.get("/api/health", environ_base=lan).status_code == 200
    # These reach the team API, which answers for itself.
    assert client.post("/api/team/join", json={}, environ_base=lan).status_code == 400
    assert client.post("/api/team/ingest", json={}, environ_base=lan).status_code == 401
    assert client.get("/api/team/me", environ_base=lan).status_code == 401
    assert client.delete("/api/team/me", environ_base=lan).status_code == 401


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_this_machine_still_gets_everything(guarded_app, address):
    client = guarded_app.test_client()

    with client.get("/api/analytics.csv", environ_base={"REMOTE_ADDR": address}) as response:
        assert response.status_code == 200
        assert b"medical results" in response.data

    response = client.get("/api/team/overview", headers={"X-Admin-Key": ADMIN_KEY},
                          environ_base={"REMOTE_ADDR": address})
    assert response.status_code == 200


@pytest.mark.parametrize("address, expected", [
    ("127.0.0.1", True),
    ("127.8.9.10", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),
    ("fe80::1%en0", False),
    ("::ffff:192.168.1.77", False),
    ("192.168.1.77", False),
    ("10.0.0.1", False),
    ("", False),
    (None, False),
    ("localhost", False),
    ("not an address", False),
])
def test_is_loopback(address, expected):
    assert sp.is_loopback(address) is expected


def test_member_paths_cover_everything_the_member_collector_calls():
    source = (PROJECT_ROOT / "collector" / "team_sync.py").read_text(encoding="utf-8")
    called = set(re.findall(r"/api/team/[a-z_]+", source))

    assert called, "team_sync.py no longer calls the team API?"
    assert called <= sp.MEMBER_PATHS


def test_team_mode_serves_through_waitress_with_the_guard(served, monkeypatch, capsys):
    monkeypatch.setattr(config, "API_HOST", "0.0.0.0")

    call = served("team")

    assert call["host"] == "0.0.0.0"
    assert call["port"] == config.API_PORT
    assert "trusted_proxy" not in call
    assert isinstance(call["app"].wsgi_app, sp.MemberEndpointsOnly)
    assert not call["app"].debug

    out = capsys.readouterr().out
    assert ADMIN_KEY in out
    assert "/api/team/ingest" in out


def test_behind_proxy_stays_on_loopback_and_sees_visitors(served, monkeypatch):
    # Even if the environment asks for every interface.
    monkeypatch.setattr(config, "API_HOST", "0.0.0.0")

    call = served("proxy")

    assert call["host"] == "127.0.0.1"
    assert call["trusted_proxy"] == "127.0.0.1"
    assert "x-forwarded-for" in call["trusted_proxy_headers"].split()
    assert isinstance(call["app"].wsgi_app, sp.MemberEndpointsOnly)
    assert not call["app"].debug


def test_proxied_errors_never_show_a_traceback(served):
    """
    The README's Caddy recipe used to publish Flask's debug server: an
    unhandled error (deeply nested JSON) returned the Werkzeug
    traceback page with source lines and paths.
    """

    app = served("proxy")["app"]
    client = app.test_client()
    depth = 100_000

    response = client.post(
        "/api/team/join",
        data="[" * depth + "]" * depth,
        content_type="application/json",
        headers={"Host": "team.example.com"},
    )

    assert response.status_code in (400, 500)
    assert b"Traceback" not in response.data
    assert b"Werkzeug Debugger" not in response.data
    assert str(PROJECT_ROOT).encode() not in response.data


# ============================================================
# main(): WHAT EACH FLAG HANDS THE SERVICES
# ============================================================

def test_default_launch(launched, lock):
    started = launched("--no-browser")

    assert list(started) == ["Activity Collector", "Live Analytics Pipeline", "Flask API"]
    assert started["Flask API"]["command"] == [sys.executable, str(sp.API_SERVER)]

    for entry in started.values():
        assert entry["env"]["WORKSPACE_API_PORT"] == "5050"
        assert entry["env"]["WORKSPACE_API_HOST"] is None
        assert entry["env"]["WORKSPACE_TEAM_DB"] is None

    assert not lock.exists()


def test_sandbox_sends_every_service_to_data_demo(launched):
    started = launched("--sandbox", "--focus", "--no-browser")

    assert "Activity Collector" not in started
    assert started["Sandbox Activity Feed"]["command"][1:3] == ["-m", "collector.demo_feed"]

    for entry in started.values():
        assert entry["env"]["WORKSPACE_DATA_DIR"] == str(sp.SANDBOX_DATA_DIR)


def test_team_listens_on_the_network_through_the_guard(launched):
    started = launched("--team", "--no-browser")
    api = started["Flask API"]

    assert api["env"]["WORKSPACE_API_HOST"] == "0.0.0.0"
    assert api["command"][:2] == [sys.executable, "-c"]
    assert "serve_api('team')" in api["command"][2]


def test_behind_proxy_keeps_the_api_on_this_machine(launched):
    started = launched("--behind-proxy", "--no-browser", WORKSPACE_API_HOST="0.0.0.0")
    api = started["Flask API"]

    assert api["env"]["WORKSPACE_API_HOST"] == "127.0.0.1"
    assert "serve_api('proxy')" in api["command"][2]


def test_team_and_behind_proxy_exclude_each_other(capsys):
    with pytest.raises(SystemExit):
        sp.parse_args(["--team", "--behind-proxy"])


def test_serve_api_command_imports_from_the_project_root():
    # The child runs `python -c "import start_project; ..."` from the
    # project root, so that import must find this file.
    args = sp.parse_args(["--team"])

    assert "import start_project" in sp.api_command(args)[2]
    assert (PROJECT_ROOT / "start_project.py").resolve() == Path(sp.__file__).resolve()


# ============================================================
# SETTINGS LEFT IN THE SHELL
# ============================================================

@pytest.mark.parametrize("host", ["0.0.0.0", " 0.0.0.0 ", "::"])
def test_network_host_without_team_still_gets_the_guard(launched, host):
    """
    WORKSPACE_API_HOST=0.0.0.0 with a plain launch used to run
    `python api/server.py` on the network with no filter.
    """

    started = launched("--no-browser", WORKSPACE_API_HOST=host)
    api = started["Flask API"]

    assert api["command"][:2] == [sys.executable, "-c"]
    assert "serve_api('team')" in api["command"][2]
    assert api["env"]["WORKSPACE_API_HOST"] == "0.0.0.0"


def test_network_host_without_team_hides_the_owners_data(launched, api_files, tmp_path,
                                                         monkeypatch):
    import waitress

    served = {}
    original_create_app = server.create_app

    monkeypatch.setattr(waitress, "serve", lambda app, **options: served.update(app=app))
    monkeypatch.setattr(
        server,
        "create_app",
        lambda: original_create_app(db_file=tmp_path / "team.db", admin_key=ADMIN_KEY),
    )

    command = launched("--no-browser", WORKSPACE_API_HOST="0.0.0.0")["Flask API"]["command"]

    # Run what the API's child process would run, in this process, with
    # the environment the launcher gave it.
    host = os.environ["WORKSPACE_API_HOST"]
    monkeypatch.setattr(config, "API_HOST", host)
    monkeypatch.setattr(server, "API_HOST", host)

    if command[1] == "-c":
        exec(command[2], {})
    else:
        assert command[1:] == [str(sp.API_SERVER)]
        server.main()

    client = served["app"].test_client()
    lan = {"REMOTE_ADDR": "192.168.1.77"}

    for path in ("/api/analytics.csv", "/api/status", "/api/focus/live"):
        with client.get(path, environ_base=lan) as response:
            assert response.status_code == 403, path
            assert b"medical results" not in response.data

    assert client.get("/api/health", environ_base=lan).status_code == 200


@pytest.mark.parametrize("host", ["192.168.1.77", "my-laptop.local", "10.0.0.5"])
def test_single_network_address_is_refused(launched, lock, host, capsys):
    with pytest.raises(SystemExit) as stopped:
        launched("--no-browser", WORKSPACE_API_HOST=host)

    assert stopped.value.code == 1
    assert not lock.exists()
    assert sp.processes == []
    assert "--team" in capsys.readouterr().out


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", " ::1 ", "[::1]", "127.0.0.2", "", "  "])
def test_loopback_or_blank_host_keeps_the_plain_api(launched, host):
    started = launched("--no-browser", WORKSPACE_API_HOST=host)
    api = started["Flask API"]

    assert api["command"] == [sys.executable, str(sp.API_SERVER)]
    assert api["env"]["WORKSPACE_API_HOST"] == host


def test_behind_proxy_wins_over_a_network_host(launched):
    started = launched("--behind-proxy", "--no-browser", WORKSPACE_API_HOST="192.168.1.77")

    assert "serve_api('proxy')" in started["Flask API"]["command"][2]
    assert started["Flask API"]["env"]["WORKSPACE_API_HOST"] == "127.0.0.1"


@pytest.mark.parametrize("value, port", [("", "5050"), ("   ", "5050"), (" 5251 ", "5251"), ("05252", "5252")])
def test_blank_or_padded_api_port(launched, monkeypatch, value, port):
    waited = []
    monkeypatch.setattr(sp, "wait_for_url", lambda url, **kwargs: waited.append(url) or True)

    started = launched("--demo", "--no-browser", WORKSPACE_API_PORT=value)

    for entry in started.values():
        assert entry["env"]["WORKSPACE_API_PORT"] == port

    assert waited == [f"http://127.0.0.1:{port}/api/health"]
    assert started["Live Demo Members"]["command"][-2:] == ["--server", f"http://127.0.0.1:{port}"]


@pytest.mark.parametrize("value", ["abc", "0", "70000", "50 50", "-1"])
def test_bad_api_port_is_refused_before_anything_starts(launched, lock, value, capsys):
    with pytest.raises(SystemExit) as stopped:
        launched("--no-browser", WORKSPACE_API_PORT=value)

    assert stopped.value.code == 1
    assert not lock.exists()
    assert "WORKSPACE_API_PORT" in capsys.readouterr().out


@pytest.fixture
def browser(launched, monkeypatch):
    """
    Launch with the dashboard "started"; record what the launcher
    waits for and what it opens.
    """

    record = {"waited": [], "opened": []}

    monkeypatch.setattr(sp, "ensure_frontend_ready", lambda npm: True)
    monkeypatch.setattr(sp, "wait_for_url",
                        lambda url, **kwargs: record["waited"].append(url) or True)
    monkeypatch.setattr(sp.webbrowser, "open", lambda url: record["opened"].append(url))

    def run(**env):
        launched(**env)
        return record

    return run


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_dashboard_url_opens_the_vite_address(browser, capsys, value):
    record = browser(WORKSPACE_DASHBOARD_URL=value)

    assert record["waited"][-1] == sp.VITE_URL == "http://localhost:5173"
    assert record["opened"] == [sp.VITE_URL]
    assert "Launcher failed" not in capsys.readouterr().out


def test_dashboard_url_is_opened_after_waiting_for_vite(browser):
    record = browser(WORKSPACE_DASHBOARD_URL="http://127.0.0.1:5173/focus")

    assert record["waited"][-1] == sp.VITE_URL
    assert record["opened"] == ["http://127.0.0.1:5173/focus"]


@pytest.mark.parametrize("url", ["", "not a url", "localhost:5173"])
def test_wait_for_url_gives_up_on_a_non_url(url):
    started = time.monotonic()

    assert sp.wait_for_url(url, timeout=10) is False
    assert time.monotonic() - started < 5


def test_launcher_reads_settings_like_config():
    os_env = dict(os.environ)

    try:
        os.environ["WORKSPACE_DASHBOARD_URL"] = "  "
        assert sp.dashboard_url() == sp.VITE_URL
        os.environ["WORKSPACE_DASHBOARD_URL"] = " http://example.test:5173 "
        assert sp.dashboard_url() == "http://example.test:5173"
    finally:
        os.environ.clear()
        os.environ.update(os_env)


# ============================================================
# --demo
# ============================================================

def test_demo_streams_into_the_team_database_members_already_use(launched):
    """
    --team --demo used to switch the API to data/team/demo.db on the
    same address: every member who had joined got 401, and `leave`
    (which the 401 message suggests) deleted their queue.
    """

    plain = launched("--team", "--no-browser")
    demo = launched("--team", "--demo", "--no-browser")

    assert demo["Flask API"]["env"] == plain["Flask API"]["env"]
    assert demo["Flask API"]["env"]["WORKSPACE_TEAM_DB"] is None
    assert demo["Live Demo Members"]["command"][-2:] == ["--server", "http://127.0.0.1:5050"]


@pytest.mark.parametrize("chosen", ["data/team/team.db", "/somewhere/else.db", "  "])
def test_demo_never_changes_the_team_database_setting(launched, chosen):
    started = launched("--demo", "--no-browser", WORKSPACE_TEAM_DB=chosen)

    for entry in started.values():
        assert entry["env"]["WORKSPACE_TEAM_DB"] == chosen


def test_members_who_joined_keep_uploading_during_a_demo(launched, tmp_path, monkeypatch):
    """
    The API that --team --demo starts reads the same team database, so
    a member's token from before the demo still works.
    """

    from team import db

    # Where WORKSPACE_TEAM_DB (or its default) points the API.
    team_db = tmp_path / "team.db"
    monkeypatch.setattr(db, "TEAM_DB_FILE", team_db)
    app = server.create_app(admin_key=ADMIN_KEY)
    client = app.test_client()

    invite = client.post("/api/team/invites", json={"name": "Asha"},
                         headers={"X-Admin-Key": ADMIN_KEY}).get_json()
    joined = client.post("/api/team/join", json={
        "invite_code": invite["invite_code"],
        "consent": {"accepted": True, "version": config.TEAM_CONSENT_VERSION,
                    "fields": config.TEAM_SHARED_FIELDS},
    }).get_json()

    started = launched("--team", "--demo", "--no-browser")
    demo_env = started["Flask API"]["env"]["WORKSPACE_TEAM_DB"]

    # The launcher leaves the team database alone, so the restarted API
    # opens the one the member joined.
    assert demo_env is None
    restarted = server.create_app(admin_key=ADMIN_KEY).test_client()
    response = restarted.get("/api/team/me",
                             headers={"Authorization": f"Bearer {joined['token']}"})

    assert response.status_code == 200
    assert [m["name"] for m in db.list_members(team_db)] == ["Asha"]


# ============================================================
# SINGLE INSTANCE LOCK
# ============================================================

def test_lock_from_a_dead_launcher_is_taken_over(lock):
    lock.write_text(f"{_dead_pid()}\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    assert sp.lock_held


def test_running_launcher_is_not_started_twice(lock, sleeper, capsys):
    launcher = sleeper("start_project.py")
    lock.write_text(f"{launcher.pid}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as stopped:
        sp.acquire_lock()

    assert stopped.value.code == 1
    assert sp.read_lock()[0] == launcher.pid
    assert launcher.poll() is None

    out = capsys.readouterr().out
    assert f"already running (launcher PID {launcher.pid})" in out
    # The way out when the user knows no launcher runs.
    assert str(lock) in out
    assert "delete the lock file" in out


def test_lock_whose_pid_now_belongs_to_another_program_is_stale(lock, sleeper):
    other_program = sleeper()
    lock.write_text(f"{other_program.pid}\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    assert other_program.poll() is None


def test_lock_older_than_the_process_with_its_pid_is_stale(lock, sleeper):
    # A launcher-like process that started after the lock was written
    # cannot have written it: the PID was reused.
    newer = sleeper("start_project.py")
    lock.write_text(f"{newer.pid}\n", encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(lock, (an_hour_ago, an_hour_ago))

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()
    assert newer.poll() is None


@pytest.mark.skipif(os.name == "nt", reason="PID 1 is a POSIX init process")
def test_lock_naming_a_system_process_is_stale(lock):
    try:
        if psutil.Process(1).username() == psutil.Process().username():
            pytest.skip("PID 1 runs as this user here")
    except psutil.Error:
        pytest.skip("PID 1 is not visible here")

    lock.write_text("1\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()


def test_unreadable_lock_is_replaced(lock):
    lock.write_text("not a pid\n", encoding="utf-8")

    sp.acquire_lock()

    assert sp.read_lock()[0] == os.getpid()


def test_services_left_by_a_killed_launcher_are_stopped(lock, sleeper, capsys):
    collector = sleeper()
    lock.write_text(f"{_dead_pid()}\n{collector.pid} Activity Collector\n", encoding="utf-8")

    sp.acquire_lock()

    assert collector.wait(timeout=15) is not None
    assert sp.read_lock()[0] == os.getpid()
    assert "Activity Collector" in capsys.readouterr().out


def test_processes_newer_than_the_lock_are_never_stopped(lock, sleeper):
    unrelated = sleeper()
    lock.write_text(f"{_dead_pid()}\n{unrelated.pid} Activity Collector\n", encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(lock, (an_hour_ago, an_hour_ago))

    sp.acquire_lock()

    time.sleep(0.5)
    assert unrelated.poll() is None


def test_lock_records_each_service_and_is_released(lock, tmp_path, capsys):
    sp.acquire_lock()
    process = sp.start_process("Sleeper", [sys.executable, "-c", SLEEP], cwd=tmp_path)

    try:
        launcher, services, _ = sp.read_lock()
        assert launcher == os.getpid()
        assert services == [(process.pid, "Sleeper")]
    finally:
        sp.stop_all()

    assert process.poll() is not None

    sp.release_lock()
    assert not lock.exists()
    assert not sp.lock_held


def test_release_lock_leaves_another_launchers_lock(lock):
    lock.write_text("12345\n", encoding="utf-8")

    sp.release_lock()

    assert lock.read_text(encoding="utf-8") == "12345\n"


# ============================================================
# SIGNALS
# ============================================================

DRIVER = """
import sys
from pathlib import Path

sys.path.insert(0, {root!r})

import start_project as sp

sp.LOCK_FILE = Path({lock!r})
sp.check_paths = lambda: None
sp.ensure_frontend_ready = lambda npm: False
sp.wait_for_url = lambda *args, **kwargs: True

real_start = sp.start_process


def sleeping_service(name, command, cwd=None):
    # Never the real collector, pipeline or API.
    return real_start(name, [sys.executable, "-c", {sleep!r}], cwd={tmp!r})


sp.start_process = sleeping_service
sp.main(["--no-browser"])
print("MAIN RETURNED", flush=True)
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, getattr(signal, "SIGHUP", None)])
def test_kill_stops_every_service_and_removes_the_lock(tmp_path, signum):
    lock = tmp_path / ".start_project.pid"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(
        root=str(PROJECT_ROOT), lock=str(lock), sleep=SLEEP, tmp=str(tmp_path),
    ), encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "WORKSPACE_DATA_DIR": str(tmp_path / "data"),
        "WORKSPACE_TEAM_DB": str(tmp_path / "team.db"),
        "WORKSPACE_API_PORT": "5299",
    })

    launcher = subprocess.Popen(
        [sys.executable, str(driver)], cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    services = []

    try:
        deadline = time.monotonic() + 30

        while time.monotonic() < deadline and len(services) < 3:
            lock_text = lock.read_text(encoding="utf-8") if lock.exists() else ""
            services = [int(line.split()[0]) for line in lock_text.splitlines()[1:]]
            time.sleep(0.2)

        assert len(services) == 3, "the launcher did not start its three services"

        # Let it settle into its keep-alive loop.
        time.sleep(1)
        launcher.send_signal(signum)
        out, _ = launcher.communicate(timeout=60)

        assert launcher.returncode == 0, out
        assert f"{signal.Signals(signum).name} received." in out
        assert "All services stopped." in out
        assert "MAIN RETURNED" in out
        assert not lock.exists()

        for pid in services:
            assert _wait_gone(pid), f"service PID {pid} is still running"

    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.communicate(timeout=10)

        for pid in services:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def test_signal_handlers_raise_stop_requested():
    """
    In a child process, so pytest's own handlers stay untouched.
    """

    code = (
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "import start_project as sp\n"
        "installed = sp.install_signal_handlers()\n"
        "assert 'SIGTERM' in installed, installed\n"
        "try:\n"
        "    signal.raise_signal(signal.SIGTERM)\n"
        "    time.sleep(5)\n"
        "except sp.StopRequested as stop:\n"
        "    print('stopped by', stop)\n"
        "ignored = sp.ignore_stop_signals()\n"
        "assert 'SIGINT' in ignored and 'SIGTERM' in ignored, ignored\n"
        "signal.raise_signal(signal.SIGTERM)\n"
        "print('still alive')\n"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "stopped by SIGTERM" in result.stdout
    assert "still alive" in result.stdout


# ============================================================
# PYTHON VERSION
# ============================================================

def test_old_python_gets_a_clear_message(capsys):
    with pytest.raises(SystemExit) as stopped:
        sp.check_python_version((3, 9, 6))

    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "Python 3.9" in out
    assert "3.12 or newer" in out


@pytest.mark.parametrize("version", [(3, 12, 0), (3, 14, 8), (4, 0)])
def test_supported_python_passes(version):
    assert sp.check_python_version(version) is None


def test_launcher_stays_parseable_on_python_39():
    source = (PROJECT_ROOT / "start_project.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))


def _old_python():
    for candidate in ("python3.9", "python3.10", "python3.11", "/usr/bin/python3"):
        path = shutil.which(candidate)

        if not path:
            continue

        try:
            version = subprocess.run(
                [path, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                capture_output=True, text=True, timeout=30,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            continue

        if len(version) == 2 and (int(version[0]), int(version[1])) < sp.MIN_PYTHON:
            return path

    return None


def test_old_python_is_stopped_before_anything_starts(tmp_path):
    old = _old_python()

    if old is None:
        pytest.skip("no Python older than 3.12 on this machine")

    # A copy in tmp_path, so even a broken check could not reach the
    # real project's data.
    copy = tmp_path / "start_project.py"
    shutil.copy(PROJECT_ROOT / "start_project.py", copy)

    result = subprocess.run([old, "-I", str(copy)], cwd=str(tmp_path),
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "3.12 or newer" in result.stdout
    assert not (tmp_path / "data").exists()


# ============================================================
# README AND CI
# ============================================================

def test_readme_documents_every_launcher_option():
    options = [
        option
        for action in sp.build_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    ]

    assert "--sandbox" in options
    missing = [option for option in options if f"start_project.py {option}" not in README
               and f"`{option}`" not in README]
    assert not missing


def test_readme_has_no_stale_absolute_path_advice():
    assert "Use an absolute path" not in README
    assert 'export WORKSPACE_DATA_DIR' not in README
    # Relative paths are taken from the project root (config.py).
    assert config._project_path("data/demo") == config.PROJECT_ROOT / "data" / "demo"


def test_readme_caddy_recipe_uses_the_safe_launch():
    https = README.split("## HTTPS for team mode", 1)[1].split("\n## ", 1)[0]

    assert "--behind-proxy" in https
    assert "/api/team/ingest" in https
    assert "start the platform without `--team`" not in https


def test_readme_warns_what_team_mode_opens():
    for path in sorted(sp.MEMBER_PATHS):
        assert path in README


FRONTEND = PROJECT_ROOT / "frontend" / "Multimodal-Workspace-Analytics" / "frontend"


def test_dashboard_loads_no_web_fonts():
    """
    The README says the dashboard loads nothing from the internet; the
    Google Fonts @import used to contact fonts.googleapis.com.
    """

    sources = [FRONTEND / "index.html"] + sorted((FRONTEND / "src").rglob("*.css"))

    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, path
        assert "fonts.gstatic.com" not in text, path
        assert not re.search(r"@import\s+url\(\s*[\"']?https?:", text), path

    css = (FRONTEND / "src" / "index.css").read_text(encoding="utf-8")
    assert "system-ui" in css
    assert "ui-monospace" in css

    assert "Google Fonts" not in README
    assert "loads nothing from\nthe internet" in README


def test_readme_quick_start_checks_the_python_version():
    quick_start = README.split("### macOS and Linux", 1)[1].split("### Windows", 1)[0]

    assert "python3.12 -m venv .venv" in quick_start
    assert "python --version" in quick_start
    assert "python3 --version" in quick_start


def test_readme_describes_demo_members_as_real_team_neighbours():
    demo = README.split("### Demo data", 1)[1].split("\n## ", 1)[0]

    assert "python -m team.live_demo --remove" in demo
    assert "**Demo**" in demo
    assert "never affect your real members'" in demo
    # The old separate-database behaviour is gone.
    assert "With `--demo` the server uses a separate team database" not in README
    assert "data/team/demo.db` unless" not in README


def test_readme_describes_the_network_access_rule():
    assert "X-Admin-Key" in README
    assert "WORKSPACE_API_DEBUG" in README
    assert "has no such filter" not in README
    assert "runs Flask's\n  debug server" not in README


# ============================================================
# --sandbox --focus
# ============================================================

def test_sandbox_help_says_the_focus_assistant_still_watches():
    sandbox = next(action for action in sp.build_parser()._actions
                   if "--sandbox" in action.option_strings)

    assert "nothing is collected" not in sandbox.help
    assert "Focus Assistant" in sandbox.help
    assert "window titles" in sandbox.help

    demo = README.split("### Demo data", 1)[1].split("\n## ", 1)[0]
    assert "python start_project.py --sandbox --focus" in demo
    assert "still watches this computer" in demo


# ============================================================
# DASHBOARD PACKAGES
# ============================================================

def _write_lock(path, packages):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")


WANTED = {
    "": {"name": "workspace-analytics-dashboard", "version": "1.0.0"},
    "node_modules/vite": {"version": "8.3.0", "dev": True},
    "node_modules/react": {"version": "19.2.8"},
    "node_modules/@oxlint/binding-win32-x64-msvc": {"version": "1.81.0", "dev": True,
                                                    "optional": True},
}

INSTALLED = {key: value for key, value in WANTED.items()
             if key and "win32" not in key}


@pytest.fixture
def frontend(tmp_path, monkeypatch):
    """
    A scratch dashboard folder; npm ci is recorded, never run.
    """

    folder = tmp_path / "frontend"
    folder.mkdir()
    _write_lock(folder / "package-lock.json", WANTED)

    calls = []

    def fake_run(command, cwd=None, check=False):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, fake_run.returncode)

    fake_run.returncode = 0

    monkeypatch.setattr(sp, "FRONTEND_DIR", folder)
    monkeypatch.setattr(sp.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sp.subprocess, "run", fake_run)

    def install(packages=INSTALLED, vite=True):
        modules = folder / "node_modules"
        modules.mkdir(exist_ok=True)

        if packages is not None:
            _write_lock(modules / ".package-lock.json", packages)

        if vite:
            (modules / ".bin").mkdir(exist_ok=True)
            (modules / ".bin" / "vite").write_text("", encoding="utf-8")

    return folder, calls, install, fake_run


def test_complete_install_is_used_as_is(frontend):
    _, calls, install, _ = frontend
    install()

    assert sp.frontend_packages_installed()
    assert sp.ensure_frontend_ready("npm") is True
    assert calls == []


def test_first_run_installs(frontend):
    _, calls, _, _ = frontend

    assert sp.ensure_frontend_ready("npm") is True
    assert calls == [["npm", "ci"]]


def test_interrupted_npm_ci_is_installed_again(frontend, capsys):
    """
    npm ci empties node_modules first and writes .package-lock.json
    last; a Ctrl+C in between used to leave a folder the launcher
    trusted forever (`sh: vite: command not found`).
    """

    folder, calls, install, _ = frontend
    install(packages=None, vite=False)
    (folder / "node_modules" / "react").mkdir()

    assert not sp.frontend_packages_installed()
    assert sp.ensure_frontend_ready("npm") is True
    assert calls == [["npm", "ci"]]
    assert "incomplete or out of date" in capsys.readouterr().out


def test_missing_vite_binary_is_installed_again(frontend):
    _, calls, install, _ = frontend
    install(vite=False)

    assert sp.ensure_frontend_ready("npm") is True
    assert calls == [["npm", "ci"]]


def test_changed_package_lock_is_installed_again(frontend):
    # e.g. after a `git pull` that upgrades vite.
    folder, calls, install, _ = frontend
    install()
    _write_lock(folder / "package-lock.json",
                dict(WANTED, **{"node_modules/vite": {"version": "8.4.0", "dev": True}}))

    assert sp.ensure_frontend_ready("npm") is True
    assert calls == [["npm", "ci"]]


def test_new_package_in_the_lock_is_installed(frontend):
    folder, calls, install, _ = frontend
    install()
    _write_lock(folder / "package-lock.json",
                dict(WANTED, **{"node_modules/papaparse": {"version": "5.7.0"}}))

    assert not sp.frontend_packages_installed()


def test_failed_npm_ci_is_reported(frontend, capsys):
    _, calls, _, fake_run = frontend
    fake_run.returncode = 1

    assert sp.ensure_frontend_ready("npm") is False
    assert "npm ci failed" in capsys.readouterr().out


def test_without_npm_the_dashboard_is_skipped(frontend, monkeypatch, capsys):
    _, calls, _, _ = frontend
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)

    assert sp.ensure_frontend_ready("npm") is False
    assert calls == []
    assert "npm was not found" in capsys.readouterr().out


def test_the_real_install_check_reads_npm_files_only():
    # On this checkout: no exception, whatever the state of node_modules.
    assert sp.frontend_packages_installed() in (True, False)


# ============================================================
# .gitignore
# ============================================================

def _ignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=str(PROJECT_ROOT), capture_output=True, timeout=30,
    )

    if result.returncode not in (0, 1):
        pytest.skip(f"git check-ignore failed: {result.stderr!r}")

    return result.returncode == 0


@pytest.fixture
def git():
    if shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        pytest.skip("not a git checkout")


@pytest.mark.parametrize("path", [
    "team.db",
    "demo.db",
    "team/demo.db",
    "team.db-wal",
    "scratch/members.sqlite3",
    "sandbox/raw/workspace_activity.csv",
    "sandbox/raw/workspace_activity.2026-10-01.csv",
    "sandbox/processed/workspace_activity_processed.csv",
    "sandbox/processed/status.json",
    "sandbox/focus/focus_events.csv",
    "sandbox/focus/live.json",
    "member-home/team.json",
    "data/raw/workspace_activity.csv",
    "data/team/admin_key",
])
def test_personal_data_outside_data_is_ignored(git, path):
    """
    config takes relative WORKSPACE_DATA_DIR / WORKSPACE_TEAM_DB values
    from the project root, so they can land outside data/.
    """

    assert _ignored(path)


@pytest.mark.parametrize("path", [
    "data/sample/workspace_activity_sample.csv",
    "team/db.py",
    "focus/analytics.py",
    "collector/demo_feed.py",
    "frontend/Multimodal-Workspace-Analytics/frontend/src/data/team.js",
    "frontend/Multimodal-Workspace-Analytics/frontend/package-lock.json",
])
def test_source_and_sample_files_are_not_ignored(git, path):
    assert not _ignored(path)


# ============================================================
# CI
# ============================================================

def test_ci_runs_every_step_in_bash():
    """
    On windows-latest the default shell is pwsh, where only the last
    command of a multi-line step decides its result: a broken
    `import collector.team_sync` passed as long as collector.py --help
    worked.
    """

    assert re.search(r"^defaults:\n  run:\n    shell: bash\n", CI, re.M)

    collector_job = CI.split("\n  collector:", 1)[1].split("\n  frontend:", 1)[0]
    step = collector_job.split("- name: Import the collector and show its help", 1)[1]
    assert re.search(r"^\s+shell: bash$", step.split("run: |", 1)[0], re.M)
    assert "import collector.collector, collector.team_sync" in step
    assert "python collector/team_sync.py --help" in step


def test_ci_checks_that_help_never_starts_the_scripts():
    python_job = CI.split("\n  python:", 1)[1].split("\n  collector:", 1)[0]
    step = python_job.split("- name: Scripts answer --help without starting", 1)[1]

    assert "timeout-minutes:" in step
    for script in ("api/server.py", "run_pipeline.py", "live_pipeline.py", "start_project.py"):
        assert f"python {script} --help" in step


def test_ci_runs_the_member_collector_where_members_run_it():
    collector_job = CI.split("\n  collector:", 1)[1].split("\n  frontend:", 1)[0]

    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in collector_job
    assert '"3.9"' in collector_job
    assert "requirements-collector.txt" in collector_job
