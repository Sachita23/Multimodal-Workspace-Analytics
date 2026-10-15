"""
Read-only Focus Assistant endpoints for the dashboard.

    GET /api/focus/live      the running session, from the assistant's heartbeat
    GET /api/focus/summary   totals across every recorded session
    GET /api/focus/sessions  every session, newest first

Everything is read from config.FOCUS_DATA_DIR (app.config["FOCUS_DATA_DIR"]
overrides it, e.g. in tests). This module must not import tkinter.
"""

import os
import sys
from pathlib import Path

from flask import Blueprint, current_app, jsonify


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
)


def _data_dir():
    return Path(current_app.config.get("FOCUS_DATA_DIR") or config.FOCUS_DATA_DIR)


def _fresh(payload):
    # The files change every second while a session runs.
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------

@focus_api.get("/live")
def focus_live():
    """
    stale=true (and running=false) when live.json is missing or its
    updated_at is older than 5 s.
    """

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
