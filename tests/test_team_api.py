import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask

from config import TEAM_MAX_BATCH
from team import db
from team.api import init_team
from tests.conftest import ADMIN_KEY, consent, observation, recent_timestamp


# --------------------------------------------------
# Joining and consent
# --------------------------------------------------

def test_join_requires_explicit_consent(client, invite):
    code = invite()

    response = client.post(
        "/api/team/join",
        json={"invite_code": code, "consent": consent(accepted=False)},
    )
    assert response.status_code == 400

    # The invite is still usable after a refused attempt.
    response = client.post(
        "/api/team/join", json={"invite_code": code, "consent": consent()}
    )
    assert response.status_code == 201


def test_join_rejects_consent_for_different_fields(client, invite):
    response = client.post(
        "/api/team/join",
        json={
            "invite_code": invite(),
            "consent": consent(fields=["timestamp", "application"]),
        },
    )
    assert response.status_code == 409


def test_invite_is_single_use(client, invite):
    code = invite()
    body = {"invite_code": code, "consent": consent()}

    assert client.post("/api/team/join", json=body).status_code == 201
    assert client.post("/api/team/join", json=body).status_code == 403


def test_expired_invite_is_rejected(client, invite, db_file):
    code = invite()

    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE members SET invite_expires = ?",
            ((datetime.now().astimezone() - timedelta(hours=1)).isoformat(),),
        )

    response = client.post(
        "/api/team/join", json={"invite_code": code, "consent": consent()}
    )
    assert response.status_code == 403


def test_join_records_consent(client, member, db_file):
    member_id, _ = member()
    stored = db.get_member(member_id, db_file)

    assert stored["consent_at"] is not None
    assert stored["consent_version"] == 1
    assert "window_title" in stored["consent_fields"]
    assert stored["invite_hash"] is None


def test_tokens_are_stored_hashed(client, member, db_file):
    member_id, headers = member()
    token = headers["Authorization"].split()[1]
    stored = db.get_member(member_id, db_file)

    assert stored["token_hash"] != token
    assert stored["token_hash"] == db.hash_secret(token)


# --------------------------------------------------
# Ingest
# --------------------------------------------------

def test_ingest_requires_member_token(client):
    response = client.post("/api/team/ingest", json={"observations": []})
    assert response.status_code == 401

    response = client.post(
        "/api/team/ingest",
        json={"observations": []},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


def test_ingest_stores_and_deduplicates(client, member):
    _, headers = member()
    rows = [observation(recent_timestamp(0)), observation(recent_timestamp(5))]

    first = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert first.get_json() == {"accepted": 2, "duplicates": 0, "rejected": 0}

    # A retried upload doesn't create duplicates.
    again = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert again.get_json() == {"accepted": 0, "duplicates": 2, "rejected": 0}


def test_ingest_rejects_invalid_rows_but_keeps_valid_ones(client, member):
    _, headers = member()
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    rows = [
        observation(recent_timestamp(0)),
        observation("not a time"),
        observation(recent_timestamp(10), cpu_percent=150),
        observation(recent_timestamp(15), activity_status="Hacking"),
        observation(recent_timestamp(20), charging="yes"),
        observation(recent_timestamp(25), idle_seconds=float("nan")),
        observation(future),
        {"timestamp": recent_timestamp(30)},
        "not an object",
    ]

    response = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert response.get_json() == {"accepted": 1, "duplicates": 0, "rejected": 8}


def test_ingest_truncates_long_text(client, member, db_file):
    member_id, headers = member()
    client.post(
        "/api/team/ingest",
        json={"observations": [observation(window_title="x" * 5000)]},
        headers=headers,
    )

    rows, _, _ = db.recent_observations(member_id, db_file=db_file)
    assert len(rows[0]["window_title"]) == 512


def test_ingest_limits_batch_size(client, member):
    _, headers = member()
    rows = [observation() for _ in range(TEAM_MAX_BATCH + 1)]

    response = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert response.status_code == 413


def test_ingest_updates_last_seen(client, member, db_file):
    member_id, headers = member()
    assert db.get_member(member_id, db_file)["last_seen"] is None

    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)
    assert db.get_member(member_id, db_file)["last_seen"] is not None


