"""Invite-only GitHub sign-in for the hosted, single-owner workspace."""

import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete

from akaryon.database.models import HostedLoginRecord, HostedSessionRecord
from akaryon.database.session import session_scope

SESSION_COOKIE = "__Host-akaryon-session"
STATE_COOKIE = "__Host-akaryon-login"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_SECONDS = 600
WEB_DIR = Path(__file__).with_name("web")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secure_cookie(response, name, value, max_age):
    response.set_cookie(name, value, max_age=max_age, secure=True,
                        httponly=True, samesite="lax", path="/")


class HostedAccess:
    def __init__(self, settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        parsed = urlsplit(settings.public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.port not in (None, 443) or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Hosted mode requires AKARYON_PUBLIC_URL to be an HTTPS origin")
        if not session_factory:
            raise ValueError("Hosted mode requires persistent AKARYON_DATABASE_URL")
        if not settings.invited_email or not settings.invited_github_id.isdigit():
            raise ValueError("Set the invited email and immutable GitHub account ID")
        if not settings.github_client_id or not settings.github_client_secret:
            raise ValueError("Set AKARYON_GITHUB_CLIENT_ID and AKARYON_GITHUB_CLIENT_SECRET")
        self.origin = settings.public_url.rstrip("/")
        self.hostname = parsed.hostname.casefold()
        self.callback = self.origin + "/auth/callback"
        self.allowed_email = settings.invited_email.strip().casefold()
        self.allowed_id = settings.invited_github_id
        self.login_attempts = {}

    def identity(self, token):
        if not token or len(token) > 128:
            return None
        with session_scope(self.session_factory) as db:
            record = db.get(HostedSessionRecord, digest(token))
            if (record and record.expires_at > int(time.time()) and
                    record.email == self.allowed_email and record.github_id == self.allowed_id):
                return {"email": record.email, "github_id": record.github_id}
        return None

    def guard(self, request: Request):
        # Render health probes contain no private application status or data.
        if request.url.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return None
        if (request.url.hostname or "").casefold() != self.hostname or request.url.port not in (None, 443):
            return JSONResponse({"detail": "Unrecognized host"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (request.headers.get("origin") != self.origin or
                    request.headers.get("sec-fetch-site", "same-origin") != "same-origin"):
                return JSONResponse({"detail": "Cross-origin state-changing requests are not allowed"}, status_code=403)
        if request.url.path in {"/signin", "/auth/login", "/auth/callback", "/ui-assets/signin.css"}:
            return None
        identity = self.identity(request.cookies.get(SESSION_COOKIE))
        if not identity:
            if request.url.path == "/" and request.method in {"GET", "HEAD"}:
                return RedirectResponse("/signin", status_code=303)
            return JSONResponse({"detail": "Sign in to Akaryon", "signin_url": "/signin"}, status_code=401)
        request.state.identity = identity
        return None

    def install(self, app):
        @app.get("/healthz", include_in_schema=False)
        def healthz():
            return {"status": "ok"}

        @app.get("/signin", include_in_schema=False)
        def signin():
            return FileResponse(WEB_DIR / "signin.html", headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            })

        @app.get("/auth/login", include_in_schema=False)
        def login(request: Request):
            now = int(time.time())
            # Bound login challenges per peer and globally on this single instance.
            self.login_attempts = {key: values for key, values in self.login_attempts.items()
                                   if values[0] > now - 60}
            peer = request.client.host if request.client else "unknown"
            first, count = self.login_attempts.get(peer, (now, 0))
            if count >= 10 or len(self.login_attempts) >= 1000:
                return JSONResponse({"detail": "Please wait a minute before signing in again"}, status_code=429)
            self.login_attempts[peer] = (first, count + 1)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            with session_scope(self.session_factory) as db:
                db.execute(delete(HostedLoginRecord).where(HostedLoginRecord.expires_at <= now))
                db.execute(delete(HostedSessionRecord).where(HostedSessionRecord.expires_at <= now))
                previous = request.cookies.get(STATE_COOKIE)
                if previous:
                    db.execute(delete(HostedLoginRecord).where(HostedLoginRecord.state_hash == digest(previous)))
                db.add(HostedLoginRecord(state_hash=digest(state), verifier=verifier, expires_at=now + LOGIN_SECONDS))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode({
                "client_id": self.settings.github_client_id, "redirect_uri": self.callback,
                "scope": "user:email", "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "allow_signup": "false",
            }), status_code=303)
            secure_cookie(response, STATE_COOKIE, state, LOGIN_SECONDS)
            return response

        @app.get("/auth/callback", include_in_schema=False)
        async def callback(request: Request):
            state = request.query_params.get("state", "")
            cookie = request.cookies.get(STATE_COOKIE, "")
            code = request.query_params.get("code", "")
            if not state or len(state) > 128 or not cookie or not secrets.compare_digest(state, cookie):
                return self.failure("Sign-in expired. Please start again.", 400)
            with session_scope(self.session_factory) as db:
                # Atomic consumption prevents callback replays across requests/workers.
                verifier = db.execute(delete(HostedLoginRecord).where(
                    HostedLoginRecord.state_hash == digest(state),
                    HostedLoginRecord.expires_at > int(time.time()),
                ).returning(HostedLoginRecord.verifier)).scalar_one_or_none()
            if not verifier or not code or len(code) > 512:
                return self.failure("Sign-in expired. Please start again.", 400)
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                    exchanged = await client.post("https://github.com/login/oauth/access_token", data={
                        "client_id": self.settings.github_client_id,
                        "client_secret": self.settings.github_client_secret,
                        "code": code, "redirect_uri": self.callback, "code_verifier": verifier,
                    }, headers={"Accept": "application/json"})
                    exchanged.raise_for_status()
                    access_token = exchanged.json().get("access_token")
                    if not isinstance(access_token, str) or not access_token:
                        return self.failure("GitHub sign-in could not be completed. Please try again.", 401)
                    headers = {"Authorization": "Bearer " + access_token,
                               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
                    user_response = await client.get("https://api.github.com/user", headers=headers)
                    emails_response = await client.get("https://api.github.com/user/emails", headers=headers,
                                                       params={"per_page": 100})
                    user_response.raise_for_status()
                    emails_response.raise_for_status()
                    user_id = str(user_response.json().get("id", ""))
                    email_matches = any(item.get("verified") is True and
                        str(item.get("email", "")).casefold() == self.allowed_email
                        for item in emails_response.json())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # Never return or log tokens, authorization codes, or upstream payloads.
                return self.failure("GitHub is unavailable. Please try signing in again.", 502)
            if user_id != self.allowed_id or not email_matches:
                return self.failure("This GitHub account has not been invited to Akaryon.", 403)
            token = secrets.token_urlsafe(32)
            with session_scope(self.session_factory) as db:
                previous = request.cookies.get(SESSION_COOKIE)
                if previous:
                    db.execute(delete(HostedSessionRecord).where(HostedSessionRecord.token_hash == digest(previous)))
                db.add(HostedSessionRecord(token_hash=digest(token), github_id=user_id,
                                          email=self.allowed_email, expires_at=int(time.time()) + SESSION_SECONDS))
            response = RedirectResponse("/#home", status_code=303)
            response.delete_cookie(STATE_COOKIE, secure=True, httponly=True, samesite="lax")
            secure_cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
            return response

        @app.get("/auth/session", include_in_schema=False)
        def session(request: Request):
            return {"hosted": True, "email": request.state.identity["email"]}

        @app.post("/auth/logout", include_in_schema=False)
        def logout(request: Request):
            token = request.cookies.get(SESSION_COOKIE, "")
            with session_scope(self.session_factory) as db:
                db.execute(delete(HostedSessionRecord).where(HostedSessionRecord.token_hash == digest(token)))
            response = RedirectResponse("/signin", status_code=303)
            response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
            return response

    @staticmethod
    def failure(message, status):
        response = JSONResponse({"detail": message, "signin_url": "/signin"}, status_code=status)
        response.delete_cookie(STATE_COOKIE, secure=True, httponly=True, samesite="lax")
        return response


def hosted_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
