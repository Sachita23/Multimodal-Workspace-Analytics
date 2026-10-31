"""
Who may read the main user's own data from api/server.py, when Flask's
debug mode is allowed, and that --help never starts the API or the
pipelines.

Nothing here starts a server or a pipeline: apps are built on files in
tmp_path, main() runs with app.run / waitress.serve replaced by
recorders, and --help is checked through parse_args().
"""

import ast
import sys
from pathlib import Path

import pytest

import live_pipeline
import run_pipeline
from api import server


PROJECT_ROOT = Path(server.__file__).resolve().parent.parent

ADMIN_KEY = "server-access-admin-key-0123456789"
SECRET_TITLE = "Re: medical results - Inbox"

LAN = {"REMOTE_ADDR": "192.168.1.77"}

OWN_DATA_PATHS = [
    "/api/analytics.csv",
    "/api/status",
    "/api/focus/live",
    "/api/focus/summary",
    "/api/focus/sessions",
]


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def files(tmp_path, monkeypatch):
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    processed.parent.mkdir(parents=True)
    processed.write_text(
        "timestamp,application,window_title\n"
        f"2026-10-01 09:00:00,Mail,{SECRET_TITLE}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "PROCESSED_DATA_FILE", processed)
    monkeypatch.setattr(server, "RAW_DATA_FILE", tmp_path / "raw" / "workspace_activity.csv")
    monkeypatch.setattr(server, "PIPELINE_STATUS_FILE", tmp_path / "processed" / "status.json")

    return tmp_path


@pytest.fixture
def make_app(files, monkeypatch):
    """
    create_app() on scratch storage, as if WORKSPACE_API_HOST were `host`.
    """

    def build(host):
        monkeypatch.setattr(server, "API_HOST", host)
        app = server.create_app(db_file=files / "team.db", admin_key=ADMIN_KEY)
        app.config["FOCUS_DATA_DIR"] = str(files / "focus")
        return app

    return build


@pytest.fixture
def network_client(make_app):
    return make_app("0.0.0.0").test_client()


# ============================================================
# LISTENING ON THE NETWORK: OWN DATA ONLY FOR THIS MACHINE OR THE KEY
# ============================================================

@pytest.mark.parametrize("path", OWN_DATA_PATHS + ["/api/status/", "/api/unknown", "/"])
def test_other_computers_without_the_key_get_403(network_client, path):
    with network_client.get(path, environ_base=LAN) as response:
        assert response.status_code == 403
        assert "error" in response.get_json()
        assert SECRET_TITLE.encode() not in response.data


@pytest.mark.parametrize("key", ["", "wrong-key-wrong-key-wrong", ADMIN_KEY[:-1], ADMIN_KEY + "x"])
def test_a_wrong_or_empty_key_is_not_enough(network_client, key):
    with network_client.get("/api/analytics.csv", headers={"X-Admin-Key": key},
                            environ_base=LAN) as response:
        assert response.status_code == 403
        assert SECRET_TITLE.encode() not in response.data


def test_the_admin_key_opens_own_data_to_other_computers(network_client):
    headers = {"X-Admin-Key": ADMIN_KEY}

    with network_client.get("/api/analytics.csv", headers=headers, environ_base=LAN) as response:
        assert response.status_code == 200
        assert SECRET_TITLE.encode() in response.data

    assert network_client.get("/api/status", headers=headers, environ_base=LAN).status_code == 200
    assert network_client.get("/api/focus/live", headers=headers, environ_base=LAN).status_code == 200


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.5", "::1", "::ffff:127.0.0.1"])
def test_this_machine_reads_everything_without_a_key(network_client, address):
    # The dashboard reaches the API through Vite's proxy on 127.0.0.1.
    local = {"REMOTE_ADDR": address}

    with network_client.get("/api/analytics.csv", environ_base=local) as response:
        assert response.status_code == 200
        assert SECRET_TITLE.encode() in response.data

    for path in ("/api/status", "/api/focus/live", "/api/focus/summary", "/api/focus/sessions"):
        assert network_client.get(path, environ_base=local).status_code == 200, path


def test_health_stays_open_and_holds_no_personal_data(network_client):
    response = network_client.get("/api/health", environ_base=LAN)

    assert response.status_code == 200
    assert set(response.get_json()) <= {"status", "processed_file_exists", "version"}
    assert SECRET_TITLE.encode() not in response.data


def test_team_endpoints_keep_their_own_auth(network_client):
    # Members reach join/ingest/me; the team API checks tokens itself.
    assert network_client.post("/api/team/join", json={}, environ_base=LAN).status_code == 400
    assert network_client.post("/api/team/ingest", json={}, environ_base=LAN).status_code == 401
    assert network_client.get("/api/team/me", environ_base=LAN).status_code == 401

    # Admin endpoints: the admin key, as before.
    assert network_client.get("/api/team/overview", environ_base=LAN).status_code == 401
    response = network_client.get("/api/team/overview", headers={"X-Admin-Key": ADMIN_KEY},
                                  environ_base=LAN)
    assert response.status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "my-laptop.local"])
