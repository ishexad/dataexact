"""
Free-usage limiting for anonymous (not-signed-in) visitors.

Design goals:
- No login required to use the tool at all.
- Once an anonymous visitor has generated FREE_LIMIT reports, further
  attempts are blocked until they sign in (and, eventually, subscribe).
- Signed-in users (request.session["user"] set by auth.py) are never
  limited here — that's a separate, later gate once subscriptions exist.

Tracking key: a hash of the client's IP address. This is a soft gate, not
airtight — shared/office IPs share a bucket, and IPs can change. It's meant
to stop casual repeat use without requiring an account, not to be
impossible to bypass. The IP itself is never stored, only a salted hash.

Storage: reuses the same SQLite file as auth.py (users.db).
"""

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager

from fastapi import HTTPException, Request

DB_PATH = "users.db"
FREE_LIMIT = 3
WINDOW_SECONDS = 30 * 24 * 60 * 60  # rolling 30-day window per IP


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
            CREATE TABLE IF NOT EXISTS anon_usage (
                ip_hash TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                window_start REAL NOT NULL
            )
        """)


_init_db()


def _client_ip(request: Request) -> str:
    # Render (and most reverse proxies) sit in front of the app, so the
    # real client IP arrives via X-Forwarded-For rather than the socket peer.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _ip_hash(request: Request) -> str:
    salt = os.environ.get("USAGE_SALT", "dev-only-salt-change-me")
    ip = _client_ip(request)
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def _current_count(ip_hash: str) -> int:
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT count, window_start FROM anon_usage WHERE ip_hash=?", (ip_hash,)
        ).fetchone()
    if row is None or (now - row[1]) > WINDOW_SECONDS:
        return 0
    return row[0]


def remaining(request: Request) -> int:
    count = _current_count(_ip_hash(request))
    return max(0, FREE_LIMIT - count)


def enforce_limit(request: Request) -> None:
    """Raise 402 if this IP has used up its free reports for the current window."""
    if _current_count(_ip_hash(request)) >= FREE_LIMIT:
        raise HTTPException(402, {
            "reason": "free_limit",
            "message": "You've used your 3 free reports this month. Sign in to keep going.",
        })


def record_use(request: Request) -> None:
    """Call once a report has actually been generated successfully."""
    ip_hash = _ip_hash(request)
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT count, window_start FROM anon_usage WHERE ip_hash=?", (ip_hash,)
        ).fetchone()
        if row is None or (now - row[1]) > WINDOW_SECONDS:
            conn.execute("""
                INSERT INTO anon_usage (ip_hash, count, window_start) VALUES (?, 1, ?)
                ON CONFLICT(ip_hash) DO UPDATE SET count=1, window_start=excluded.window_start
            """, (ip_hash, now))
        else:
            conn.execute("UPDATE anon_usage SET count = count + 1 WHERE ip_hash=?", (ip_hash,))
