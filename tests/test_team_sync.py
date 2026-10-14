"""
The member collector, run against the real API through Flask's
test client instead of a network.
"""

import json
from urllib.parse import urlparse

import pytest

from collector import team_sync
from team import db
from tests.conftest import observation


@pytest.fixture
def sync(tmp_path, monkeypatch, client):
    """
    Point team_sync's state at a temp dir and route its HTTP
    calls to the test client.
    """

    monkeypatch.setattr(team_sync, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(team_sync, "CONFIG_FILE", tmp_path / "state" / "team.json")
    monkeypatch.setattr(team_sync, "QUEUE_FILE", tmp_path / "state" / "queue.db")

    calls = []

    def fake_request(method, url, body=None, token=None, timeout=15):
        calls.append((method, urlparse(url).path))
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.open(urlparse(url).path, method=method,
                               json=body, headers=headers)
        if response.status_code >= 400:
            raise team_sync.ServerError(
                response.status_code, response.get_json().get("error")
            )
        return response.get_json()

    monkeypatch.setattr(team_sync, "request_json", fake_request)
    return calls


def _join(monkeypatch, invite_code, answer="yes"):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    return team_sync.main(
        ["join", "--server", "http://127.0.0.1:5000", "--invite", invite_code]
    )


def test_declining_consent_sends_nothing(sync, monkeypatch, invite, capsys):
    assert _join(monkeypatch, invite(), answer="no") == 1

    assert sync == []
    assert not team_sync.CONFIG_FILE.exists()


def test_consent_screen_lists_window_titles(sync, monkeypatch, invite, capsys):
    _join(monkeypatch, invite(), answer="no")
    out = capsys.readouterr().out

    assert "window" in out.lower()
    assert "does not collect keystrokes" in out.lower()


def test_join_saves_private_config(sync, monkeypatch, invite):
    assert _join(monkeypatch, invite("Asha")) == 0

    config = json.loads(team_sync.CONFIG_FILE.read_text())
    assert config["name"] == "Asha"
    assert config["token"]
    assert team_sync.CONFIG_FILE.stat().st_mode & 0o077 == 0


def test_queue_survives_outage_then_uploads(sync, monkeypatch, invite, db_file):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()

    for second in range(0, 30, 5):
        team_sync.enqueue(conn, observation(f"2026-10-01 09:00:{second:02d}"))

    def offline(*_args, **_kwargs):
        raise OSError("connection refused")

    real = team_sync.request_json
    monkeypatch.setattr(team_sync, "request_json", offline)
    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 6

    monkeypatch.setattr(team_sync, "request_json", real)
    assert team_sync.flush(conn, config) is True
    assert team_sync.queued_count(conn) == 0

    with db.connect(db_file) as c:
        assert c.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 6


def test_queue_only_holds_consented_fields(sync, monkeypatch, invite):
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, {**observation(), "extra_secret": "should not be sent"})

    payload = json.loads(conn.execute("SELECT payload FROM queue").fetchone()[0])
    assert "extra_secret" not in payload


def test_removed_member_stops(sync, monkeypatch, invite, client, admin):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation())

    client.delete(f"/api/team/members/{config['member_id']}", headers=admin)

    assert team_sync.flush(conn, config) is False


def test_leave_deletes_server_data(sync, monkeypatch, invite, db_file):
    _join(monkeypatch, invite())
    config = team_sync.load_config()
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, observation())
    team_sync.flush(conn, config)
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert team_sync.main(["leave"]) == 0

    assert db.get_member(config["member_id"], db_file) is None
    assert not team_sync.CONFIG_FILE.exists()