def test_every_non_loopback_host_turns_the_guard_on(make_app, host):
    assert make_app(host).config["LOCAL_DATA_ONLY"] is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", " 127.0.0.1 ", "127.0.0.2"])
def test_loopback_hosts_need_no_guard(make_app, host):
    # Nothing from another computer can arrive there (a proxy on this
    # machine is what start_project.py --behind-proxy is for).
    app = make_app(host)

    assert app.config["LOCAL_DATA_ONLY"] is False
    assert app.test_client().get("/api/status", environ_base=LAN).status_code == 200


@pytest.mark.parametrize("address, expected", [
    ("127.0.0.1", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),
    ("192.168.1.77", False),
    ("::ffff:192.168.1.77", False),
    ("fe80::1%en0", False),
    ("", False),
    (None, False),
    ("garbage", False),
])
def test_is_loopback_address(address, expected):
    assert server.is_loopback_address(address) is expected


# ============================================================
# DEBUG MODE
# ============================================================

@pytest.fixture
def run_main(files, monkeypatch, capsys):
    """
    Run server.main() for `host` with app.run and waitress.serve
    recorded instead of serving.
    """

    import waitress

    calls = []
    original_create_app = server.create_app

    def build():
        return original_create_app(db_file=files / "team.db", admin_key=ADMIN_KEY)

    def fake_run(self, **options):
        calls.append(("flask", options))

    def fake_serve(app, **options):
        calls.append(("waitress", options))

    monkeypatch.setattr(server, "create_app", build)
    monkeypatch.setattr(server.Flask, "run", fake_run)
    monkeypatch.setattr(waitress, "serve", fake_serve)
    monkeypatch.delenv(server.DEBUG_ENV, raising=False)

    def run(host, debug_env=None):
        monkeypatch.setattr(server, "API_HOST", host)

        if debug_env is not None:
            monkeypatch.setenv(server.DEBUG_ENV, debug_env)

        calls.clear()
        server.main()
        return calls[-1], capsys.readouterr().out

    return run


def test_debug_mode_is_off_by_default(run_main):
    (kind, options), out = run_main("127.0.0.1")

    assert kind == "flask"
    assert options["debug"] is False
    assert "debug mode is ON" not in out


def test_debug_mode_needs_the_variable_and_a_loopback_host(run_main):
    (kind, options), out = run_main("127.0.0.1", "1")

    assert kind == "flask"
    assert options["debug"] is True
    assert "debug mode is ON" in out


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_only_the_value_1_turns_debug_on(run_main, value):
    (_, options), _ = run_main("127.0.0.1", value)

    assert options["debug"] is False


def test_debug_variable_is_ignored_on_the_network(run_main):
    (kind, options), out = run_main("0.0.0.0", "1")

    assert kind == "waitress"
    assert options["host"] == "0.0.0.0"
    assert "is ignored" in out


def test_network_without_waitress_still_has_no_debug(run_main, monkeypatch):
    monkeypatch.setitem(sys.modules, "waitress", None)

    (kind, options), _ = run_main("0.0.0.0", "1")

    assert kind == "flask"
    assert options["debug"] is False


# ============================================================
# --help NEVER STARTS ANYTHING
# ============================================================

@pytest.mark.parametrize("module, words", [
    (server, "Workspace Analytics API"),
    (run_pipeline, "once"),
    (live_pipeline, "every"),
])
def test_help_prints_a_description_and_exits(module, words, capsys):
    with pytest.raises(SystemExit) as stopped:
        module.parse_args(["--help"])

    assert stopped.value.code == 0
    assert words in capsys.readouterr().out


@pytest.mark.parametrize("module", [server, run_pipeline, live_pipeline])
def test_unknown_options_are_refused(module, capsys):
    with pytest.raises(SystemExit) as stopped:
        module.parse_args(["--bogus"])

    assert stopped.value.code == 2


@pytest.mark.parametrize("relative", ["api/server.py", "run_pipeline.py", "live_pipeline.py"])
def test_main_block_parses_arguments_before_running(relative):
    """
    `python <script> --help` exits in parse_args(), before main() could
    start a server or a pipeline.
    """

    tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))

    blocks = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and getattr(node.test.left, "id", None) == "__name__"
    ]
    assert len(blocks) == 1

    calls = [
        node.value.func.id
        for node in blocks[0].body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]

    assert calls[:2] == ["parse_args", "main"]
