import argparse
import hmac
import ipaddress
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Flask, current_app, jsonify, request, send_file


# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    API_HOST,
    API_PORT,
    PIPELINE_REFRESH_SECONDS,
    PIPELINE_STATUS_FILE,
    PROCESSED_DATA_FILE,
    RAW_DATA_FILE,
)
from team.api import init_team  # noqa: E402


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# When the API listens beyond this machine, these stay open to every
# computer: the health check, and the team API, which checks member
# tokens and the admin key itself. Everything else (your activity log,
# pipeline status, Focus sessions) answers only to this machine (the
# dashboard through Vite's proxy) or to a request with the admin key.
OPEN_PATHS = frozenset({"/api/health"})
OPEN_PATH_PREFIXES = ("/api/team/",)

# Flask's debugger runs code typed into the browser, so it stays off
# unless this is "1" and the API listens on this machine only.
DEBUG_ENV = "WORKSPACE_API_DEBUG"

# The collector writes a row every few seconds; a raw file older
# than this means it has stopped.
COLLECTOR_STALE_SECONDS = 60

# Keys of status.json (written by live_pipeline.py) that the API
# exposes; the error traceback stays on disk.
PIPELINE_STATUS_KEYS = (
    "last_success",
    "last_error",
    "rows",
    "sessions",
    "last_observation_ts",
    "duration_s",
)

core_api = Blueprint("core_api", __name__)


# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------

@core_api.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "processed_file_exists": PROCESSED_DATA_FILE.exists(),
    })


# ---------------------------------------------------------
# Pipeline / collector status
# ---------------------------------------------------------

def _mtime(path):
    """
    Modification time of `path` as a local datetime, or None.
    """

    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()

    except OSError:
        return None


def _iso(moment):
    return moment.isoformat(timespec="seconds") if moment else None


def _pipeline_status():

    try:
        status = json.loads(PIPELINE_STATUS_FILE.read_text(encoding="utf-8"))

    except (OSError, ValueError):
        return None

    if not isinstance(status, dict):
        return None

    return {key: status.get(key) for key in PIPELINE_STATUS_KEYS}


@core_api.get("/api/status")
def status():

    now = datetime.now().astimezone()
    raw_mtime = _mtime(RAW_DATA_FILE)
    processed_mtime = _mtime(PROCESSED_DATA_FILE)

    collector_stale = (
        raw_mtime is None
        or (now - raw_mtime).total_seconds() > COLLECTOR_STALE_SECONDS
    )

    response = jsonify({
        "status": "ok",
        "server_time": _iso(now),
        "refresh_seconds": PIPELINE_REFRESH_SECONDS,
        "pipeline": _pipeline_status(),
        "raw_mtime": _iso(raw_mtime),
        "processed_mtime": _iso(processed_mtime),
        "collector_stale": collector_stale,
    })

    response.headers["Cache-Control"] = "no-store"

    return response


# ---------------------------------------------------------
# Analytics CSV endpoint
# ---------------------------------------------------------

def _not_available():
    return jsonify({
        "error": "Processed analytics data is not available yet."
    }), 404


@core_api.get("/api/analytics.csv")
def analytics_csv():

    if not PROCESSED_DATA_FILE.exists():
        return _not_available()

    # live_pipeline swaps in a complete new file atomically, so the
    # open file is always one consistent snapshot. The ETag changes
    # with every rewrite; unchanged polls get 304 Not Modified.
    try:
        response = send_file(
            PROCESSED_DATA_FILE,
            mimetype="text/csv",
            conditional=True,
            etag=True,
            max_age=0,
        )

    except FileNotFoundError:
        return _not_available()

    # Always revalidate: the live pipeline keeps updating the CSV.
    response.headers["Cache-Control"] = "no-cache"

    return response


# ---------------------------------------------------------
# Access when listening beyond this machine
# ---------------------------------------------------------

def is_loopback_address(address):
    """
    True for 127.0.0.0/8, ::1 and ::ffff:127.x.x.x; False for anything
    else, including a missing or malformed address.
    """

    try:
        ip = ipaddress.ip_address((address or "").split("%")[0])

    except ValueError:
        return False

    mapped = getattr(ip, "ipv4_mapped", None)

    return (mapped or ip).is_loopback


def is_loopback_host(host):
    """
    True when a server bound to `host` is reachable from this machine only.
    """

    host = (host or "").strip()

    return host in LOOPBACK_HOSTS or is_loopback_address(host.strip("[]"))


# Set by `start_project.py --tunnel`: the public https address a tunnel
# (Cloudflare) forwards to this server. Tunnelled requests reach us from
# 127.0.0.1, so they are told apart by the forwarding headers below.
PUBLIC_URL_ENV = "WORKSPACE_PUBLIC_URL"

# Headers a reverse proxy or tunnel adds. The dashboard's own Vite proxy
# adds none of them, so a loopback request carrying one came from
# another computer through a tunnel.
FORWARDING_HEADERS = ("Cf-Connecting-Ip", "X-Forwarded-For", "Forwarded", "X-Real-Ip")


