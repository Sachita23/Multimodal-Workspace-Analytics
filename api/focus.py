"""
Focus endpoints for the dashboard.

    GET  /api/focus/live            the running session (engine or assistant)
    GET  /api/focus/summary         totals across every recorded session
    GET  /api/focus/sessions        every session, newest first
    GET  /api/focus/apps            applications to offer for a session

    POST /api/focus/session         start a session here (focus/engine.py)
    POST /api/focus/session/stop    end it
    POST /api/focus/session/extend  add minutes
    POST /api/focus/session/allow   add an application to its focus zone

Everything is read from config.FOCUS_DATA_DIR (app.config["FOCUS_DATA_DIR"]
overrides it, e.g. in tests). Sessions started here run in one FocusEngine
per Flask app (app.extensions["focus_engine"]), on a background thread.

Every POST needs Content-Type: application/json (else 415): a browser
only sends that cross-origin after a CORS preflight, which this API never
grants, so another website cannot start or stop sessions. A page that
rebinds its own DNS name to 127.0.0.1 is same-origin with this API and
needs no preflight, so every focus request must also name this machine
in its Host header (localhost, an IP address, or a configured host),
and a POST that carries an Origin header must come from such a host too
(403 otherwise). A request with the team admin key passes. api/server.py's
loopback / admin-key rule applies on top. This module must not import
tkinter.
"""

import hmac
import ipaddress
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request


# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_DIR = Path(__file__).resolve().parent


def _project_root_first():
    """
    `python api/server.py` puts api/ on sys.path. There this file would
    shadow the top-level focus package, so the project root must come
    before api/.
    """

    entries = [os.path.realpath(entry or os.getcwd()) for entry in sys.path]
    root = os.path.realpath(PROJECT_ROOT)
    api_dir = os.path.realpath(API_DIR)

    if root not in entries:
        sys.path.insert(0, str(PROJECT_ROOT))
    elif api_dir in entries and entries.index(api_dir) < entries.index(root):
        sys.path.insert(0, str(PROJECT_ROOT))


_project_root_first()

import config  # noqa: E402
from focus import analytics as focus_analytics  # noqa: E402
from focus import apps as focus_apps  # noqa: E402
from focus import engine as focus_engine  # noqa: E402


# ---------------------------------------------------------
# Blueprint
# ---------------------------------------------------------

focus_api = Blueprint("focus_api", __name__, url_prefix="/api/focus")

SUMMARY_KEYS = (
    "sessions",
    "completed",
    "adherence_pct",
    "focus_seconds_today",
    "focus_seconds_total",
    "deviation_count",
    "deviation_sources",
    "focus_score",
)

ENGINE_KEY = "focus_engine"

_engine_lock = threading.Lock()


def _data_dir():
    return Path(current_app.config.get("FOCUS_DATA_DIR") or config.FOCUS_DATA_DIR)


def _fresh(payload, status=200):
    # The files change every second while a session runs.
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message, status):
    return _fresh({"error": str(message)}, status)


def _running_engine():
    """
    The app's FocusEngine when it runs a session, else None.
    """

    engine = current_app.extensions.get(ENGINE_KEY)
    return engine if engine is not None and engine.running else None


def _engine():
    """
    The app's FocusEngine, created on first use.
    """

    with _engine_lock:
        engine = current_app.extensions.get(ENGINE_KEY)

        if engine is None:
            engine = focus_engine.FocusEngine(data_dir=_data_dir(), source="dashboard")
            current_app.extensions[ENGINE_KEY] = engine

        return engine


# ---------------------------------------------------------
# Host and Origin (DNS rebinding)
# ---------------------------------------------------------

LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain"}

# The public address `start_project.py --tunnel` sets (api/server.py).
PUBLIC_URL_ENV = "WORKSPACE_PUBLIC_URL"


def _hostname(value):
    """
    The lower-case host name of a Host header or URL, without the port
    (None when there is none).
    """

    value = (value or "").strip()

    if not value:
        return None

    try:
        parts = urlsplit(value if "//" in value else f"//{value}")
        host = parts.hostname
    except ValueError:
        return None

    return host.lower().rstrip(".") if host else None


def _configured_hosts():
    hosts = set()

    for value in (
        getattr(config, "API_HOST", ""),
        getattr(config, "DASHBOARD_URL", ""),
        os.environ.get(PUBLIC_URL_ENV, ""),
        current_app.config.get("FOCUS_ALLOWED_HOST", ""),
    ):
        host = _hostname(value)

        if host and host not in ("0.0.0.0", "::"):
            hosts.add(host)

    return hosts


