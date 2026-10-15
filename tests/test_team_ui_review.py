"""
Server behaviour the Team pages' "Revoke" button relies on.

The dashboard decides that a member is a pending invite from data that can
be seconds (or, while paused, minutes) old, and DELETE /api/team/members/<id>
removes a member whatever their state. So the frontend
(revokePendingInvite in src/data/team.js) never sends a plain DELETE to
revoke: it first rotates the code with POST /members/<id>/invite, which

  * answers 409 once the member has joined (then nothing is deleted), and
  * makes the code the member was sent useless, so nobody can join
    between that check and the DELETE.

These tests pin both halves of that contract.
"""

from team import db

from tests.conftest import consent, observation, recent_timestamp


def _create_invite(client, admin, name="Pending"):
    response = client.post("/api/team/invites", json={"name": name}, headers=admin)
    assert response.status_code == 201
    body = response.get_json()
    return body["member_id"], body["invite_code"]


def _join(client, code):
    return client.post(
        "/api/team/join", json={"invite_code": code, "consent": consent()}
    )


def _status_in_overview(client, admin, member_id):
    members = client.get("/api/team/overview", headers=admin).get_json()["members"]
    return next(m["status"] for m in members if m["id"] == member_id)


def _revoke_like_the_dashboard(client, admin, member_id):
    """
    The requests revokePendingInvite sends. Returns the status codes.
    """

    check = client.post(f"/api/team/members/{member_id}/invite", headers=admin)

    if check.status_code != 201:
        return check.status_code, None

    delete = client.delete(f"/api/team/members/{member_id}", headers=admin)
    return check.status_code, delete.status_code


def test_revoke_after_the_member_joined_deletes_nothing(client, admin, db_file):
    member_id, code = _create_invite(client, admin)

    # What the page last drew.
    assert _status_in_overview(client, admin, member_id) == "invited"

    # Meanwhile the member joins and starts sharing.
    joined = _join(client, code)
    assert joined.status_code == 201
    token = {"Authorization": f"Bearer {joined.get_json()['token']}"}
    rows = [observation(recent_timestamp(i * 5)) for i in range(20)]
    assert client.post("/api/team/ingest", json={"observations": rows}, headers=token).status_code == 200

    # The admin confirms "Revoke" on the stale row.
    assert _revoke_like_the_dashboard(client, admin, member_id) == (409, None)

    # The member, their data and their token are untouched.
    detail = client.get(f"/api/team/members/{member_id}", headers=admin)
    assert detail.status_code == 200
    assert db.member_with_stats(member_id, db_file)["observation_count"] == 20
    assert client.get("/api/team/me", headers=token).status_code == 200


def test_revoke_of_a_pending_invite_removes_it_and_its_code(client, admin):
    member_id, code = _create_invite(client, admin)

    assert _revoke_like_the_dashboard(client, admin, member_id) == (201, 200)

    assert client.get(f"/api/team/members/{member_id}", headers=admin).status_code == 404
    assert _join(client, code).status_code == 403


def test_the_check_locks_out_the_code_the_member_was_sent(client, admin, db_file):
    """
    Between the check and the DELETE the member can no longer join, so
    the DELETE can only ever remove a member who never joined.
    """

    member_id, code = _create_invite(client, admin)

    check = client.post(f"/api/team/members/{member_id}/invite", headers=admin)
    assert check.status_code == 201

    # A join that lands between the two requests fails...
    assert _join(client, code).status_code == 403
    member = db.member_with_stats(member_id, db_file)
    assert member["joined_at"] is None
    assert member["observation_count"] == 0

    # ...so the DELETE that follows removes only the unused invite.
    assert client.delete(f"/api/team/members/{member_id}", headers=admin).status_code == 200


def test_revoke_of_an_expired_invite_works(client, admin, db_file):
    member_id, _ = _create_invite(client, admin)

    with db.connect(db_file) as conn:
        conn.execute(
            "UPDATE members SET invite_expires = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (member_id,),
        )

    assert _status_in_overview(client, admin, member_id) == "expired"
    assert _revoke_like_the_dashboard(client, admin, member_id) == (201, 200)


def test_revoke_of_a_member_removed_elsewhere_is_404_without_delete(client, admin):
    member_id, _ = _create_invite(client, admin)
    assert client.delete(f"/api/team/members/{member_id}", headers=admin).status_code == 200

    assert _revoke_like_the_dashboard(client, admin, member_id) == (404, None)
