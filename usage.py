"""
Free-usage limiting for anonymous (not-signed-in) visitors.

Design goals:
- No login required to use any tool at all.
- Each tool has its own independent free allowance — 3 files on Excel to
  Word doesn't touch your 3 files on Detect Anomalies. Once an anonymous
  visitor has used up a given tool's free reports, further attempts on
  *that tool* are blocked until they sign in (and, eventually, subscribe).
  A subscription unlocks unlimited use of every tool — see gating.py.
- Signed-in users (request.session["user"] set by auth.py) are never
  limited here — that's a separate, later gate once subscriptions exist.

Tracking key: a hash of the client's IP address, per tool. This is a soft
gate, not airtight — shared/office IPs share a bucket, and IPs can change.
It's meant to stop casual repeat use without requiring an account, not to
be impossible to bypass. The IP itself is never stored, only a salted hash.

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
WINDOW_SECONDS = 30 * 24 * 60 * 60  # rolling 30-day window per (IP, tool)


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
            CREATE TABLE IF NOT EXISTS anon_usage_by_tool (
                ip_hash TEXT NOT NULL,
                tool TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                window_start REAL NOT NULL,
                PRIMARY KEY (ip_hash, tool)
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


def ip_hash(request: Request) -> str:
    """Public alias: email_auth.py rate-limits sign-in emails by the same
    hashed-IP identity this module gates free files with."""
    return _ip_hash(request)


def _current_count(ip_hash: str, tool: str) -> int:
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT count, window_start FROM anon_usage_by_tool WHERE ip_hash=? AND tool=?",
            (ip_hash, tool),
        ).fetchone()
    if row is None or (now - row[1]) > WINDOW_SECONDS:
        return 0
    return row[0]


def remaining(request: Request, tool: str) -> int:
    count = _current_count(_ip_hash(request), tool)
    return max(0, FREE_LIMIT - count)


def enforce_limit(request: Request, tool: str) -> None:
    """Raise 402 if this IP has used up its free files on this tool for the current window."""
    if _current_count(_ip_hash(request), tool) >= FREE_LIMIT:
        raise HTTPException(402, {
            "reason": "free_limit",
            "message": "You've used your 3 free files on this tool this month. Sign in to keep going.",
        })


def record_use(request: Request, tool: str) -> None:
    """Call once a file has actually been generated/processed successfully."""
    ip_hash = _ip_hash(request)
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT count, window_start FROM anon_usage_by_tool WHERE ip_hash=? AND tool=?",
            (ip_hash, tool),
        ).fetchone()
        if row is None or (now - row[1]) > WINDOW_SECONDS:
            conn.execute("""
                INSERT INTO anon_usage_by_tool (ip_hash, tool, count, window_start) VALUES (?, ?, 1, ?)
                ON CONFLICT(ip_hash, tool) DO UPDATE SET count=1, window_start=excluded.window_start
            """, (ip_hash, tool, now))
        else:
            conn.execute(
                "UPDATE anon_usage_by_tool SET count = count + 1 WHERE ip_hash=? AND tool=?",
                (ip_hash, tool),
            )
