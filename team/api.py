"""
Flask blueprint for team collection and analysis.

Member endpoints (Authorization: Bearer <member token>):
    POST   /api/team/join      redeem invite + recorded consent -> token
    POST   /api/team/ingest    upload a batch of observations
    GET    /api/team/me        what the server holds about you
    DELETE /api/team/me        leave the team and delete your data

Main-user endpoints (X-Admin-Key: <admin key>):
    POST   /api/team/invites
    GET    /api/team/overview                ?days=1|7|30|all
    GET    /api/team/live                    current activity, ETag
    GET    /api/team/members/<id>            ?days=1|7|30|all
    GET    /api/team/members/<id>/activity   ?limit&before&after&offset
    POST   /api/team/members/<id>/invite     new code for a pending member
    DELETE /api/team/members/<id>
"""

import hmac
import math
import os
import secrets
import socket
import sys
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request

from config import (
    API_HOST,
    API_PORT,
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

# Shorter admin keys (including an empty key file) are treated as
# missing and replaced, so the admin API is never left open.
MIN_ADMIN_KEY_LENGTH = 16

# Observations older than this are rejected at ingest.
MAX_OBSERVATION_AGE_DAYS = 90

DAY_CHOICES = {"1": 1, "7": 7, "30": 30, "all": None}


# --------------------------------------------------
# Setup
# --------------------------------------------------

def _usable_key(key):
    key = (key or "").strip()
    return key if len(key) >= MIN_ADMIN_KEY_LENGTH else None


def _write_key_file(path, key, replace):
    """
    Write the key with owner-only permissions via a temp file, so
    a crash never leaves an empty or half-written key file.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".admin_key.")

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(key)

        os.chmod(tmp, 0o600)

        if replace:
            os.replace(tmp, path)
        else:
            # Exclusive create: if another process created the
            # file first, keep theirs.
            os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_admin_key(path=None):
    """
    The main user's key: WORKSPACE_TEAM_ADMIN_KEY if set,
    otherwise read from (or created in) data/team/admin_key.

    A key shorter than MIN_ADMIN_KEY_LENGTH characters (including
    an empty or whitespace-only file) is never used: the file is
    replaced with a new random key.
    """

    path = path or TEAM_ADMIN_KEY_FILE
    env_key = os.environ.get("WORKSPACE_TEAM_ADMIN_KEY")

    if env_key is not None and env_key.strip():
        key = _usable_key(env_key)

        if key:
            return key

        print(
            f"[WARNING] WORKSPACE_TEAM_ADMIN_KEY is shorter than "
            f"{MIN_ADMIN_KEY_LENGTH} characters and was ignored; "
            f"using the key in {path} instead.",
            file=sys.stderr,
        )

    for _ in range(3):
        if path.exists():
            key = _usable_key(path.read_text(encoding="utf-8"))

            if key:
                return key

            # Empty, whitespace or too short: replace it.
            print(
                f"[WARNING] The admin key in {path} was empty or shorter "
                f"than {MIN_ADMIN_KEY_LENGTH} characters; a new key was "
                "generated.",
                file=sys.stderr,
            )
            key = secrets.token_urlsafe(24)
            _write_key_file(path, key, replace=True)
            return key

        key = secrets.token_urlsafe(24)

        try:
            _write_key_file(path, key, replace=False)
            return key
        except FileExistsError:
            continue  # created by another process; read theirs
        except OSError:
            # Filesystems without hard links.
            _write_key_file(path, key, replace=True)
            return key

    raise RuntimeError(f"Could not read or create the admin key at {path}")


def init_team(app, admin_key=None, db_file=None):
    """
    Register the blueprint and prepare storage.
    """

    admin_key = (admin_key or "").strip()

    if admin_key and len(admin_key) < MIN_ADMIN_KEY_LENGTH:
        raise ValueError(
            f"The admin key must be at least {MIN_ADMIN_KEY_LENGTH} characters."
        )

    # A str path (env var, command line) works the same as a Path.
    db_file = Path(db_file) if db_file else None

    app.config["TEAM_DB_FILE"] = db_file
    app.config["TEAM_ADMIN_KEY"] = admin_key or load_admin_key()
    app.config.setdefault("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)

    db.init_db(db_file)
    app.register_blueprint(team_api)


def member_server_url():
    """
    The address members should use, or None when the server only
    listens on this machine (members could not reach it).
    """

    if API_HOST in ("127.0.0.1", "localhost", "::1"):
        return None

    host = API_HOST

    if host in ("0.0.0.0", "::"):
        # Connecting a UDP socket sends nothing; it just picks the
        # interface used for outside traffic.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 80))
                host = probe.getsockname()[0]
        except OSError:
            return None

    return f"http://{host}:{API_PORT}"


def _db_file():
    return current_app.config.get("TEAM_DB_FILE")


def _error(message, status):
    return jsonify({"error": message}), status


def _json_object():
    """
    The request's JSON body as a dict ({} when there is no body),
    or None when it is valid JSON but not an object.
    """

    body = request.get_json(silent=True)

    if body is None:
        return {}

    return body if isinstance(body, dict) else None


def _days_arg():
    """
    The ?days= window (1, 7, 30 or all; default TEAM_ANALYSIS_DAYS).
    Returns (days, error_response).
    """

    raw = request.args.get("days")

    if raw is None or raw == "":
        return analysis.ANALYSIS_DAYS, None

    if raw not in DAY_CHOICES:
        return None, _error("days must be 1, 7, 30 or all.", 400)

    return DAY_CHOICES[raw], None


def _timestamp_arg(name):
    """
    A "YYYY-MM-DD HH:MM:SS" query argument, or None.
    Raises ValueError when it is present but malformed.
    """

    raw = request.args.get(name)

    if raw is None or raw == "":
        return None

    return datetime.strptime(raw, TIMESTAMP_FORMAT).strftime(TIMESTAMP_FORMAT)


# --------------------------------------------------
# Auth
# --------------------------------------------------

def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-Admin-Key", "")
        expected = current_app.config.get("TEAM_ADMIN_KEY") or ""

        # An empty key never matches, even an empty header.
        if (
            not expected
            or not supplied
            or not hmac.compare_digest(supplied.encode(), expected.encode())
        ):
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
        # but reject obviously wrong clocks, and very old rows.
        now = datetime.now()
        if timestamp > now + timedelta(days=1):
            return None
        if timestamp < now - timedelta(days=MAX_OBSERVATION_AGE_DAYS):
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
    body = _json_object()

    if body is None:
        return _error("Expected a JSON object.", 400)

    consent = body.get("consent") or {}

    if not isinstance(consent, dict):
        return _error("Expected 'consent' to be an object.", 400)

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
    body = _json_object()

    if body is None:
        return _error("Expected a JSON object.", 400)

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
    member = db.member_with_stats(g.member["id"], _db_file())

    if member is None:
        return _error("Unknown or revoked member token.", 401)

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

def _invite_response(member_id, code):
    member = db.get_member(member_id, _db_file())
    server_url = member_server_url()
    server = server_url or f"http://YOUR-IP:{API_PORT}"

    return jsonify({
        "member_id": member_id,
        "name": member["name"],
        "invite_code": code,
        "expires": member["invite_expires"],
        "server_url": server_url,
        "port": API_PORT,
        "join_command": (
            f"python collector/team_sync.py join --server {server} "
            f"--invite {code}"
        ),
        "simulated": bool(member.get("simulated")),
    }), 201


@team_api.post("/invites")
@require_admin
def create_invite():
    body = _json_object()

    if body is None:
        return _error("Expected a JSON object.", 400)

    name = str(body.get("name", "")).strip()

    if not name or len(name) > 80:
        return _error("Give the member a name (up to 80 characters).", 400)

    simulated = body.get("simulated", False)

    if not isinstance(simulated, bool):
        return _error("'simulated' must be true or false.", 400)

    member_id, code = db.create_invite(name, _db_file(), simulated=simulated)

    return _invite_response(member_id, code)


@team_api.post("/members/<int:member_id>/invite")
@require_admin
def reissue_invite(member_id):
    """
    A new code and expiry for a member who has not joined yet
    (pending or expired invite). The old code stops working.
    """

    if db.get_member(member_id, _db_file()) is None:
        return _error("No such member.", 404)

    code = db.reissue_invite(member_id, _db_file())

    if code is None:
        return _error("This member has already joined.", 409)

    return _invite_response(member_id, code)


@team_api.get("/overview")
@require_admin
def overview():
    days, error = _days_arg()

    if error:
        return error

    return jsonify(analysis.team_overview(_db_file(), days))


@team_api.get("/live")
@require_admin
def live():
    """
    What each member is doing now. SQL only; unchanged polls
    (matching If-None-Match) get 304 without reading observations.
    """

    members, tag = analysis.live_signature(_db_file())

    if request.if_none_match.contains(tag):
        response = current_app.response_class(status=304)
    else:
        response = jsonify(analysis.team_live(_db_file(), members))

    response.set_etag(tag)
    response.headers["Cache-Control"] = "no-cache"

    return response


@team_api.get("/members/<int:member_id>")
@require_admin
def member_detail(member_id):
    days, error = _days_arg()

    if error:
        return error

    detail = analysis.member_detail(member_id, _db_file(), days)

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

    try:
        before = _timestamp_arg("before")
        after = _timestamp_arg("after")
    except ValueError:
        return _error("before / after must look like YYYY-MM-DD HH:MM:SS.", 400)

    rows, total, has_more = db.recent_observations(
        member_id, limit, offset, _db_file(), before=before, after=after
    )

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
        "has_more": has_more,
    })


@team_api.delete("/members/<int:member_id>")
@require_admin
def remove_member(member_id):
    if not db.delete_member(member_id, _db_file()):
        return _error("No such member.", 404)

    return jsonify({"deleted": True})