def public_url():
    return os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")


def is_local_request():
    """
    True for requests made on this machine (the dashboard), not for
    ones that arrived through a tunnel or proxy.
    """

    if not is_loopback_address(request.remote_addr):
        return False

    return not any(request.headers.get(name) for name in FORWARDING_HEADERS)


def _has_admin_key():
    supplied = request.headers.get("X-Admin-Key", "")
    expected = current_app.config.get("TEAM_ADMIN_KEY") or ""

    # An empty key never matches, even an empty header.
    return bool(expected and supplied) and hmac.compare_digest(
        supplied.encode(), expected.encode()
    )


def _single_user_guard():
    """
    before_request hook. While the API listens beyond this machine,
    only open paths answer to other computers; the rest needs a
    request from this machine or the team admin key.
    """

    if not current_app.config.get("LOCAL_DATA_ONLY"):
        return None

    path = request.path

    if path in OPEN_PATHS or path.startswith(OPEN_PATH_PREFIXES):
        return None

    if is_local_request() or _has_admin_key():
        return None

    return jsonify({
        "error": "Only available on the main user's computer (or with the "
                 "admin key in the X-Admin-Key header).",
    }), 403


# ---------------------------------------------------------
# Application factory
# ---------------------------------------------------------

def create_app(db_file=None, admin_key=None):
    """
    Build the API app. Team storage (db_file, admin key) is
    prepared here rather than at import time, so tests and tools
    can import this module without touching data/team.
    """

    app = Flask(__name__)

    # Listening beyond this machine (WORKSPACE_API_HOST=0.0.0.0): the
    # single-user endpoints answer only to this machine or the admin key.
    # A public tunnel makes the server reachable from anywhere even on
    # 127.0.0.1, so the same rule applies then.
    app.config["LOCAL_DATA_ONLY"] = not is_loopback_host(API_HOST) or bool(public_url())
    app.before_request(_single_user_guard)

    app.register_blueprint(core_api)

    init_team(app, admin_key, db_file)

    try:
        from api.focus import focus_api

    except Exception as error:
        # The Focus page is optional; a broken or missing focus module
        # must never stop the dashboard and team API from starting.
        print(f"Focus API not available: {error}", file=sys.stderr)

    else:
        app.register_blueprint(focus_api)

    return app


# ---------------------------------------------------------
# Run server
# ---------------------------------------------------------

def debug_requested(loopback):
    """
    Flask's debug mode: off unless WORKSPACE_API_DEBUG=1, and never
    while the API listens beyond this machine.
    """

    return loopback and os.environ.get(DEBUG_ENV, "").strip() == "1"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve the Workspace Analytics API: the processed "
                    "activity, pipeline status, Focus Assistant and team "
                    "endpoints. Normally started by start_project.py.",
        epilog="Settings come from environment variables: WORKSPACE_API_HOST "
               "(default 127.0.0.1), WORKSPACE_API_PORT (default 5050), "
               "WORKSPACE_DATA_DIR, WORKSPACE_TEAM_DB, WORKSPACE_TEAM_ADMIN_KEY "
               "and WORKSPACE_API_DEBUG=1 (Flask debug mode, only on 127.0.0.1). "
               "To accept team members, use `python start_project.py --team`.",
    )

    return parser.parse_args(argv)


def main():

    app = create_app()

    loopback = is_loopback_host(API_HOST)
    debug = debug_requested(loopback)

    base_url = f"http://{API_HOST}:{API_PORT}"

    print("=" * 60)
    print("Workspace Analytics API")
    print("=" * 60)

    print("Processed file:")
    print(PROCESSED_DATA_FILE)

    print()
    print(f"API:        {base_url}")
    print(f"Health:     {base_url}/api/health")
    print(f"Status:     {base_url}/api/status")
    print(f"Analytics:  {base_url}/api/analytics.csv")
    print(f"Team:       {base_url}/api/team/...")

    print()
    print("Team admin key (paste into the Team page):")
    print(app.config["TEAM_ADMIN_KEY"])

    if not loopback:
        print()
        print("Listening beyond this machine: debug mode is OFF.")
        print("Your activity, status and Focus data answer only to this")
        print("machine or to the admin key. Members' data travels over")
        print("plain HTTP. Use a trusted network, or put the server")
        print("behind HTTPS.")

        if os.environ.get(DEBUG_ENV, "").strip() == "1":
            print(f"{DEBUG_ENV}=1 is ignored here.")

    elif debug:
        print()
        print(f"Flask debug mode is ON ({DEBUG_ENV}=1). Never put this")
        print("server behind a proxy that other computers can reach.")

    print("=" * 60, flush=True)

    if not loopback:

        try:
            from waitress import serve

        except ImportError:
            print("waitress is not installed; using Flask's development server.")

        else:
            print("Serving with waitress.", flush=True)
            serve(app, host=API_HOST, port=API_PORT)
            return

    app.run(
        host=API_HOST,
        port=API_PORT,
        debug=debug
    )


if __name__ == "__main__":
    # --help answers without starting the server.
    parse_args()
    main()
