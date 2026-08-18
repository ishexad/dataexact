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
  BASE_URL                e.g. https://www.dataexact.io (used to build the
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
        # Billing columns, added after the fact — SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so just swallow the "already exists" error.
        for column_def in (
            "stripe_customer_id TEXT",
            "plan TEXT",
            "subscription_status TEXT",
            "current_period_end REAL",
        ):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column_def}")
            except sqlite3.OperationalError:
                pass


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _upsert_user(provider: str, provider_user_id: str, email: str, name: str) -> int:
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
        row = conn.execute(
            "SELECT id FROM users WHERE provider=? AND provider_user_id=?",
            (provider, provider_user_id),
        ).fetchone()
        return row[0]


_init_db()


def upsert_email_user(email: str, name: str) -> int:
    """Create/refresh an account that signs in by emailed link rather than
    OAuth (see email_auth.py). The address is the provider id, so the same
    address always lands on the same row."""
    return _upsert_user("email", email.lower(), email, name)


def sign_in(request: Request, user_id: int, provider: str, email: str, name: str) -> None:
    """The one place the session's user shape is written, so the OAuth
    callback and the magic-link verify can't drift apart."""
    request.session["user"] = {"id": user_id, "provider": provider, "email": email, "name": name}


def get_user(user_id: int) -> dict | None:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None


def set_stripe_customer_id(user_id: int, customer_id: str):
    with _db() as conn:
        conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user_id))


def update_subscription(customer_id: str, plan: str | None, status: str | None, period_end: float | None):
    with _db() as conn:
        conn.execute("""
            UPDATE users SET plan=?, subscription_status=?, current_period_end=?
            WHERE stripe_customer_id=?
        """, (plan, status, period_end, customer_id))


@router.get("/auth/providers")
async def providers():
    return {"providers": list(oauth._clients.keys())}


@router.get("/auth/{provider}/login")
async def login(provider: str, request: Request, next: str = "/"):
    if provider not in oauth._clients:
        return JSONResponse({"error": f"{provider} login isn't configured yet"}, status_code=404)
    # Only allow same-site relative paths, so this can't be used as an open redirect.
    request.session["oauth_next"] = next if next.startswith("/") else "/"
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

    user_id = _upsert_user(provider, provider_user_id, email, name)

    sign_in(request, user_id, provider, email, name)
    next_url = request.session.pop("oauth_next", "/")
    return RedirectResponse(url=next_url)


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/")


@router.get("/me")
async def me(request: Request):
    return {"user": request.session.get("user")}