def test_empty_or_rejected_upload_does_not_make_member_online(client, admin, member, db_file):
    member_id, headers = member()
    too_old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")

    for rows in ([], [observation(too_old)], [{"timestamp": "garbage"}]):
        response = client.post(
            "/api/team/ingest", json={"observations": rows}, headers=headers
        )
        assert response.status_code == 200
        assert response.get_json()["accepted"] == 0
        assert db.get_member(member_id, db_file)["last_seen"] is None

    team = client.get("/api/team/overview", headers=admin).get_json()["team"]
    assert team["online"] == 0
    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["status"] == "joined"

    # A retried upload (all duplicates) still shows the collector is alive.
    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)
    with db.connect(db_file) as conn:
        conn.execute("UPDATE members SET last_seen = NULL WHERE id = ?", (member_id,))
    response = client.post(
        "/api/team/ingest", json={"observations": [observation()]}, headers=headers
    )
    assert response.get_json()["duplicates"] == 1
    assert db.get_member(member_id, db_file)["last_seen"] is not None


# --------------------------------------------------
# Member self-service
# --------------------------------------------------

def test_member_can_see_what_is_stored(client, member):
    _, headers = member("Asha")
    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)

    body = client.get("/api/team/me", headers=headers).get_json()
    assert body["name"] == "Asha"
    assert body["observations_stored"] == 1
    assert "window_title" in body["shared_fields"]


def test_leaving_deletes_member_data_and_revokes_token(client, member, db_file):
    member_id, headers = member()
    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)

    assert client.delete("/api/team/me", headers=headers).status_code == 200

    assert db.get_member(member_id, db_file) is None
    with db.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

    response = client.post(
        "/api/team/ingest", json={"observations": [observation()]}, headers=headers
    )
    assert response.status_code == 401


# --------------------------------------------------
# Main-user endpoints
# --------------------------------------------------

def test_admin_endpoints_require_admin_key(client, member):
    member_id, member_headers = member()

    for method, url in [
        ("get", "/api/team/overview"),
        ("get", f"/api/team/members/{member_id}"),
        ("get", f"/api/team/members/{member_id}/activity"),
        ("delete", f"/api/team/members/{member_id}"),
        ("post", "/api/team/invites"),
    ]:
        call = getattr(client, method)
        assert call(url).status_code == 401
        assert call(url, headers={"X-Admin-Key": "wrong"}).status_code == 401
        # A member token is not an admin key.
        assert call(url, headers=member_headers).status_code == 401


def test_invite_requires_a_name(client, admin):
    assert client.post("/api/team/invites", json={}, headers=admin).status_code == 400
    assert client.post(
        "/api/team/invites", json={"name": "x" * 81}, headers=admin
    ).status_code == 400


def test_overview_lists_invited_and_joined_members(client, admin, invite, member):
    invite("Pending")
    member("Joined")

    body = client.get("/api/team/overview", headers=admin).get_json()
    statuses = {m["name"]: m["status"] for m in body["members"]}

    assert statuses == {"Pending": "invited", "Joined": "joined"}
    assert body["team"]["members"] == 2
    assert body["team"]["joined"] == 1

    # No secrets leak to the Team page.
    for m in body["members"]:
        assert "token_hash" not in m
        assert "invite_hash" not in m


def test_activity_is_paged_newest_first(client, admin, member):
    member_id, headers = member()
    rows = [
        observation(recent_timestamp(second), window_title=f"w{second}")
        for second in range(0, 50, 5)
    ]
    client.post("/api/team/ingest", json={"observations": rows}, headers=headers)

    body = client.get(
        f"/api/team/members/{member_id}/activity?limit=3&offset=1", headers=admin
    ).get_json()

    assert body["total"] == 10
    assert [r["window_title"] for r in body["rows"]] == ["w40", "w35", "w30"]


def test_unknown_member_is_404(client, admin):
    assert client.get("/api/team/members/999", headers=admin).status_code == 404
    assert client.get("/api/team/members/999/activity", headers=admin).status_code == 404
    assert client.delete("/api/team/members/999", headers=admin).status_code == 404


def test_removing_member_deletes_data_and_revokes_token(client, admin, member, db_file):
    member_id, headers = member()
    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)

    response = client.delete(f"/api/team/members/{member_id}", headers=admin)
    assert response.status_code == 200

    with db.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

    assert client.get("/api/team/me", headers=headers).status_code == 401


