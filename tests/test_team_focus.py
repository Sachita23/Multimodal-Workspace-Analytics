"""
Shared focus sessions on the team server: POST /api/team/focus,
storage (including migrating an older database), the admin focus
endpoint, the focus blocks of /overview and /live, and the live demo's
focus sessions.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from team import analysis, db, live_demo
from team.api import validate_focus_session, FocusValidationError
from team.simulate import PROFILES
from tests.conftest import ADMIN_KEY, observation
from tests.test_live_demo import SERVER, demo_server  # noqa: F401  (fixture)


def _iso(value):
    return value.isoformat(timespec="seconds")


def focus_session(**overrides):
    """
    A valid running session, started 10 minutes ago.
    """

    now = datetime.now().replace(microsecond=0)

    session = {
        "session_id": "a1b2c3",
        "title": "Write the report",
        "goal": "Finish section 2",
        "planned_minutes": 25,
        "allowed_apps": ["Visual Studio Code", "Terminal"],
        "started_at": _iso(now - timedelta(minutes=10)),
        "ended_at": None,
        "status": "running",
        "deviations": 1,
        "deviation_seconds": 42.5,
        "allowed_seconds": 520.0,
        "score": 91,
        "current_app": "Visual Studio Code",
        "current_allowed": True,
        "remaining_s": 900,
        "updated_at": _iso(now),
    }
    session.update(overrides)
    return session


def finished_session(session_id, score, started_days_ago=1, **overrides):
    start = datetime.now().replace(microsecond=0) - timedelta(days=started_days_ago)
    return focus_session(
        session_id=session_id,
        status="Completed",
        started_at=_iso(start),
        ended_at=_iso(start + timedelta(minutes=25)),
        updated_at=_iso(start + timedelta(minutes=25)),
        score=score,
        current_app=None,
        current_allowed=None,
        remaining_s=None,
        **overrides,
    )


def share(client, headers, session):
    return client.post("/api/team/focus", json={"session": session}, headers=headers)


def stored(db_file):
    return db.focus_sessions(db_file=db_file)


# --------------------------------------------------
# Upload
# --------------------------------------------------

def test_member_shares_a_running_session_then_its_end(client, member, db_file):
    member_id, headers = member("Asha")

    response = share(client, headers, focus_session())
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}

    [row] = stored(db_file)
    assert row["member_id"] == member_id
    assert row["status"] == "running"
    assert row["allowed_apps"] == ["Visual Studio Code", "Terminal"]
    assert row["current_allowed"] is True
    assert row["received_at"]

    # The same session again: replaced, not added.
    end = finished_session("a1b2c3", 86, started_days_ago=0)
    end["started_at"] = focus_session()["started_at"]
    assert share(client, headers, end).status_code == 200

    [row] = stored(db_file)
    assert row["status"] == "Completed"
    assert row["score"] == 86
    assert row["ended_at"] == end["ended_at"]
    assert row["current_app"] is None and row["current_allowed"] is None


def test_upload_marks_the_member_online(client, member, admin):
    _, headers = member("Asha")
    share(client, headers, focus_session())

    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["status"] == "online"


def test_same_session_id_from_two_members_is_two_sessions(client, member, db_file):
    _, first = member("Asha")
    _, second = member("Ben")

    share(client, first, focus_session())
    share(client, second, focus_session())

    assert len(stored(db_file)) == 2


def test_focus_upload_needs_a_member_token(client, admin, db_file):
    assert client.post("/api/team/focus", json={"session": focus_session()}).status_code == 401
    # The admin key is not a member token.
    assert share(client, admin, focus_session()).status_code == 401
    assert stored(db_file) == []


def _days_ago(days):
    return _iso(datetime.now().replace(microsecond=0) - timedelta(days=days))


@pytest.mark.parametrize("overrides", [
    {"session_id": ""},
    {"session_id": 7},
    {"session_id": "x" * 513},
    {"title": "   "},
    {"title": None},
    {"title": "t" * 513},
    {"goal": "g" * 513},
    {"goal": 5},
    {"status": "In progress"},
    {"status": None},
    {"allowed_apps": []},
    {"allowed_apps": "Visual Studio Code"},
    {"allowed_apps": [f"App {n}" for n in range(31)]},
    {"allowed_apps": ["Code", 3]},
    {"allowed_apps": ["Code", ""]},
    {"allowed_apps": ["a" * 513]},
    {"planned_minutes": -1},
    {"planned_minutes": 2.5},
    {"planned_minutes": "25"},
    {"planned_minutes": True},
    {"deviations": -1},
    {"deviations": 1.5},
    {"deviation_seconds": -0.1},
    {"deviation_seconds": "42"},
    {"allowed_seconds": None},
    {"score": 101},
    {"score": -1},
    {"score": 50.5},
    {"score": True},
    {"score": None},
    {"current_app": 12},
    {"current_app": "c" * 513},
    {"current_allowed": "yes"},
    {"current_allowed": 1},
    {"remaining_s": -5},
    {"remaining_s": 1.5},
    {"started_at": "yesterday"},
    {"started_at": None},
    {"started_at": "2026-13-01T10:00:00"},
    {"started_at": _days_ago(91)},
    {"started_at": _days_ago(-2)},
    {"updated_at": "soon"},
    {"updated_at": _days_ago(91)},
    {"ended_at": _days_ago(0)},                       # running with an end
    {"status": "Completed"},                          # finished without one
    {"status": "Stopped", "ended_at": _days_ago(1)},  # ends before it started
])
def test_invalid_focus_sessions_are_refused(client, member, db_file, overrides):
    _, headers = member()

    response = share(client, headers, focus_session(**overrides))

    assert response.status_code == 400, overrides
    assert response.get_json()["error"]
    assert stored(db_file) == []


@pytest.mark.parametrize("key", [
    "session_id", "title", "planned_minutes", "allowed_apps", "started_at",
    "status", "deviations", "deviation_seconds", "allowed_seconds", "score",
    "updated_at",
])
def test_required_fields_must_be_present(client, member, key):
    _, headers = member()
    session = focus_session()
    del session[key]

    assert share(client, headers, session).status_code == 400


@pytest.mark.parametrize("body", [
    "[]", '{"session": []}', '{"session": "x"}', "{}", '{"other": {}}',
])
def test_upload_must_hold_a_session_object(client, member, body):
    _, headers = member()

    response = client.post(
        "/api/team/focus", data=body, headers=headers,
        content_type="application/json",
    )

    assert response.status_code == 400


@pytest.mark.parametrize("field, raw", [
    ("deviation_seconds", "NaN"),
    ("allowed_seconds", "Infinity"),
    ("score", "-Infinity"),
])
def test_non_finite_numbers_are_refused(client, member, db_file, field, raw):
    _, headers = member()
    body = json.dumps({"session": focus_session(**{field: 0})}).replace(
        f'"{field}": 0', f'"{field}": {raw}'
    )
    assert raw in body

    response = client.post(
        "/api/team/focus", data=body, headers=headers,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert stored(db_file) == []


def test_optional_fields_and_time_zones_are_normalised():
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    clean = validate_focus_session(focus_session(
        goal=None,
        current_app=None,
        current_allowed=None,
        remaining_s=None,
        title="  Report  ",
        started_at=start.isoformat(),
        score=90.0,
    ))

    assert clean["goal"] == ""
    assert clean["title"] == "Report"
    assert clean["score"] == 90 and isinstance(clean["score"], int)
    # A UTC timestamp is stored in this machine's local time, like the
    # collector's own timestamps.
    assert clean["started_at"] == start.astimezone().replace(tzinfo=None).isoformat()

    with pytest.raises(FocusValidationError):
        validate_focus_session(None)


def test_validated_session_has_exactly_the_stored_columns():
    assert set(validate_focus_session(focus_session())) == set(db.FOCUS_COLUMNS)


# --------------------------------------------------
# Storage
# --------------------------------------------------

# The schema before focus sessions (user_version 1).
SCHEMA_V1 = """
CREATE TABLE members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    invite_hash     TEXT    UNIQUE,
    invite_expires  TEXT,
    token_hash      TEXT    UNIQUE,
    consent_version INTEGER,
    consent_fields  TEXT,
    consent_at      TEXT,
    joined_at       TEXT,
    last_seen       TEXT,
    created_at      TEXT    NOT NULL,
    simulated       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    application     TEXT    NOT NULL,
    window_title    TEXT    NOT NULL,
    activity_status TEXT    NOT NULL,
    idle_seconds    REAL    NOT NULL,
    cpu_percent     REAL    NOT NULL,
    memory_percent  REAL    NOT NULL,
    battery_percent REAL    NOT NULL,
    charging        INTEGER NOT NULL,
    received_at     TEXT    NOT NULL,
    UNIQUE (member_id, timestamp)
);
CREATE INDEX idx_observations_member_time ON observations (member_id, timestamp);
PRAGMA user_version = 1;
"""


def test_older_database_is_migrated_and_keeps_its_data(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT INTO members (name, token_hash, joined_at, created_at, simulated) "
        "VALUES ('Rita', 'hash', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00', 0)"
    )
    row = observation()
    conn.execute(
        "INSERT INTO observations (member_id, timestamp, application, window_title, "
        "activity_status, idle_seconds, cpu_percent, memory_percent, "
        "battery_percent, charging, received_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["timestamp"], row["application"], row["window_title"],
         row["activity_status"], row["idle_seconds"], row["cpu_percent"],
         row["memory_percent"], row["battery_percent"], 1, "2026-01-01"),
    )
    conn.commit()
    conn.close()

    db.init_db(path)
    db.init_db(path)  # running it again changes nothing

    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 3
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(focus_sessions)")}
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(focus_sessions)")}

    assert columns == {"member_id", *db.FOCUS_COLUMNS, "received_at"}
    assert "idx_focus_sessions_member_start" in indexes
    assert [m["name"] for m in db.list_members(path)] == ["Rita"]

    db.upsert_focus_session(1, validate_focus_session(focus_session()), path)
    assert len(db.focus_sessions(1, path)) == 1


def test_removing_a_member_deletes_their_focus_sessions(client, member, admin, db_file):
    asha_id, asha = member("Asha")
    ben_id, ben = member("Ben")
    share(client, asha, focus_session())
    share(client, ben, focus_session())

    assert client.delete(f"/api/team/members/{asha_id}", headers=admin).status_code == 200

    assert [row["member_id"] for row in stored(db_file)] == [ben_id]

    # Leaving (DELETE /me) deletes them too.
    assert client.delete("/api/team/me", headers=ben).status_code == 200
    assert stored(db_file) == []


def test_foreign_key_cascades_when_a_member_row_is_deleted(client, member, db_file):
    member_id, headers = member()
    share(client, headers, focus_session())

    with db.connect(db_file) as conn:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))

    assert stored(db_file) == []


# --------------------------------------------------
# Admin views
# --------------------------------------------------

def _age_received(db_file, seconds):
    old = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE focus_sessions SET received_at = ?",
            (old.isoformat(timespec="seconds"),),
        )


def test_overview_and_live_show_focus_score_and_live_session(client, member, admin, db_file):
    asha_id, asha = member("Asha")
    member("Ben")

    share(client, asha, finished_session("old-1", 80, started_days_ago=2))
    share(client, asha, finished_session("old-2", 61, started_days_ago=1))
    # Outside the 30-day score window, but still accepted (< 90 days).
    share(client, asha, finished_session("ancient", 0, started_days_ago=40))
    share(client, asha, focus_session(
        session_id="now", score=95, deviations=0, deviation_seconds=0,
    ))

    overview = client.get("/api/team/overview", headers=admin).get_json()
    by_name = {m["name"]: m for m in overview["members"]}

    focus = by_name["Asha"]["focus"]
    assert focus["score"] == 70.5
    assert focus["sessions"] == 3
    assert focus["live"] == {
        "running": True,
        "session_id": "now",
        "title": "Write the report",
        "goal": "Finish section 2",
        "started_at": focus_session()["started_at"],
        "planned_minutes": 25,
        "allowed_apps": ["Visual Studio Code", "Terminal"],
        "remaining_s": 900,
        "current_app": "Visual Studio Code",
        "allowed": True,
        "score": 95,
        "deviations": 0,
        "updated_at": focus["live"]["updated_at"],
    }
    assert by_name["Ben"]["focus"] == {"score": None, "sessions": 0, "live": None}

    live = client.get("/api/team/live", headers=admin).get_json()
    live_by_name = {m["name"]: m for m in live["members"]}
    assert live_by_name["Asha"]["focus"] == focus
    assert live_by_name["Ben"]["focus"]["live"] is None

    detail = client.get(f"/api/team/members/{asha_id}", headers=admin).get_json()
    assert detail["focus"] == focus


def test_running_session_stops_being_live_after_30_seconds(client, member, admin, db_file):
    _, headers = member("Asha")
    share(client, headers, focus_session())

    _age_received(db_file, 20)
    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["focus"]["live"]["running"] is True

    _age_received(db_file, 31)
    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["focus"]["live"] is None
    # An abandoned running session never counts in the score.
    assert live["members"][0]["focus"]["score"] is None


def test_live_judges_freshness_by_server_time_not_member_clock(client, member, admin):
    _, headers = member("Asha")
    # The member's clock is 12 hours behind (or another time zone).
    behind = datetime.now().replace(microsecond=0) - timedelta(hours=12)
    share(client, headers, focus_session(
        started_at=_iso(behind - timedelta(minutes=5)), updated_at=_iso(behind),
    ))

    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["focus"]["live"] is not None


def test_live_etag_changes_with_every_focus_upload(client, member, admin):
    _, headers = member("Asha")
    share(client, headers, focus_session())

    first = client.get("/api/team/live", headers=admin)
    tag = first.headers["ETag"]
    assert client.get(
        "/api/team/live", headers={**admin, "If-None-Match": tag}
    ).status_code == 304

    # Same second, new numbers: the response must change.
    share(client, headers, focus_session(remaining_s=899, current_allowed=False,
                                         current_app="Slack", deviations=2))

    second = client.get("/api/team/live", headers={**admin, "If-None-Match": tag})
    assert second.status_code == 200
    focus_live = second.get_json()["members"][0]["focus"]["live"]
    assert focus_live["current_app"] == "Slack"
    assert focus_live["allowed"] is False


def test_member_focus_endpoint_lists_newest_first_with_summary(client, member, admin, db_file):
    asha_id, asha = member("Asha")
    share(client, asha, finished_session("a", 90, started_days_ago=3, deviations=2,
                                         deviation_seconds=0))
    share(client, asha, finished_session("b", 70, started_days_ago=1, deviations=4,
                                         deviation_seconds=0))
    share(client, asha, focus_session(session_id="c", deviations=1))

    response = client.get(f"/api/team/members/{asha_id}/focus", headers=admin)
    assert response.status_code == 200
    body = response.get_json()

    assert [s["session_id"] for s in body["sessions"]] == ["c", "b", "a"]
    assert body["sessions"][0]["live"] is True
    assert body["sessions"][1]["live"] is False
    assert body["summary"] == {
        "sessions": 3, "focus_score": 80.0, "deviation_count": 7,
        "window_days": 30,
    }
    assert body["live"]["session_id"] == "c"
    assert set(body["sessions"][0]) >= {
        "session_id", "title", "goal", "planned_minutes", "allowed_apps",
        "started_at", "ended_at", "status", "deviations", "deviation_seconds",
        "allowed_seconds", "score", "current_app", "current_allowed",
        "remaining_s", "updated_at",
    }


def test_member_focus_endpoint_caps_at_100_sessions(client, member, admin, db_file):
    member_id, _ = member("Asha")
    start = datetime.now().replace(microsecond=0) - timedelta(days=5)

    for n in range(105):
        db.upsert_focus_session(member_id, validate_focus_session(finished_session(
            f"s{n:03d}", 50,
        ) | {"started_at": _iso(start + timedelta(minutes=n)),
             "ended_at": _iso(start + timedelta(minutes=n + 1))}), db_file)

    body = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    assert len(body["sessions"]) == 100
    assert body["sessions"][0]["session_id"] == "s104"
    assert body["summary"]["sessions"] == 105


def test_member_focus_endpoint_needs_admin_and_a_known_member(client, member, admin):
    member_id, headers = member()

    assert client.get(f"/api/team/members/{member_id}/focus").status_code == 401
    assert client.get(f"/api/team/members/{member_id}/focus", headers=headers).status_code == 401
    assert client.get("/api/team/members/9999/focus", headers=admin).status_code == 404

    empty = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    assert empty["sessions"] == [] and empty["live"] is None
    assert empty["summary"]["focus_score"] is None


def test_focus_is_live_rules():
    now = datetime.now(timezone.utc)
    fresh = {"status": "running", "received_at": now.isoformat()}

    assert analysis.focus_is_live(fresh, now)
    assert not analysis.focus_is_live({**fresh, "status": "Completed"}, now)
    assert not analysis.focus_is_live({**fresh, "received_at": "garbage"}, now)
    assert not analysis.focus_is_live(fresh, now + timedelta(seconds=31))


# --------------------------------------------------
# Live demo
# --------------------------------------------------

def _run_demo_member(profile_name, minutes, seed=4):
    start = (datetime.now() - timedelta(hours=3)).replace(microsecond=0)
    member = live_demo.DemoMember(
        live_demo.NAME_PREFIX + profile_name, PROFILES[profile_name], seed, start
    )
    rows = []

    for step in range(1, minutes * 12):
        member.sample(start + timedelta(seconds=5 * step), 5)
        rows.extend(member.take_focus_rows())

    return rows


def test_demo_members_run_focus_sessions_the_server_accepts():
    rows = _run_demo_member("Asha (focused dev)", 120)
    finished = [r for r in rows if r["status"] != "running"]
    running = [r for r in rows if r["status"] == "running"]

    assert running, "no focus session started in two hours"
    assert finished, "no focus session ended in two hours"

    for row in rows:
        validate_focus_session(row)  # raises when the server would refuse it

    for row in finished:
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["ended_at"])
        assert end - start == timedelta(minutes=live_demo.FOCUS_MINUTES)
        assert row["allowed_apps"] == ["Visual Studio Code", "Windows Terminal"]


def test_eli_deviates_more_than_asha():
    def finished(profile_name):
        return [
            row
            for seed in range(4)
            for row in live_demo.history_focus_sessions(
                profile_name, PROFILES[profile_name], seed, 5, datetime.now()
            )
        ]

    eli = finished("Eli (context switcher)")
    asha = finished("Asha (focused dev)")

    assert len(eli) == len(asha) == 20
    assert sum(r["deviations"] for r in eli) > 3 * max(1, sum(r["deviations"] for r in asha))
    assert sum(r["score"] for r in eli) < sum(r["score"] for r in asha)


def test_demo_streams_focus_sessions_to_the_team_page(demo_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(live_demo, "FOCUS_FIRST_MINUTES", (0, 0))
    [saved] = live_demo.ensure_members(SERVER, history_days=2)
    admin = {"X-Admin-Key": ADMIN_KEY}

    history = demo_server.get(
        f"/api/team/members/{saved['member_id']}/focus", headers=admin
    ).get_json()
    assert [s["status"] for s in history["sessions"]] == ["Completed", "Completed"]

    now = datetime.now().replace(microsecond=0)
    name = saved["name"]
    stream = live_demo.DemoMember(name, PROFILES[name[len(live_demo.NAME_PREFIX):]],
                                  seed=1, now=now)
    streams = {name: stream}
    pending = {name: [stream.sample(now + timedelta(seconds=5), 5)]}

    live_demo.flush(SERVER, streams, {name: saved["token"]}, pending)

    overview = demo_server.get("/api/team/overview", headers=admin).get_json()
    focus = overview["members"][0]["focus"]
    assert focus["live"] is not None and focus["live"]["running"] is True
    assert focus["sessions"] == 3
    assert focus["score"] is not None


def test_demo_keeps_focus_rows_while_the_server_is_unreachable(monkeypatch):
    now = datetime.now().replace(microsecond=0)
    name = live_demo.NAME_PREFIX + "Asha (focused dev)"
    stream = live_demo.DemoMember(name, PROFILES["Asha (focused dev)"], 1, now)
    stream.start_focus(now)
    row = stream.sample(now + timedelta(seconds=5), 5)

    monkeypatch.setattr(live_demo, "upload", lambda *a: 1)

    def unreachable(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(live_demo, "upload_focus", unreachable)
    pending = {name: [row]}
    live_demo.flush(SERVER, {name: stream}, {name: "t"}, pending)

    # Observations went up; the focus row waits for the next upload.
    assert pending[name] == []
    assert len(stream.focus_outbox) == 1


# --------------------------------------------------
# Review fixes: monotonic upserts, abandoned sessions, limits, score
# --------------------------------------------------

def test_late_running_row_never_brings_a_finished_session_back(client, member, admin, db_file):
    member_id, headers = member("Asha")
    now = datetime.now().replace(microsecond=0)
    running = focus_session(score=91, updated_at=_iso(now - timedelta(seconds=30)))
    final = focus_session(
        status="Completed", ended_at=_iso(now), updated_at=_iso(now),
        deviations=4, deviation_seconds=120, score=68,
        current_app=None, current_allowed=None, remaining_s=None,
    )

    assert share(client, headers, running).status_code == 200
    assert share(client, headers, final).status_code == 200
    # A retry from another process (the old pending row) arrives late:
    # answered ok (nothing to retry) but ignored.
    late = share(client, headers, running)
    assert late.status_code == 200 and late.get_json() == {"ok": True}

    [row] = stored(db_file)
    assert row["status"] == "Completed" and row["score"] == 68
    assert row["ended_at"] == final["ended_at"]

    body = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    assert body["live"] is None
    assert body["summary"]["focus_score"] == 68.0
    assert body["sessions"][0]["status"] == "Completed"


def test_older_running_row_does_not_replace_a_newer_one(client, member, db_file):
    _, headers = member("Asha")
    now = datetime.now().replace(microsecond=0)

    share(client, headers, focus_session(remaining_s=600, updated_at=_iso(now)))
    share(client, headers, focus_session(
        remaining_s=900, updated_at=_iso(now - timedelta(seconds=5))
    ))

    [row] = stored(db_file)
    assert row["remaining_s"] == 600


def test_abandoned_running_session_counts_as_interrupted(client, member, admin, db_file):
    member_id, headers = member("Asha")
    share(client, headers, finished_session(
        "good", 100, deviations=0, deviation_seconds=0,
    ))
    share(client, headers, focus_session(
        session_id="gone", deviations=12, deviation_seconds=600, score=0,
    ))
    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE focus_sessions SET received_at = ? WHERE session_id = 'gone'",
            ((datetime.now(timezone.utc) - timedelta(hours=2, minutes=40))
             .isoformat(timespec="seconds"),),
        )

    body = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["gone"]["status"] == "Interrupted"
    assert by_id["gone"]["live"] is False
    assert body["summary"]["focus_score"] == 50.0
    assert body["live"] is None

    overview = client.get("/api/team/overview", headers=admin).get_json()
    assert overview["members"][0]["focus"]["score"] == 50.0
    live = client.get("/api/team/live", headers=admin).get_json()
    assert live["members"][0]["focus"]["score"] == 50.0

    # The real end still arrives later (kept by the collector): it wins.
    final = focus_session(
        session_id="gone", status="Stopped", ended_at=_iso(datetime.now()),
        deviations=12, deviation_seconds=600, score=0,
        current_app=None, current_allowed=None, remaining_s=None,
    )
    assert share(client, headers, final).status_code == 200
    [row] = [r for r in stored(db_file) if r["session_id"] == "gone"]
    assert row["status"] == "Stopped"


def test_running_session_briefly_unheard_is_not_yet_interrupted(client, member, admin, db_file):
    member_id, headers = member("Asha")
    share(client, headers, focus_session())
    _age_received(db_file, analysis.FOCUS_ABANDONED_SECONDS - 10)

    body = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    assert body["sessions"][0]["status"] == "running"
    assert body["summary"]["focus_score"] is None


def test_new_sessions_per_member_are_rate_limited(client, member, monkeypatch, db_file):
    from team import api as team_api

    monkeypatch.setattr(team_api, "FOCUS_MAX_NEW_SESSIONS_PER_DAY", 3)
    _, headers = member("Asha")
    _, ben = member("Ben")

    for n in range(3):
        assert share(client, headers, focus_session(session_id=f"s{n}")).status_code == 200

    refused = share(client, headers, focus_session(session_id="s3"))
    assert refused.status_code == 429
    assert "error" in refused.get_json()

    # Updates of a known session, and other members, are not limited.
    assert share(client, headers, focus_session(session_id="s0", remaining_s=10)).status_code == 200
    assert share(client, ben, focus_session(session_id="s3")).status_code == 200
    assert len(stored(db_file)) == 4


def test_sql_focus_summary_matches_the_row_by_row_rules(client, member, admin, db_file):
    member_id, headers = member("Asha")
    for n, (status, devs) in enumerate([("Completed", 0), ("Stopped", 3),
                                         ("Interrupted", 7), ("running", 1)]):
        session = finished_session(f"s{n}", 100, started_days_ago=n + 1,
                                   deviations=devs, deviation_seconds=30 * devs)
        if status == "running":
            session = focus_session(session_id=f"s{n}", deviations=devs)
        else:
            session["status"] = status
        share(client, headers, session)

    rows = db.focus_sessions(member_id, db_file)
    expected = analysis.focus_summary(rows)
    body = client.get(f"/api/team/members/{member_id}/focus", headers=admin).get_json()
    assert body["summary"] == expected
    assert analysis.focus_overview(db_file)[member_id]["score"] == expected["focus_score"]


def test_focus_queries_use_indexes(db_file):
    with db.connect(db_file) as conn:
        window = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT member_id, COUNT(*) FROM focus_sessions "
            "WHERE started_at >= ? GROUP BY member_id", ("2026-01-01",)
        ))
        live = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM focus_sessions "
            "WHERE status = ? AND received_at >= ? AND received_at <= ?",
            ("running", "a", "b"),
        ))
    assert "idx_focus_sessions" in window or "INDEX" in window
    assert "idx_focus_sessions_status_received" in live


def test_server_score_follows_the_deviations():
    clean = validate_focus_session(focus_session(
        status="Completed", ended_at=_iso(datetime.now()),
        deviations=40, deviation_seconds=1400, score=100,
        current_app=None, current_allowed=None, remaining_s=None,
    ))
    assert clean["score"] == 0

    # A lower score than the formula is kept.
    assert validate_focus_session(focus_session(score=50))["score"] == 50


@pytest.mark.parametrize("overrides", [
    {"planned_minutes": 481},
    {"planned_minutes": 10080},
    {"planned_minutes": 25, "remaining_s": 25 * 60 + 61},
])
def test_planned_minutes_and_remaining_time_are_bounded(overrides):
    with pytest.raises(FocusValidationError):
        validate_focus_session(focus_session(**overrides))


def test_planned_limits_still_accept_the_largest_session():
    clean = validate_focus_session(focus_session(planned_minutes=480, remaining_s=480 * 60))
    assert clean["planned_minutes"] == 480
