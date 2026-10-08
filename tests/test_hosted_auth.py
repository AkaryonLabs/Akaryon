import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from akaryon.core.config import Settings
from akaryon.database.models import HostedLoginRecord, HostedSessionRecord
from akaryon.database.session import create_session_factory, session_scope
from akaryon.hosted_auth import SESSION_COOKIE, STATE_COOKIE, digest
from akaryon.main import create_app


@pytest.fixture
def hosted(tmp_path):
    settings = Settings(_env_file=None, access_mode="invited", public_url="https://akaryon.live",
                        invited_email="owner@example.com", invited_github_id="123",
                        github_client_id="test-client", github_client_secret="test-secret",
                        database_url="sqlite:///" + (tmp_path / "hosted.db").as_posix(),
                        database_create_tables=True)
    app = create_app(settings)
    _, factory = create_session_factory(settings.database_url)
    client = TestClient(app, base_url=settings.public_url, follow_redirects=False)
    return client, factory, settings


def allow_session(client, factory, expires=None, github_id="123"):
    token = "test-opaque-session-token"
    with session_scope(factory) as db:
        db.add(HostedSessionRecord(token_hash=digest(token), github_id=github_id,
                                  email="owner@example.com", expires_at=expires or int(time.time()) + 60))
    client.cookies.set(SESSION_COOKIE, token)
    return token


@pytest.mark.parametrize("path", ["/chat", "/chat/stream", "/memory", "/projects", "/tasks", "/health",
                                  "/agents", "/approvals/ui", "/openapi.json", "/docs",
                                  "/ui-assets/index.html", "/ui-assets/void-speaker.glb", "/auth/session"])
def test_all_private_surfaces_require_session(hosted, path):
    client, _, _ = hosted
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Forwarded-User": "owner@example.com"}).status_code == 401


def test_public_signin_health_and_host_boundary(hosted):
    client, _, _ = hosted
    assert client.get("/").headers["location"] == "/signin"
    assert client.get("/signin").status_code == 200
    assert client.get("/ui-assets/signin.css").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz", headers={"Host": "render-probe"}).status_code == 200
    for host in ("attacker.example", "akaryon.live.evil", "akaryon.live:1234"):
        assert client.get("/signin", headers={"Host": host}).status_code == 400


def test_session_opens_app_but_mutations_require_origin_and_logout_revokes(hosted):
    client, factory, _ = hosted
    token = allow_session(client, factory)
    assert client.get("/").status_code == 200
    assert client.get("/ui-assets/void-speaker.glb").content[:4] == b"glTF"
    response = client.get("/auth/session")
    assert response.json() == {"hosted": True, "email": "owner@example.com"}
    assert response.headers["Cache-Control"] == "private, no-store"
    for headers in ({}, {"Origin": "https://evil.example"},
                    {"Origin": "https://akaryon.live", "Sec-Fetch-Site": "cross-site"}):
        assert client.post("/chat", json={"message": "test"}, headers=headers).status_code == 403
    assert client.post("/chat", json={"message": "test"}, headers={"Origin": "https://akaryon.live"}).status_code == 200
    assert client.post("/auth/logout", headers={"Origin": "https://akaryon.live"}).status_code == 303
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/auth/session").status_code == 401


@pytest.mark.parametrize("expires,github_id", [(1, "123"), (None, "456")])
def test_expired_or_wrong_account_sessions_fail(hosted, expires, github_id):
    client, factory, _ = hosted
    allow_session(client, factory, expires, github_id)
    assert client.get("/auth/session").status_code == 401


def fake_github(monkeypatch, email="owner@example.com", verified=True, user_id=123):
    real_client = httpx.AsyncClient
    def handler(request):
        if request.url.path == "/login/oauth/access_token":
            assert b"code_verifier=" in request.content
            return httpx.Response(200, json={"access_token": "private-oauth-token"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": user_id})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=[{"email": email, "verified": verified}])
        raise AssertionError(str(request.url))
    monkeypatch.setattr("akaryon.hosted_auth.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def test_oauth_state_pkce_cookie_security_and_success(hosted, monkeypatch):
    client, factory, _ = hosted
    fake_github(monkeypatch)
    login = client.get("/auth/login")
    query = parse_qs(urlsplit(login.headers["location"]).query)
    state = query["state"][0]
    assert query["scope"] == ["user:email"]
    assert query["redirect_uri"] == ["https://akaryon.live/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert client.get("/auth/callback?state=forged&code=test").status_code == 400
    # Restore the original browser-bound challenge after testing the failure response.
    client.cookies.set(STATE_COOKIE, state)
    success = client.get("/auth/callback", params={"state": state, "code": "test"})
    assert success.status_code == 303 and success.headers["location"] == "/#home"
    assert "HttpOnly" in success.headers["set-cookie"] and "private-oauth-token" not in str(success.headers)
    assert client.get("/auth/session").status_code == 200
    client.cookies.set(STATE_COOKIE, state)
    assert client.get("/auth/callback", params={"state": state, "code": "test"}).status_code == 400
    with session_scope(factory) as db:
        record = db.scalar(select(HostedSessionRecord))
        assert len(record.token_hash) == hashlib.sha256().digest_size * 2
        assert db.scalar(select(HostedLoginRecord)) is None


@pytest.mark.parametrize("email,verified,user_id", [
    ("stranger@example.com", True, 123), ("owner@example.com", False, 123),
    ("owner@example.com", True, 456)])
def test_uninvited_and_unverified_accounts_denied(hosted, monkeypatch, email, verified, user_id):
    client, factory, _ = hosted
    fake_github(monkeypatch, email, verified, user_id)
    login = client.get("/auth/login")
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    assert client.get("/auth/callback", params={"state": state, "code": "test"}).status_code == 403
    with session_scope(factory) as db:
        assert db.scalar(select(HostedSessionRecord)) is None


def test_hosted_mode_fails_closed_without_auth_configuration():
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(Settings(_env_file=None, access_mode="invited"))
