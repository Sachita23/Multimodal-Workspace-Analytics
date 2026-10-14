import sys
from pathlib import Path

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS  # noqa: E402
from team import db  # noqa: E402
from team.api import init_team  # noqa: E402


ADMIN_KEY = "test-admin-key"


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "team.db"
    db.init_db(path)
    return path


@pytest.fixture
def app(db_file):
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_team(app, admin_key=ADMIN_KEY, db_file=db_file)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin():
    return {"X-Admin-Key": ADMIN_KEY}


def consent(**overrides):
    return {
        "accepted": True,
        "version": TEAM_CONSENT_VERSION,
        "fields": list(TEAM_SHARED_FIELDS),
        **overrides,
    }


@pytest.fixture
def invite(client, admin):
    def make(name="Member"):
        response = client.post("/api/team/invites", json={"name": name}, headers=admin)
        assert response.status_code == 201
        return response.get_json()["invite_code"]

    return make


@pytest.fixture
def member(client, invite):
    """
    Create a joined member; returns (member_id, auth headers).
    """

    def make(name="Member"):
        response = client.post(
            "/api/team/join",
            json={"invite_code": invite(name), "consent": consent()},
        )
        assert response.status_code == 201
        body = response.get_json()
        return body["member_id"], {"Authorization": f"Bearer {body['token']}"}

    return make


def observation(timestamp="2026-10-01 09:00:00", **overrides):
    return {
        "timestamp": timestamp,
        "application": "Visual Studio Code",
        "window_title": "main.py - VS Code",
        "activity_status": "Active",
        "idle_seconds": 1.0,
        "cpu_percent": 20.0,
        "memory_percent": 50.0,
        "battery_percent": 90.0,
        "charging": True,
        **overrides,
    }
