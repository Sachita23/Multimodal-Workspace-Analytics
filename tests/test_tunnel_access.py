"""
--tunnel: a Cloudflare tunnel connects to the API from this machine, so
tunnelled visitors must still be treated as other computers.
"""

import json

import pytest

import start_project
from api import server


TUNNEL_HEADERS = {"Cf-Connecting-Ip": "203.0.113.7", "X-Forwarded-For": "203.0.113.7"}


@pytest.fixture
def tunnel_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_PUBLIC_URL", "https://example-words.trycloudflare.com")
    app = server.create_app(db_file=tmp_path / "team.db", admin_key="k" * 24)
    app.config["TESTING"] = True
    return app


def test_public_url_turns_on_the_local_data_guard(tunnel_app):
    # The API listens on 127.0.0.1 with a tunnel, yet it is public.
    assert tunnel_app.config["LOCAL_DATA_ONLY"] is True


def test_tunnelled_visitors_cannot_read_your_activity(tunnel_app):
    client = tunnel_app.test_client()

    for path in ("/api/analytics.csv", "/api/status", "/api/focus/live"):
        response = client.get(path, headers=TUNNEL_HEADERS)
        assert response.status_code == 403, path

    # Member endpoints still pass the guard (the team API checks tokens).
    assert client.get("/api/health", headers=TUNNEL_HEADERS).status_code == 200
    assert client.get("/api/team/me", headers=TUNNEL_HEADERS).status_code == 401


def test_the_dashboard_on_this_machine_keeps_access(tunnel_app):
    # The Vite proxy adds no forwarding headers.
    response = tunnel_app.test_client().get("/api/status")
    assert response.status_code == 200


def test_invites_carry_the_tunnel_address(tunnel_app):
    response = tunnel_app.test_client().post(
        "/api/team/invites", json={"name": "Charan"}, headers={"X-Admin-Key": "k" * 24}
    )

    body = response.get_json()
    assert body["server_url"] == "https://example-words.trycloudflare.com"
    assert "--server https://example-words.trycloudflare.com" in body["join_command"]


def _call(middleware, path, environ_extra):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    environ = {"PATH_INFO": path, "REMOTE_ADDR": "127.0.0.1", **environ_extra}
    body = b"".join(middleware(environ, start_response))
    return captured["status"], body


def test_member_only_middleware_rejects_tunnel_requests_to_private_paths():
    inner_calls = []

    def inner(environ, start_response):
        inner_calls.append(environ["PATH_INFO"])
        start_response("200 OK", [])
        return [b"ok"]

    middleware = start_project.MemberEndpointsOnly(inner)

    status, body = _call(middleware, "/api/team/overview", {"HTTP_CF_CONNECTING_IP": "203.0.113.7"})
    assert status.startswith("403")
    assert "Not available" in json.loads(body)["error"]

    status, _ = _call(middleware, "/api/team/ingest", {"HTTP_CF_CONNECTING_IP": "203.0.113.7"})
    assert status.startswith("200")

    # The dashboard on this machine (no Cloudflare header) passes.
    status, _ = _call(middleware, "/api/team/overview", {})
    assert status.startswith("200")
    assert inner_calls == ["/api/team/ingest", "/api/team/overview"]


def test_tunnel_flag_keeps_the_api_on_loopback_in_proxy_mode():
    args = start_project.build_parser().parse_args(["--tunnel"])

    assert start_project.api_command(args)[-1].endswith("serve_api('proxy')")
