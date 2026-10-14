from datetime import datetime, timedelta

from config import TEAM_MAX_BATCH
from team import db
from tests.conftest import consent, observation


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
    rows = [observation("2026-10-01 09:00:00"), observation("2026-10-01 09:00:05")]

    first = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert first.get_json() == {"accepted": 2, "duplicates": 0, "rejected": 0}

    # A retried upload doesn't create duplicates.
    again = client.post("/api/team/ingest", json={"observations": rows}, headers=headers)
    assert again.get_json() == {"accepted": 0, "duplicates": 2, "rejected": 0}


def test_ingest_rejects_invalid_rows_but_keeps_valid_ones(client, member):
    _, headers = member()
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    rows = [
        observation("2026-10-01 09:00:00"),
        observation("not a time"),
        observation("2026-10-01 09:00:10", cpu_percent=150),
        observation("2026-10-01 09:00:15", activity_status="Hacking"),
        observation("2026-10-01 09:00:20", charging="yes"),
        observation("2026-10-01 09:00:25", idle_seconds=float("nan")),
        observation(future),
        {"timestamp": "2026-10-01 09:00:30"},
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

    rows, _ = db.recent_observations(member_id, db_file=db_file)
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
        observation(f"2026-10-01 09:00:{second:02d}", window_title=f"w{second}")
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
