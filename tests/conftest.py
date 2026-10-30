import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# Point every data location at a throwaway folder BEFORE any project
# module reads config.py, so no test (or test bug) can ever touch the
# owner's recorded activity, team database, admin key or member state.
# This overrides whatever WORKSPACE_* values the shell running pytest has.
_SANDBOX = Path(tempfile.mkdtemp(prefix="workspace-analytics-tests-"))

for _name, _path in {
    "WORKSPACE_DATA_DIR": _SANDBOX / "data",
    "WORKSPACE_TEAM_DIR": _SANDBOX / "team",
    "WORKSPACE_TEAM_DB": _SANDBOX / "team" / "team.db",
    "WORKSPACE_TEAM_HOME": _SANDBOX / "member-home",
    "WORKSPACE_TEAM_DEMO_STATE": _SANDBOX / "team" / "live_demo.json",
}.items():
    os.environ[_name] = str(_path)

os.environ.pop("WORKSPACE_TEAM_ADMIN_KEY", None)

# Never open a real deviation popup window on the developer's screen.
os.environ["WORKSPACE_FOCUS_POPUP"] = "0"

from config import TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS  # noqa: E402
from team import db  # noqa: E402
from team.api import init_team  # noqa: E402


ADMIN_KEY = "test-admin-key-0123456789"

# The server rejects observations older than 90 days, so test rows
# are stamped relative to today: yesterday at 09:00 local time.
BASE_TIME = (datetime.now() - timedelta(days=1)).replace(
    hour=9, minute=0, second=0, microsecond=0
)


def recent_timestamp(offset_seconds=0):
    """
    A timestamp the server accepts: BASE_TIME plus an offset.
    """

    return (BASE_TIME + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


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


def observation(timestamp=None, **overrides):
    return {
        "timestamp": timestamp or recent_timestamp(),
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
