import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from collector.team_sync import ServerError
from config import PROJECT_ROOT, TEAM_DATA_DIR
from team import live_demo
from team.api import validate_observation
from team.live_demo import DemoMember
from team.simulate import PROFILES
from tests.conftest import ADMIN_KEY, consent


def _stream(profile_name="Eli (context switcher)", seed=1):
    # Recent, because the server rejects samples older than 90 days.
    now = (datetime.now() - timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return DemoMember(profile_name, PROFILES[profile_name], seed, now), now


def test_samples_pass_server_validation():
    member, now = _stream()

    for step in range(500):
        row = member.sample(now + timedelta(seconds=5 * step), 5)
        if row is not None:
            assert validate_observation(row) is not None, row


def test_members_take_breaks_and_come_back():
    member, now = _stream("Asha (focused dev)")
    states = [
        member.sample(now + timedelta(seconds=5 * step), 5) is not None
        for step in range(12 * 120)  # two hours
    ]

    assert states[0] is True
    assert False in states, "never took a break"
    first_break = states.index(False)
    assert True in states[first_break:], "never came back from the break"


def test_switcher_switches_more_than_focused_dev():
    def switches(profile_name):
        member, now = _stream(profile_name, seed=3)
        apps = [
            row["application"]
            for step in range(12 * 60)
            if (row := member.sample(now + timedelta(seconds=5 * step), 5))
        ]
        return sum(a != b for a, b in zip(apps, apps[1:]))

    assert switches("Eli (context switcher)") > 3 * switches("Asha (focused dev)")


# --------------------------------------------------
# Talking to a server (a Flask test client stands in for HTTP)
# --------------------------------------------------

SERVER = "http://demo-server.test:5299"
OTHER_SERVER = "http://other-server.test:5298"


class _Response:
    def __init__(self, response):
        self.status = response.status_code
        self._body = response.data

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def demo_server(client, monkeypatch, tmp_path):
    """
    Route live_demo's HTTP calls to the test app, and keep its state
    file and admin key away from data/team.
    """

    def urlopen(req, timeout=None):
        parts = urllib.parse.urlsplit(req.full_url)
        assert f"{parts.scheme}://{parts.netloc}" == SERVER, req.full_url
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        response = client.open(
            path, method=req.get_method(), data=req.data,
            headers=dict(req.header_items()),
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, response.status, {},
                io.BytesIO(response.data),
            )
        return _Response(response)

    def request_json(method, url, body=None, token=None, timeout=15):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise ServerError(error.code, error.reason) from None

    monkeypatch.setattr(live_demo.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(live_demo, "request_json", request_json)
    monkeypatch.setattr(live_demo, "STATE_FILE", tmp_path / "live_demo.json")
    monkeypatch.setattr(live_demo, "TEAM_ADMIN_KEY_FILE", tmp_path / "admin_key")
    monkeypatch.setenv("WORKSPACE_TEAM_ADMIN_KEY", ADMIN_KEY)
    # One demo profile keeps the history uploads small.
    monkeypatch.setattr(
        live_demo, "PROFILES", {"Asha (focused dev)": PROFILES["Asha (focused dev)"]}
    )
    return client


def _join(client, name, simulated=False):
    invite = client.post(
        "/api/team/invites", json={"name": name, "simulated": simulated},
        headers={"X-Admin-Key": ADMIN_KEY},
    ).get_json()
    joined = client.post(
        "/api/team/join",
        json={"invite_code": invite["invite_code"], "consent": consent()},
    ).get_json()
    return joined["member_id"], joined["token"]


def _members(client):
    live = client.get("/api/team/live", headers={"X-Admin-Key": ADMIN_KEY}).get_json()
    return {m["name"]: m["simulated"] for m in live["members"]}


def _save_state(members):
    live_demo.save_state({"members": members})


def test_remove_deletes_demo_members_but_never_real_ones(demo_server, capsys):
    _join(demo_server, "Real Rita")
    _join(demo_server, "Demo · Priya")  # a real person, whatever the name
    _join(demo_server, "Demo · Sim", simulated=True)

    # Created by an earlier demo that did not flag its members: only
    # its token, kept in the state file, identifies it.
    old_id, old_token = _join(demo_server, "Demo · Asha (focused dev)")
    other = {"name": "Demo · X", "member_id": 1, "token": "elsewhere",
             "server": OTHER_SERVER, "seed": 0}
    _save_state([
        {"name": "Demo · Asha (focused dev)", "member_id": old_id,
         "token": old_token, "server": SERVER, "seed": 0},
        other,
    ])

    assert live_demo.main(["--server", SERVER, "--remove"]) == 0

    assert _members(demo_server) == {"Real Rita": False, "Demo · Priya": False}
    out = capsys.readouterr().out
    assert "Removed 2 demo members" in out
    assert "Demo · Priya" in out and "--include-legacy-names" in out

    # Only this server's entries leave the state file.
    assert live_demo.load_state()["members"] == [other]


def test_remove_legacy_names_only_after_confirmation(demo_server, monkeypatch):
    _join(demo_server, "Real Rita")
    _join(demo_server, "Demo · Priya")

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    live_demo.main(["--server", SERVER, "--remove", "--include-legacy-names"])
    assert _members(demo_server) == {"Real Rita": False, "Demo · Priya": False}

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    live_demo.main(["--server", SERVER, "--remove", "--include-legacy-names"])
    assert _members(demo_server) == {"Real Rita": False}

    _join(demo_server, "Demo · Old")

    def no_prompt(prompt):
        raise AssertionError("--yes must not ask")

    monkeypatch.setattr("builtins.input", no_prompt)
    live_demo.main(["--server", SERVER, "--remove", "--include-legacy-names", "--yes"])
    assert _members(demo_server) == {"Real Rita": False}


def test_ensure_members_reuses_and_recreates_demo_members(demo_server, capsys):
    first = live_demo.ensure_members(SERVER, history_days=1)
    assert len(first) == 1
    assert _members(demo_server) == {"Demo · Asha (focused dev)": True}
    assert "history samples" in capsys.readouterr().out

    again = live_demo.ensure_members(SERVER, history_days=1)
    assert again[0]["member_id"] == first[0]["member_id"]
    assert capsys.readouterr().out == ""  # reused, no new history

    demo_server.delete(
        f"/api/team/members/{first[0]['member_id']}",
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    recreated = live_demo.ensure_members(SERVER, history_days=1)
    assert recreated[0]["member_id"] != first[0]["member_id"]
    assert _members(demo_server) == {"Demo · Asha (focused dev)": True}


def test_wrong_admin_key_is_reported_as_such(demo_server, monkeypatch, capsys):
    monkeypatch.setenv("WORKSPACE_TEAM_ADMIN_KEY", "not-the-server-key-0123456789")

    assert live_demo.main(["--server", SERVER]) == 1
    out = capsys.readouterr().out
    assert "rejected the admin key" in out
    assert "WORKSPACE_TEAM_ADMIN_KEY" in out
    assert "Start it first" not in out

    assert live_demo.main(["--server", SERVER, "--remove"]) == 1
    assert "rejected the admin key" in capsys.readouterr().out


def test_admin_key_is_read_without_creating_or_replacing_the_file(tmp_path, monkeypatch):
    key_file = tmp_path / "admin_key"
    monkeypatch.setattr(live_demo, "TEAM_ADMIN_KEY_FILE", key_file)
    monkeypatch.delenv("WORKSPACE_TEAM_ADMIN_KEY", raising=False)

    with pytest.raises(live_demo.AdminKeyError):
        live_demo.read_admin_key()
    assert not key_file.exists()

    key_file.write_text("short", encoding="utf-8")
    with pytest.raises(live_demo.AdminKeyError):
        live_demo.read_admin_key()
    assert key_file.read_text(encoding="utf-8") == "short"

    key_file.write_text("file-key-0123456789abcdef\n", encoding="utf-8")
    assert live_demo.read_admin_key() == "file-key-0123456789abcdef"

    monkeypatch.setenv("WORKSPACE_TEAM_ADMIN_KEY", "env-key-0123456789abcdef")
    assert live_demo.read_admin_key() == "env-key-0123456789abcdef"


def test_flush_keeps_rows_while_unreachable_and_drops_removed_members(monkeypatch, capsys):
    streams = {"Demo · A": object(), "Demo · B": object()}
    tokens = {"Demo · A": "a", "Demo · B": "b"}
    pending = {"Demo · A": [{"application": "X"}], "Demo · B": [{"application": "Y"}]}

    def unreachable(server, token, rows):
        raise OSError("connection refused")

    monkeypatch.setattr(live_demo, "upload", unreachable)
    live_demo.flush(SERVER, streams, tokens, pending)
    assert set(streams) == {"Demo · A", "Demo · B"}
    assert pending["Demo · A"] == [{"application": "X"}]

    def removed_a(server, token, rows):
        if token == "a":
            raise ServerError(401, "revoked")
        return len(rows)

    monkeypatch.setattr(live_demo, "upload", removed_a)
    live_demo.flush(SERVER, streams, tokens, pending)
    assert set(streams) == {"Demo · B"}
    assert pending["Demo · B"] == []
    assert "no longer streaming" in capsys.readouterr().out


def test_blank_state_env_falls_back_to_default(monkeypatch):
    default = TEAM_DATA_DIR / "live_demo.json"

    for blank in ("", "   "):
        monkeypatch.setenv("WORKSPACE_TEAM_DEMO_STATE", blank)
        assert live_demo._state_file() == default

    monkeypatch.delenv("WORKSPACE_TEAM_DEMO_STATE")
    assert live_demo._state_file() == default

    monkeypatch.setenv("WORKSPACE_TEAM_DEMO_STATE", "data/demo/state.json")
    assert live_demo._state_file() == PROJECT_ROOT / "data" / "demo" / "state.json"

    monkeypatch.setenv("WORKSPACE_TEAM_DEMO_STATE", "/private/tmp/state.json")
    assert live_demo._state_file() == Path("/private/tmp/state.json")
