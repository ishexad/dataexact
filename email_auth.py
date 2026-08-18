"""
Passwordless email sign-in ("magic link"), for people who have neither a
Google nor a Facebook account.

Why no passwords: this app stores nothing a password would protect beyond a
subscription, and a password would bring hashing, a reset flow, a
verification flow, login rate limiting, and a hash table worth stealing. A
one-time link proves control of the address, which is exactly the same thing
a password reset proves — so this skips straight to it.

Flow:
1. POST /auth/email/request  -> visitor gives an email, gets a link mailed
2. GET  /auth/email/verify   -> the link signs them in and redirects onward

The account is created on first successful verify — there's no separate
"sign up" step, since the first link and every later one do the same work.
Accounts land in the same `users` table as OAuth ones with provider='email',
so gating, billing, and the admin stats treat them identically.

Token design: a 256-bit random token is mailed, and only its SHA-256 hash is
stored, so a copy of users.db doesn't let anyone sign in as somebody else.
Tokens are single-use (consumed by an atomic UPDATE) and expire in 15
minutes. The post-login redirect is stored server-side on the token row
rather than carried in the URL, so a tampered link can't redirect anywhere.

Required environment variables to actually send mail:
  RESEND_API_KEY   from resend.com — the sending account
  MAIL_FROM        e.g. "DataExact <hello@dataexact.io>", on a domain
                   verified with Resend

With RESEND_API_KEY unset, sending is skipped and the link is written to the
server log instead — local development only, and never when BASE_URL points
at a real host.
"""

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import auth
import usage

DB_PATH = "users.db"
BASE_URL = auth.BASE_URL

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/auth/email")

TOKEN_TTL_SECONDS = 15 * 60
PRUNE_AFTER_SECONDS = 24 * 60 * 60

# Email bombing protection: someone else's address must not be usable as a
# target, and one visitor shouldn't be able to fan out requests either.
MAX_PER_EMAIL_PER_HOUR = 5
MAX_PER_IP_PER_HOUR = 10

# Deliberately loose — the only real proof an address works is that the link
# arrives, so this just catches typos and obvious junk.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS magic_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                next_url TEXT,
                ip_hash TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email, created_at)")


_init_db()


def is_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def _is_local() -> bool:
    return (urlparse(BASE_URL).hostname or "") in ("localhost", "127.0.0.1")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _prune(conn):
    conn.execute("DELETE FROM magic_links WHERE created_at < ?", (time.time() - PRUNE_AFTER_SECONDS,))


def _rate_limited(conn, email: str, ip_hash: str) -> bool:
    since = time.time() - 3600
    by_email = conn.execute(
        "SELECT COUNT(*) FROM magic_links WHERE email=? AND created_at > ?", (email, since)
    ).fetchone()[0]
    if by_email >= MAX_PER_EMAIL_PER_HOUR:
        return True
    by_ip = conn.execute(
        "SELECT COUNT(*) FROM magic_links WHERE ip_hash=? AND created_at > ?", (ip_hash, since)
    ).fetchone()[0]
    return by_ip >= MAX_PER_IP_PER_HOUR


async def _send_link(to_email: str, link: str) -> None:
    """Mail the link, or — local dev only — log it instead of sending."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        if not _is_local():
            raise HTTPException(503, "Email sign-in isn't configured yet — please use Google or Facebook.")
        logger.warning(f"[dev] no RESEND_API_KEY; magic link for {to_email}: {link}")
        return

    mail_from = os.environ.get("MAIL_FROM", "DataExact <hello@dataexact.io>")
    minutes = TOKEN_TTL_SECONDS // 60
    text = (
        f"Sign in to DataExact\n\n{link}\n\n"
        f"This link works once and expires in {minutes} minutes.\n"
        "If you didn't ask to sign in, you can ignore this email."
    )
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#22261F">
<p>Click below to sign in to DataExact.</p>
<p><a href="{link}" style="display:inline-block;padding:12px 20px;background:#C97A19;color:#fff;text-decoration:none">Sign in</a></p>
<p style="color:#767469;font-size:13px">This link works once and expires in {minutes} minutes.
If you didn't ask to sign in, you can ignore this email.</p>
</div>"""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"from": mail_from, "to": [to_email], "subject": "Sign in to DataExact",
                      "html": html, "text": text},
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        # The token row already exists; it'll simply expire unused.
        logger.warning(f"magic link send failed for {to_email}: {e}")
        raise HTTPException(502, "Couldn't send the email just now — please try again in a moment.")


@router.post("/request")
async def request_link(request: Request, email: str = Form(...), next: str = Form("/")):
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "That doesn't look like an email address.")

    # Same rule as auth.py's OAuth next: relative paths only, so a link can't
    # be used to bounce someone off-site.
    next_url = next if next.startswith("/") else "/"

    token = secrets.token_urlsafe(32)
    now = time.time()
    ip_hash = usage.ip_hash(request)

    with _db() as conn:
        _prune(conn)
        if _rate_limited(conn, email, ip_hash):
            raise HTTPException(429, "Too many sign-in emails requested. Try again in an hour.")
        conn.execute(
            """INSERT INTO magic_links (email, token_hash, next_url, ip_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (email, _hash(token), next_url, ip_hash, now, now + TOKEN_TTL_SECONDS),
        )

    link = f"{BASE_URL}/auth/email/verify?token={token}"
    await _send_link(email, link)

    sent = {"sent": True, "email": email, "expires_in_minutes": TOKEN_TTL_SECONDS // 60}
    # Local dev without a mail provider: hand the link back so the flow is
    # testable. Never reachable once BASE_URL is a real host.
    if not is_configured() and _is_local():
        sent["dev_link"] = link
    return sent


def _consume(token: str):
    """Single-use by construction: the UPDATE only matches a row that is
    unused and unexpired, so two clicks on the same link can't both win."""
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE magic_links SET used_at=? WHERE token_hash=? AND used_at IS NULL AND expires_at > ?",
            (time.time(), _hash(token), time.time()),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT email, next_url FROM magic_links WHERE token_hash=?", (_hash(token),)
        ).fetchone()


@router.get("/verify")
async def verify(request: Request, token: str = ""):
    row = _consume(token) if token else None
    if row is None:
        # Expired, already used, or made up — all the same to the visitor.
        return RedirectResponse(url="/signin/?error=link_expired")

    email, next_url = row
    name = email.split("@")[0]
    user_id = auth.upsert_email_user(email, name)
    auth.sign_in(request, user_id, "email", email, name)
    return RedirectResponse(url=next_url or "/")
