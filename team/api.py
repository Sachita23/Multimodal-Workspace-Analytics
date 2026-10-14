"""
Flask blueprint for team collection and analysis.

Member endpoints (Authorization: Bearer <member token>):
    POST   /api/team/join      redeem invite + recorded consent -> token
    POST   /api/team/ingest    upload a batch of observations
    GET    /api/team/me        what the server holds about you
    DELETE /api/team/me        leave the team and delete your data

Main-user endpoints (X-Admin-Key: <admin key>):
    POST   /api/team/invites
    GET    /api/team/overview
    GET    /api/team/members/<id>
    GET    /api/team/members/<id>/activity
    DELETE /api/team/members/<id>
"""

import hmac
import math
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from config import (
    TEAM_ADMIN_KEY_FILE,
    TEAM_CONSENT_VERSION,
    TEAM_MAX_BATCH,
    TEAM_SHARED_FIELDS,
)
from team import analysis, db


team_api = Blueprint("team_api", __name__, url_prefix="/api/team")

MAX_TEXT_LENGTH = 512
ACTIVITY_STATUSES = {"Active", "Idle", "Unknown"}
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------
# Setup
# --------------------------------------------------

def load_admin_key():
    """
    The main user's key: WORKSPACE_TEAM_ADMIN_KEY if set,
    otherwise read from (or created in) data/team/admin_key.
    """

    key = os.environ.get("WORKSPACE_TEAM_ADMIN_KEY")

    if key:
        return key

    if TEAM_ADMIN_KEY_FILE.exists():
        return TEAM_ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()

    TEAM_ADMIN_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(24)

    fd = os.open(TEAM_ADMIN_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(key)

    return key


def init_team(app, admin_key=None, db_file=None):
    """
    Register the blueprint and prepare storage.
    """

    app.config["TEAM_DB_FILE"] = db_file
    app.config["TEAM_ADMIN_KEY"] = admin_key or load_admin_key()
    app.config.setdefault("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)

    db.init_db(db_file)
    app.register_blueprint(team_api)


def _db_file():
    return current_app.config.get("TEAM_DB_FILE")


def _error(message, status):
    return jsonify({"error": message}), status


# --------------------------------------------------
# Auth
# --------------------------------------------------

def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-Admin-Key", "")
        expected = current_app.config["TEAM_ADMIN_KEY"]

        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return _error("Admin key required.", 401)

        return view(*args, **kwargs)

    return wrapper


def require_member(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        member = db.member_for_token(token, _db_file())

        if member is None:
            return _error("Unknown or revoked member token.", 401)

        g.member = member
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------
# Validation
# --------------------------------------------------

def _number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    value = float(value)
    if not math.isfinite(value):
        raise ValueError
    if low is not None and value < low:
        raise ValueError
    if high is not None and value > high:
        raise ValueError
    return value


def _text(value):
    if not isinstance(value, str):
        raise ValueError
    value = value.strip() or "Unknown"
    return value[:MAX_TEXT_LENGTH]


def validate_observation(raw):
    """
    Return a clean observation dict, or None if it is invalid.
    """

    if not isinstance(raw, dict):
        return None

    try:
        timestamp = datetime.strptime(raw["timestamp"], TIMESTAMP_FORMAT)

        # Collector clocks are local time; allow for time zones
        # but reject obviously wrong clocks.
        if timestamp > datetime.now() + timedelta(days=1):
            return None

        status = raw["activity_status"]
        if status not in ACTIVITY_STATUSES:
            return None

        if not isinstance(raw["charging"], bool):
            return None

        return {
            "timestamp": timestamp.strftime(TIMESTAMP_FORMAT),
            "application": _text(raw["application"]),
            "window_title": _text(raw["window_title"]),
            "activity_status": status,
            "idle_seconds": _number(raw["idle_seconds"], low=0),
            "cpu_percent": _number(raw["cpu_percent"], 0, 100),
            "memory_percent": _number(raw["memory_percent"], 0, 100),
            "battery_percent": _number(raw["battery_percent"], 0, 100),
            "charging": raw["charging"],
        }

    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------
# Member endpoints
# --------------------------------------------------

@team_api.post("/join")
def join():
    body = request.get_json(silent=True) or {}
    consent = body.get("consent") or {}

    if consent.get("accepted") is not True:
        return _error("Consent was not given.", 400)

    if (
        consent.get("version") != TEAM_CONSENT_VERSION
        or consent.get("fields") != TEAM_SHARED_FIELDS
    ):
        return _error(
            "Your collector shares a different set of fields than this "
            "server expects. Update the project on both machines.",
            409,
        )

    result = db.redeem_invite(
        str(body.get("invite_code", "")),
        TEAM_CONSENT_VERSION,
        TEAM_SHARED_FIELDS,
        _db_file(),
    )

    if result is None:
        return _error("Invite code is invalid, used or expired.", 403)

    member_id, name, token = result

    return jsonify({
        "member_id": member_id,
        "name": name,
        "token": token,
        "consent_version": TEAM_CONSENT_VERSION,
    }), 201


@team_api.post("/ingest")
@require_member
def ingest():
    body = request.get_json(silent=True) or {}
    rows = body.get("observations")

    if not isinstance(rows, list):
        return _error("Expected an 'observations' list.", 400)

    if len(rows) > TEAM_MAX_BATCH:
        return _error(f"At most {TEAM_MAX_BATCH} observations per upload.", 413)

    valid = [row for row in map(validate_observation, rows) if row]
    inserted = db.insert_observations(g.member["id"], valid, _db_file())

    return jsonify({
        "accepted": inserted,
        "duplicates": len(valid) - inserted,
        "rejected": len(rows) - len(valid),
    })


@team_api.get("/me")
@require_member
def me():
    member = next(
        m for m in db.list_members(_db_file()) if m["id"] == g.member["id"]
    )

    return jsonify({
        "member_id": member["id"],
        "name": member["name"],
        "joined_at": member["joined_at"],
        "consent_at": member["consent_at"],
        "shared_fields": TEAM_SHARED_FIELDS,
        "observations_stored": member["observation_count"],
        "first_observation": member["first_observation"],
        "last_observation": member["last_observation"],
    })


@team_api.delete("/me")
@require_member
def leave():
    db.delete_member(g.member["id"], _db_file())
    return jsonify({"deleted": True})


# --------------------------------------------------
# Main-user endpoints
# --------------------------------------------------

@team_api.post("/invites")
@require_admin
def create_invite():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()

    if not name or len(name) > 80:
        return _error("Give the member a name (up to 80 characters).", 400)

    member_id, code = db.create_invite(name, _db_file())
    member = db.get_member(member_id, _db_file())

    return jsonify({
        "member_id": member_id,
        "name": name,
        "invite_code": code,
        "expires": member["invite_expires"],
    }), 201


@team_api.get("/overview")
@require_admin
def overview():
    return jsonify(analysis.team_overview(_db_file()))


@team_api.get("/members/<int:member_id>")
@require_admin
def member_detail(member_id):
    detail = analysis.member_detail(member_id, _db_file())

    if detail is None:
        return _error("No such member.", 404)

    return jsonify(detail)


@team_api.get("/members/<int:member_id>/activity")
@require_admin
def member_activity(member_id):
    if db.get_member(member_id, _db_file()) is None:
        return _error("No such member.", 404)

    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    offset = max(request.args.get("offset", 0, type=int), 0)

    rows, total = db.recent_observations(member_id, limit, offset, _db_file())

    return jsonify({"total": total, "limit": limit, "offset": offset, "rows": rows})


@team_api.delete("/members/<int:member_id>")
@require_admin
def remove_member(member_id):
    if not db.delete_member(member_id, _db_file()):
        return _error("No such member.", 404)

    return jsonify({"deleted": True})