def test_invite_codes_are_safe_on_the_command_line(db_file):
    for _ in range(200):
        _, code = db.create_invite("x", db_file)
        assert not code.startswith("-")
        assert code.isalnum()


# --------------------------------------------------
# Admin key hardening
# --------------------------------------------------

def _app_with_key_file(db_file, key_file, monkeypatch):
    from flask import Flask

    from team.api import init_team

    monkeypatch.delenv("WORKSPACE_TEAM_ADMIN_KEY", raising=False)
    monkeypatch.setattr("team.api.TEAM_ADMIN_KEY_FILE", key_file)

    app = Flask(__name__)
    init_team(app, db_file=db_file)
    return app


def test_empty_or_short_admin_key_file_is_replaced(tmp_path, db_file, monkeypatch):
    from team.api import MIN_ADMIN_KEY_LENGTH

    for content in ["", "\n", "   \n", "short"]:
        key_file = tmp_path / "admin_key"
        key_file.write_text(content, encoding="utf-8")

        app = _app_with_key_file(db_file, key_file, monkeypatch)
        key = app.config["TEAM_ADMIN_KEY"]

        assert len(key) >= MIN_ADMIN_KEY_LENGTH
        assert key_file.read_text(encoding="utf-8") == key

        client = app.test_client()
        assert client.get("/api/team/overview").status_code == 401
        assert client.get(
            "/api/team/overview", headers={"X-Admin-Key": ""}
        ).status_code == 401
        assert client.get(
            "/api/team/overview", headers={"X-Admin-Key": content.strip()}
        ).status_code == 401
        assert client.get(
            "/api/team/overview", headers={"X-Admin-Key": key}
        ).status_code == 200


def test_admin_key_file_is_created_and_reused(tmp_path, db_file, monkeypatch):
    import os

    key_file = tmp_path / "keys" / "admin_key"

    first = _app_with_key_file(db_file, key_file, monkeypatch).config["TEAM_ADMIN_KEY"]
    second = _app_with_key_file(db_file, key_file, monkeypatch).config["TEAM_ADMIN_KEY"]

    assert first == second
    assert key_file.read_text(encoding="utf-8") == first
    if os.name != "nt":
        assert key_file.stat().st_mode & 0o077 == 0
    # No temp files left behind.
    assert [p.name for p in key_file.parent.iterdir()] == ["admin_key"]


def test_admin_key_env_override(tmp_path, monkeypatch):
    from team.api import load_admin_key

    key_file = tmp_path / "admin_key"
    long_key = "env-key-that-is-long-enough"

    monkeypatch.setenv("WORKSPACE_TEAM_ADMIN_KEY", long_key)
    assert load_admin_key(key_file) == long_key
    assert not key_file.exists()

    # A too-short (or blank) env key is ignored in favour of the file.
    for short in ["tiny", "   "]:
        monkeypatch.setenv("WORKSPACE_TEAM_ADMIN_KEY", short)
        key = load_admin_key(key_file)
        assert key != short.strip()
        assert key == key_file.read_text(encoding="utf-8")


def test_short_explicit_admin_key_is_refused(db_file):
    import pytest
    from flask import Flask

    from team.api import init_team

    with pytest.raises(ValueError):
        init_team(Flask(__name__), admin_key="too-short", db_file=db_file)


def test_empty_configured_admin_key_never_authorises(app):
    app.config["TEAM_ADMIN_KEY"] = ""
    client = app.test_client()

    assert client.get("/api/team/overview").status_code == 401
    assert client.get(
        "/api/team/overview", headers={"X-Admin-Key": ""}
    ).status_code == 401


# --------------------------------------------------
# Request validation
# --------------------------------------------------

def test_non_object_json_bodies_are_rejected(client, admin, invite, member):
    _, headers = member()

    for body in [[1], "text", 3, None]:
        if body is None:
            continue
        assert client.post("/api/team/join", json=body).status_code == 400
        assert client.post("/api/team/ingest", json=body, headers=headers).status_code == 400
        assert client.post("/api/team/invites", json=body, headers=admin).status_code == 400

    for bad_consent in ["yes", [1], 5]:
        response = client.post(
            "/api/team/join",
            json={"invite_code": invite(), "consent": bad_consent},
        )
        assert response.status_code == 400


