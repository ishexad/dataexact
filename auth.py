"""
Lightweight "sign in to be tracked" auth module.

Design goals, matching what was actually asked for:
- Login exists purely to identify/track who's using the tool.
- The tool itself stays fully usable WITHOUT logging in — nothing is gated.
- Supports Google and Facebook via standard OAuth2 (Authlib).
- Apple Sign In is intentionally NOT included here — it needs an Apple
  Developer Program account and a different credential setup (a signed JWT
  client secret rather than a plain secret). See apple_signin_notes.md.

Storage: a small SQLite file (users.db). Good enough for tracking who's
logged in; not a general-purpose user system.

Required environment variables (set these in Render's dashboard, never
commit them to the repo):
  SESSION_SECRET_KEY      any long random string, used to sign session cookies
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  FACEBOOK_CLIENT_ID
  FACEBOOK_CLIENT_SECRET
  BASE_URL                e.g. https://dataexact.co.uk (used to build the
                           OAuth redirect/callback URLs correctly)
"""

import os
import sqlite3
import time
from contextlib import contextmanager

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse

DB_PATH = "users.db"
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

router = APIRouter()
oauth = OAuth()

# Only register a provider if its credentials are actually present, so the
# app doesn't crash locally / before you've set these up in Render.
if os.environ.get("GOOGLE_CLIENT_ID"):
    oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

if os.environ.get("FACEBOOK_CLIENT_ID"):
    oauth.register(
        name="facebook",
        client_id=os.environ["FACEBOOK_CLIENT_ID"],
        client_secret=os.environ["FACEBOOK_CLIENT_SECRET"],
        access_token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
        api_base_url="https://graph.facebook.com/v19.0/",
        client_kwargs={"scope": "email public_profile"},
    )


def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                email TEXT,
                name TEXT,
                first_login REAL NOT NULL,
                last_login REAL NOT NULL,
                login_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider, provider_user_id)
            )
        """)


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _upsert_user(provider: str, provider_user_id: str, email: str, name: str):
    now = time.time()
    with _db() as conn:
        conn.execute("""
            INSERT INTO users (provider, provider_user_id, email, name, first_login, last_login, login_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(provider, provider_user_id)
            DO UPDATE SET last_login = excluded.last_login,
                          login_count = login_count + 1,
                          email = excluded.email,
                          name = excluded.name
        """, (provider, provider_user_id, email, name, now, now))


_init_db()


@router.get("/auth/{provider}/login")
async def login(provider: str, request: Request):
    if provider not in oauth._clients:
        return JSONResponse({"error": f"{provider} login isn't configured yet"}, status_code=404)
    redirect_uri = f"{BASE_URL}/auth/{provider}/callback"
    client = oauth.create_client(provider)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/{provider}/callback")
async def callback(provider: str, request: Request):
    if provider not in oauth._clients:
        return JSONResponse({"error": f"{provider} login isn't configured yet"}, status_code=404)
    client = oauth.create_client(provider)
    token = await client.authorize_access_token(request)

    if provider == "google":
        userinfo = token.get("userinfo") or await client.userinfo(token=token)
        provider_user_id = userinfo["sub"]
        email = userinfo.get("email")
        name = userinfo.get("name")
    elif provider == "facebook":
        resp = await client.get("me?fields=id,name,email", token=token)
        userinfo = resp.json()
        provider_user_id = userinfo["id"]
        email = userinfo.get("email")
        name = userinfo.get("name")
    else:
        return JSONResponse({"error": "unsupported provider"}, status_code=400)

    _upsert_user(provider, provider_user_id, email, name)

    request.session["user"] = {"provider": provider, "email": email, "name": name}
    return RedirectResponse(url="/")


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/")


@router.get("/me")
async def me(request: Request):
    return {"user": request.session.get("user")}
