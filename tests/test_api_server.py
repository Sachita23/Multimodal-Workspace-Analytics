import json
import os
import sys
import time
import types

import pytest
from flask import Blueprint

import run_pipeline
from api import server


ADMIN_KEY = "x" * 24

STATUS_KEYS = {
    "last_success", "last_error", "rows", "sessions",
    "last_observation_ts", "duration_s",
}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "workspace_activity.csv"
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    status = tmp_path / "processed" / "status.json"

    monkeypatch.setattr(server, "RAW_DATA_FILE", raw)
    monkeypatch.setattr(server, "PROCESSED_DATA_FILE", processed)
    monkeypatch.setattr(server, "PIPELINE_STATUS_FILE", status)

    return raw, processed, status


@pytest.fixture
def client(tmp_path, paths):
    app = server.create_app(db_file=tmp_path / "t.db", admin_key=ADMIN_KEY)
    app.config["TESTING"] = True
    return app.test_client()


def _write(path, text):
    run_pipeline.write_atomically(path, lambda temp: temp.write_text(text))


def test_importing_the_module_does_not_build_an_app():
    # Team storage is only touched when create_app() runs.
    assert not hasattr(server, "app")


def test_health(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_team_api_is_registered_with_the_given_key(client):
    assert client.get("/api/team/overview").status_code == 401

    response = client.get("/api/team/overview", headers={"X-Admin-Key": ADMIN_KEY})
    assert response.status_code == 200


def test_analytics_csv_is_404_before_the_pipeline_ran(client):
    response = client.get("/api/analytics.csv")

    assert response.status_code == 404
    assert "not available yet" in response.get_json()["error"]


def _open_handles(path):
    """
    How many of this process's open files are `path` (None when psutil
    cannot tell on this platform).
    """
    try:
        import psutil
        open_files = psutil.Process().open_files()
    except Exception:
        return None

    return sum(os.path.samefile(item.path, path) for item in open_files
               if os.path.exists(item.path))


def test_analytics_csv_uses_etags(client, paths):
    _, processed, _ = paths
    _write(processed, "timestamp,application\n2026-10-01 09:00:00,Slack\n")

    # The test client keeps send_file's handle open until the response
    # is closed. Real servers close it after sending; here it must be
    # closed by hand, or os.replace below fails on Windows.
    with client.get("/api/analytics.csv") as first:

        assert first.status_code == 200
        assert first.mimetype == "text/csv"
        assert first.data.startswith(b"timestamp,application")
        assert first.headers["Cache-Control"] == "no-cache"

        etag = first.headers["ETag"]
        assert etag

    with client.get("/api/analytics.csv", headers={"If-None-Match": etag}) as unchanged:
        assert unchanged.status_code == 304
        assert unchanged.data == b""

    assert _open_handles(processed) in (0, None)

    # A rewrite by the pipeline produces a new ETag.
    time.sleep(0.01)
    _write(processed, "timestamp,application\n2026-10-01 09:00:05,Zoom\n")

    with client.get("/api/analytics.csv", headers={"If-None-Match": etag}) as changed:
        assert changed.status_code == 200
        assert changed.headers["ETag"] != etag
        assert b"Zoom" in changed.data


def test_status_before_anything_ran(client):
    response = client.get("/api/status")
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["pipeline"] is None
    assert body["raw_mtime"] is None
    assert body["processed_mtime"] is None
    assert body["collector_stale"] is True
    assert body["refresh_seconds"] == 10
    assert body["server_time"]


def test_status_reports_pipeline_and_collector(client, paths):
    raw, processed, status = paths
    raw.parent.mkdir(parents=True)
    raw.write_text("timestamp\n")
    _write(processed, "timestamp\n")
    _write(status, json.dumps({
        "last_success": "2026-10-01T09:00:00+00:00",
        "last_error": None,
        "error_trace": "Traceback ...",
        "rows": 12,
        "sessions": 2,
        "last_observation_ts": "2026-10-01 09:00:00",
        "duration_s": 0.1,
    }))

    body = client.get("/api/status").get_json()

    assert set(body["pipeline"]) == STATUS_KEYS
    assert body["pipeline"]["rows"] == 12
    assert body["pipeline"]["sessions"] == 2
    assert body["raw_mtime"] and body["processed_mtime"]
    assert body["collector_stale"] is False

    # The collector stopped two minutes ago.
    old = time.time() - 120
    os.utime(raw, (old, old))

    assert client.get("/api/status").get_json()["collector_stale"] is True


def test_unreadable_status_file_is_reported_as_no_pipeline(client, paths):
    _, _, status = paths
    _write(status, "{not json")

    assert client.get("/api/status").get_json()["pipeline"] is None


def test_focus_blueprint_is_optional(tmp_path, paths, monkeypatch):
    monkeypatch.setitem(sys.modules, "api.focus", None)

    app = server.create_app(db_file=tmp_path / "a.db", admin_key=ADMIN_KEY)

    assert app.test_client().get("/api/health").status_code == 200


def test_focus_blueprint_is_registered_when_present(tmp_path, paths, monkeypatch):
    focus_api = Blueprint("focus_api", __name__, url_prefix="/api/focus")
    focus_api.add_url_rule("/live", "live", lambda: {"running": False})

    module = types.ModuleType("api.focus")
    module.focus_api = focus_api
    monkeypatch.setitem(sys.modules, "api.focus", module)

    app = server.create_app(db_file=tmp_path / "b.db", admin_key=ADMIN_KEY)

    assert app.test_client().get("/api/focus/live").get_json() == {"running": False}