def test_ingest_rejects_very_old_observations(client, member):
    _, headers = member()
    old = (datetime.now() - timedelta(days=91)).strftime("%Y-%m-%d %H:%M:%S")
    fairly_old = (datetime.now() - timedelta(days=80)).strftime("%Y-%m-%d %H:%M:%S")

    response = client.post(
        "/api/team/ingest",
        json={"observations": [observation(old), observation(fairly_old)]},
        headers=headers,
    )
    assert response.get_json() == {"accepted": 1, "duplicates": 0, "rejected": 1}


# --------------------------------------------------
# Invites: response shape, expiry, re-issue
# --------------------------------------------------

def test_invite_response_has_port_and_join_command(client, admin):
    from config import API_PORT

    body = client.post("/api/team/invites", json={"name": "A"}, headers=admin).get_json()

    assert body["port"] == API_PORT
    assert body["simulated"] is False
    assert body["join_command"].startswith("python collector/team_sync.py join --server ")
    assert body["join_command"].endswith(f"--invite {body['invite_code']}")
    server = body["server_url"] or f"http://YOUR-IP:{API_PORT}"
    assert f"--server {server} " in body["join_command"]


def test_simulated_flag_is_stored_and_exposed(client, admin, db_file):
    body = client.post(
        "/api/team/invites", json={"name": "Bot", "simulated": True}, headers=admin
    ).get_json()
    client.post("/api/team/invites", json={"name": "Human"}, headers=admin)

    assert body["simulated"] is True
    assert db.get_member(body["member_id"], db_file)["simulated"] == 1

    overview = client.get("/api/team/overview", headers=admin).get_json()
    assert {m["name"]: m["simulated"] for m in overview["members"]} == {
        "Bot": True, "Human": False,
    }

    detail = client.get(f"/api/team/members/{body['member_id']}", headers=admin).get_json()
    assert detail["simulated"] is True

    live = client.get("/api/team/live", headers=admin).get_json()
    assert {m["name"]: m["simulated"] for m in live["members"]} == {
        "Bot": True, "Human": False,
    }

    assert client.post(
        "/api/team/invites", json={"name": "X", "simulated": "yes"}, headers=admin
    ).status_code == 400


def test_simulated_column_is_added_to_old_databases(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        db.SCHEMA.replace(
            ",\n    simulated       INTEGER NOT NULL DEFAULT 0", ""
        )
    )
    conn.execute(
        "INSERT INTO members (name, created_at) VALUES ('Old', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)  # idempotent

    assert db.list_members(path)[0]["simulated"] == 0
    member_id, _ = db.create_invite("New", path, simulated=True)
    assert db.get_member(member_id, path)["simulated"] == 1


def _legacy_db(path, with_simulated_column):
    """
    A team.db written before members were flagged as simulated: by
    the first release (no column), or by a build that added the
    column without flagging anyone.
    """

    schema = db.SCHEMA
    if not with_simulated_column:
        schema = schema.replace(",\n    simulated       INTEGER NOT NULL DEFAULT 0", "")
        assert "simulated" not in schema

    created = "2026-10-01T09:00:00+00:00"
    members = [
        # name, joined_at
        ("Asha (focused dev)", "2026-10-01T09:00:00+00:00"),          # team.simulate
        ("Demo · Eli (context switcher)", "2026-10-01T09:00:01+00:00"),  # early live demo
        ("Ben (focused dev)", "2026-10-01T11:30:00+00:00"),           # a person, joined later
        ("Demo · Priya", "2026-10-01T09:00:00+00:00"),               # not a simulator name
        ("Chen (analyst)", None),                                      # never joined
        ("Rita", "2026-10-01T09:20:00+00:00"),
    ]

    conn = sqlite3.connect(path)
    conn.executescript(schema)
    for index, (name, joined_at) in enumerate(members):
        conn.execute(
            "INSERT INTO members (name, token_hash, joined_at, consent_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, f"hash-{index}" if joined_at else None, joined_at, joined_at, created),
        )
    conn.commit()
    conn.close()


def test_legacy_simulated_members_are_flagged_once(tmp_path):
    for with_column in (False, True):
        path = tmp_path / f"legacy-{with_column}.db"
        _legacy_db(path, with_simulated_column=with_column)

        db.init_db(path)

        flags = {m["name"]: m["simulated"] for m in db.list_members(path)}
        assert flags == {
            "Asha (focused dev)": 1,
            "Demo · Eli (context switcher)": 1,
            "Ben (focused dev)": 0,
            "Demo · Priya": 0,
            "Chen (analyst)": 0,
            "Rita": 0,
        }, with_column

        # It runs once: later members are flagged by whoever creates them.
        with db.connect(path) as conn:
            conn.execute(
                "INSERT INTO members (name, token_hash, joined_at, created_at) "
                "VALUES ('Dana (analyst)', 'x', '2026-10-02T09:00:00+00:00', "
                "'2026-10-02T09:00:00+00:00')"
            )
        db.init_db(path)
        assert {m["name"]: m["simulated"] for m in db.list_members(path)}[
            "Dana (analyst)"
        ] == 0


def test_init_team_accepts_a_str_db_file(tmp_path):
    from team import simulate

    path = str(tmp_path / "nested" / "team.db")
    app = Flask(__name__)
    init_team(app, admin_key=ADMIN_KEY, db_file=path)

    assert app.config["TEAM_DB_FILE"] == Path(path)
    client = app.test_client()
    response = client.post(
        "/api/team/invites", json={"name": "A"}, headers={"X-Admin-Key": ADMIN_KEY}
    )
    assert response.status_code == 201
    assert client.get(
        "/api/team/overview", headers={"X-Admin-Key": ADMIN_KEY}
    ).get_json()["team"]["members"] == 1

    assert [m["name"] for m in db.list_members(path)] == ["A"]
    assert simulate.real_member_count(path) == 1


def _expire_invites(db_file):
    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE members SET invite_expires = ? WHERE token_hash IS NULL",
            ((datetime.now().astimezone() - timedelta(hours=1)).isoformat(),),
        )