def is_trusted_host(host):
    """
    True for a host name that cannot be a rebound DNS name: localhost
    (and *.localhost), an IP address, or a host the configuration names.
    """

    if not host:
        return False

    if host in LOCAL_HOST_NAMES or host.endswith(".localhost"):
        return True

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass

    return host in _configured_hosts()


def _has_admin_key():
    supplied = request.headers.get("X-Admin-Key", "")
    expected = current_app.config.get("TEAM_ADMIN_KEY") or ""

    return bool(expected and supplied) and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


@focus_api.before_request
def _same_machine_only():
    """
    Refuse requests addressed to a foreign host name (DNS rebinding),
    and POSTs from a page on one.
    """

    if _has_admin_key():
        return None

    if not is_trusted_host(_hostname(request.host)):
        return _error("This request was addressed to an unknown host name.", 403)

    origin = request.headers.get("Origin")

    if request.method == "POST" and origin is not None:
        # "null" (a sandboxed page or file) is not a trusted origin either.
        if not is_trusted_host(_hostname(origin)):
            return _error("Focus sessions can only be controlled from the dashboard.", 403)

    return None


def _json_body():
    """
    (body dict, None) or (None, error response).
    """

    if request.mimetype != "application/json":
        return None, _error("Send the request as JSON (Content-Type: application/json).", 415)

    body = request.get_json(silent=True)

    if body is None:
        body = {} if not request.get_data() else None

    if not isinstance(body, dict):
        return None, _error("The request body must be a JSON object.", 400)

    return body, None


# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------

@focus_api.get("/live")
def focus_live():
    """
    The engine's live state while it runs a session here; otherwise the
    heartbeat in live.json, with stale=true (and running=false) when it
    is missing or its updated_at is older than 5 s.
    """

    engine = _running_engine()

    if engine is not None:
        # ?viewing=<browser app>: the Focus page is the visible, focused
        # tab there, so looking at the timer is not a deviation.
        viewing = request.args.get("viewing", "").strip()

        if viewing:
            engine.note_dashboard_view(viewing)

        return _fresh(engine.live())

    return _fresh(focus_analytics.live_status(_data_dir()))


@focus_api.get("/summary")
def focus_summary():
    records = focus_analytics.session_records(_data_dir())
    summary = focus_analytics.summarize(records)

    return _fresh({key: summary[key] for key in SUMMARY_KEYS})


@focus_api.get("/sessions")
def focus_sessions():
    records = focus_analytics.session_records(_data_dir())

    return _fresh({
        "sessions": [focus_analytics.session_json(record) for record in records]
    })


@focus_api.get("/apps")
def focus_apps_list():
    return _fresh({
        "running": focus_apps.running_applications(),
        "recent": focus_apps.recent_applications(
            raw_file=current_app.config.get("RAW_DATA_FILE")
        ),
    })


# ---------------------------------------------------------
# Session control (focus/engine.py)
# ---------------------------------------------------------

@focus_api.post("/session")
def focus_session_start():
    body, error = _json_body()

    if error is not None:
        return error

    title = body.get("title")
    minutes = body.get("duration_minutes")
    allowed_apps = body.get("allowed_apps")
    goal = body.get("goal")

    if not isinstance(title, str):
        return _error("title must be text.", 400)

    if goal is not None and not isinstance(goal, str):
        return _error("goal must be text.", 400)

    engine = _engine()

    try:
        live = engine.start(title, minutes, allowed_apps, goal=goal)
    except focus_engine.FocusBusy as busy:
        return _error(busy, 409)
    except ValueError as invalid:
        return _error(invalid, 400)

    engine.start_background()
    return _fresh(live, 201)


@focus_api.post("/session/stop")
def focus_session_stop():
    _body, error = _json_body()

    if error is not None:
        return error

    engine = _running_engine()

    if engine is None:
        return _error("No focus session is running.", 409)

    # Always "Stopped": only the timer running out completes a session.
    session = engine.stop("Stopped")
    return _fresh({"stopped": True, "session": session})


@focus_api.post("/session/extend")
def focus_session_extend():
    body, error = _json_body()

    if error is not None:
        return error

    engine = _running_engine()

    if engine is None:
        return _error("No focus session is running.", 409)

    try:
        return _fresh(engine.extend(body.get("minutes")))
    except focus_engine.FocusNotRunning as idle:
        return _error(idle, 409)
    except ValueError as invalid:
        return _error(invalid, 400)


@focus_api.post("/session/allow")
def focus_session_allow():
    body, error = _json_body()

    if error is not None:
        return error

    engine = _running_engine()

    if engine is None:
        return _error("No focus session is running.", 409)

    try:
        return _fresh(engine.allow(body.get("app")))
    except focus_engine.FocusNotRunning as idle:
        return _error(idle, 409)
    except ValueError as invalid:
        return _error(invalid, 400)