def test_expired_invite_status(client, admin, invite, member, db_file):
    invite("Late")
    member("Joined")
    _expire_invites(db_file)

    body = client.get("/api/team/overview", headers=admin).get_json()
    statuses = {m["name"]: m["status"] for m in body["members"]}

    assert statuses == {"Late": "expired", "Joined": "joined"}
    assert body["team"]["joined"] == 1


def test_reissued_invite_replaces_the_old_code(client, admin, db_file):
    created = client.post("/api/team/invites", json={"name": "Re"}, headers=admin).get_json()
    member_id, old_code = created["member_id"], created["invite_code"]
    _expire_invites(db_file)

    response = client.post(f"/api/team/members/{member_id}/invite", headers=admin)
    assert response.status_code == 201
    body = response.get_json()

    assert body["member_id"] == member_id
    assert body["name"] == "Re"
    assert body["invite_code"] != old_code
    assert body["expires"] > datetime.now().astimezone().isoformat()
    assert body["join_command"].endswith(body["invite_code"])

    overview = client.get("/api/team/overview", headers=admin).get_json()
    assert [m["status"] for m in overview["members"]] == ["invited"]

    # The old code no longer works; the new one joins the same row.
    assert client.post(
        "/api/team/join", json={"invite_code": old_code, "consent": consent()}
    ).status_code == 403
    joined = client.post(
        "/api/team/join", json={"invite_code": body["invite_code"], "consent": consent()}
    )
    assert joined.status_code == 201
    assert joined.get_json()["member_id"] == member_id

    # Joined members can't be re-invited; unknown ids are 404.
    assert client.post(
        f"/api/team/members/{member_id}/invite", headers=admin
    ).status_code == 409
    assert client.post("/api/team/members/999/invite", headers=admin).status_code == 404
    assert client.post(f"/api/team/members/{member_id}/invite").status_code == 401


def test_redeem_rechecks_the_code_when_updating(db_file, monkeypatch):
    """
    If the invite is redeemed by someone else between the lookup
    and the update, the second redemption must fail.
    """

    from config import TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS

    _, code = db.create_invite("Race", db_file)
    real_connect = db.connect
    raced = {"done": False}

    class RacingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql.lstrip().startswith("UPDATE") and not raced["done"]:
                raced["done"] = True
                # The competing request wins first.
                assert db.redeem_invite(
                    code, TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS, db_file
                ) is not None
            return self._conn.execute(sql, params)

    from contextlib import contextmanager

    @contextmanager
    def racing_connect(path=None):
        with real_connect(path) as conn:
            yield RacingConnection(conn)

    monkeypatch.setattr(db, "connect", racing_connect)
    loser = db.redeem_invite(code, TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS, db_file)
    monkeypatch.setattr(db, "connect", real_connect)

    assert raced["done"]
    assert loser is None


# --------------------------------------------------
# Analysis window (?days=)
# --------------------------------------------------

def test_overview_days_window(client, admin, member):
    _, headers = member("Windowed")
    old = (datetime.now() - timedelta(days=10)).replace(microsecond=0)
    rows = [
        observation((old + timedelta(seconds=5 * i)).strftime("%Y-%m-%d %H:%M:%S"))
        for i in range(30)
    ] + [observation(recent_timestamp(5 * i)) for i in range(30)]
    client.post("/api/team/ingest", json={"observations": rows}, headers=headers)

    default = client.get("/api/team/overview", headers=admin).get_json()
    week = client.get("/api/team/overview?days=7", headers=admin).get_json()
    everything = client.get("/api/team/overview?days=all", headers=admin).get_json()

    assert default["days"] == 30
    assert week["days"] == 7
    assert everything["days"] == "all"
    assert default["team"]["observations"] == 60
    assert week["team"]["observations"] == 30
    assert everything["team"]["sharing"] == 1
    assert week["data_as_of"] == recent_timestamp(5 * 29)
    assert default["generated_at"]

    # Lifetime counts on the member row are not windowed.
    assert week["members"][0]["observations"] == 60

    assert client.get("/api/team/overview?days=2", headers=admin).status_code == 400
    assert client.get("/api/team/overview?days=abc", headers=admin).status_code == 400


def test_member_detail_days_and_freshness(client, admin, member):
    member_id, headers = member()
    client.post(
        "/api/team/ingest",
        json={"observations": [observation(recent_timestamp(5 * i)) for i in range(10)]},
        headers=headers,
    )

    body = client.get(f"/api/team/members/{member_id}?days=7", headers=admin).get_json()

    assert body["days"] == 7
    assert body["simulated"] is False
    assert body["data_as_of"] == recent_timestamp(45)
    assert body["generated_at"]
    assert body["summary"]["observations"] == 10

    assert client.get(
        f"/api/team/members/{member_id}?days=3", headers=admin
    ).status_code == 400


def test_sharing_counts_members_with_data(client, admin, member):
    member("Quiet")
    _, headers = member("Busy")
    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)

    team = client.get("/api/team/overview", headers=admin).get_json()["team"]
    assert team["joined"] == 2
    assert team["sharing"] == 1


# --------------------------------------------------
# Live view
# --------------------------------------------------

def _now_rows(count, start_offset=0, **overrides):
    start = datetime.now().replace(microsecond=0) - timedelta(seconds=5 * count)
    return [
        observation(
            (start + timedelta(seconds=5 * (i + start_offset))).strftime("%Y-%m-%d %H:%M:%S"),
            **overrides,
        )
        for i in range(count)
    ]


def test_live_shows_current_activity_for_online_members(client, admin, member, invite, db_file):
    invite("Pending")
    online_id, online_headers = member("Online")
    offline_id, offline_headers = member("Offline")

    rows = _now_rows(12)
    for i, row in enumerate(rows):
        row["application"] = ["Slack", "Chrome", "Terminal"][i // 4]
    rows[-1]["activity_status"] = "Idle"
    rows[-1]["idle_seconds"] = 75.0
    client.post("/api/team/ingest", json={"observations": rows}, headers=online_headers)
    client.post("/api/team/ingest", json={"observations": _now_rows(3)}, headers=offline_headers)

    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE members SET last_seen = ? WHERE id = ?",
            ((datetime.now().astimezone() - timedelta(hours=1)).isoformat(), offline_id),
        )

    response = client.get("/api/team/live", headers=admin)
    assert response.status_code == 200
    assert response.headers["ETag"]
    body = response.get_json()

    assert body["online_window_seconds"] == 120
    assert body["generated_at"]
    by_name = {m["name"]: m for m in body["members"]}
    assert set(by_name) == {"Pending", "Online", "Offline"}

    online = by_name["Online"]
    assert online["status"] == "online"
    assert online["current"]["application"] == "Terminal"
    assert online["current"]["activity_status"] == "Idle"
    assert online["current"]["idle_seconds"] == 75.0
    assert online["current"]["timestamp"] == rows[-1]["timestamp"]
    assert set(online["current"]) == {
        "timestamp", "application", "window_title", "activity_status",
        "idle_seconds", "cpu_percent", "battery_percent",
    }
    assert online["last_10_min"] == {
        "active_seconds": 50.0,
        "idle_seconds": 5.0,
        "switches": 2,
        "distinct_apps": 3,
    }

    assert by_name["Offline"]["status"] == "offline"
    assert by_name["Offline"]["current"] is None
    assert by_name["Offline"]["last_10_min"]["active_seconds"] == 10.0

    assert by_name["Pending"]["status"] == "invited"
    assert by_name["Pending"]["current"] is None
    assert by_name["Pending"]["last_10_min"]["distinct_apps"] == 0


def test_live_etag_returns_304_until_data_changes(client, admin, member):
    _, headers = member()
    client.post("/api/team/ingest", json={"observations": _now_rows(2)}, headers=headers)

    first = client.get("/api/team/live", headers=admin)
    etag = first.headers["ETag"]

    again = client.get("/api/team/live", headers={**admin, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["ETag"] == etag
    assert not again.data

    client.post("/api/team/ingest", json={"observations": [observation()]}, headers=headers)

    changed = client.get("/api/team/live", headers={**admin, "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag

    assert client.get("/api/team/live").status_code == 401


def test_live_etag_changes_when_member_goes_offline(client, admin, member, db_file):
    member_id, headers = member()
    client.post("/api/team/ingest", json={"observations": _now_rows(2)}, headers=headers)
    etag = client.get("/api/team/live", headers=admin).headers["ETag"]

    # No new data, but the member's last upload is now too old.
    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE members SET last_seen = ? WHERE id = ?",
            ((datetime.now().astimezone() - timedelta(minutes=5)).isoformat(), member_id),
        )

    response = client.get("/api/team/live", headers={**admin, "If-None-Match": etag})
    assert response.status_code == 200
    assert response.get_json()["members"][0]["status"] == "offline"


# --------------------------------------------------
# Activity paging
# --------------------------------------------------

def test_before_cursor_paging_has_no_duplicates_while_data_streams(client, admin, member):
    member_id, headers = member()
    url = f"/api/team/members/{member_id}/activity"
    client.post(
        "/api/team/ingest",
        json={"observations": [observation(recent_timestamp(5 * i)) for i in range(10)]},
        headers=headers,
    )

    first = client.get(f"{url}?limit=4", headers=admin).get_json()
    assert first["has_more"] is True
    assert [r["timestamp"] for r in first["rows"]] == [
        recent_timestamp(5 * i) for i in (9, 8, 7, 6)
    ]

    # Six newer rows arrive between pages.
    client.post(
        "/api/team/ingest",
        json={"observations": [observation(recent_timestamp(5 * i)) for i in range(10, 16)]},
        headers=headers,
    )

    seen = [r["timestamp"] for r in first["rows"]]
    cursor = first["rows"][-1]["timestamp"]
    while True:
        page = client.get(f"{url}?limit=4&before={cursor}", headers=admin).get_json()
        seen += [r["timestamp"] for r in page["rows"]]
        if not page["has_more"]:
            break
        cursor = page["rows"][-1]["timestamp"]

    assert seen == [recent_timestamp(5 * i) for i in range(9, -1, -1)]
    assert page["total"] == 16

    # after= catches up on the new rows, oldest page first, each
    # page still newest first.
    newer = client.get(
        f"{url}?limit=4&after={recent_timestamp(45)}", headers=admin
    ).get_json()
    assert [r["timestamp"] for r in newer["rows"]] == [
        recent_timestamp(5 * i) for i in (13, 12, 11, 10)
    ]
    assert newer["has_more"] is True

    rest = client.get(
        f"{url}?limit=4&after={newer['rows'][0]['timestamp']}", headers=admin
    ).get_json()
    assert [r["timestamp"] for r in rest["rows"]] == [
        recent_timestamp(5 * i) for i in (15, 14)
    ]
    assert rest["has_more"] is False

    window = client.get(
        f"{url}?before={recent_timestamp(20)}&after={recent_timestamp(5)}", headers=admin
    ).get_json()
    assert [r["timestamp"] for r in window["rows"]] == [
        recent_timestamp(15), recent_timestamp(10)
    ]

    assert client.get(f"{url}?before=yesterday", headers=admin).status_code == 400
