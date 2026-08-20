"""
Usage event logging, plus a private admin view of the numbers.

Why this exists: auth.py knows who *logged in*, and usage.py counts free
files per IP so it can enforce the limit — neither tells you what the tools
are actually being used for. This module records one row per meaningful step
(upload, processing_complete, download) for every tool, tagged with a session
id so a single visitor's activity can be followed even when they never sign
in, and exposes it at /admin/stats.

Session identity: SessionMiddleware gives every visitor a signed cookie but
no id of its own, so the first event of a visit mints a UUID and stores it in
the session as "sid". It survives for the life of the cookie and is recorded
on every event — signed in or not — which makes it the thing to group by when
you want "how many people", not "how many clicks". Signed-in visitors carry
`user_id` as well, so their sessions can be tied back to a real account.

Failures are logged too (success=0 with the error text), so this doubles as
error monitoring: /admin/stats lists the most recent ones.

Storage: the same SQLite file as auth.py/usage.py (users.db).

Environment:
  ADMIN_EMAILS   comma-separated list of emails allowed to open
                 /admin/stats, matched against the signed-in OAuth account.
                 Unset means nobody can — the route 404s for everyone.
"""

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

DB_PATH = "users.db"

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/admin")

# Matched case-insensitively against the email on the OAuth session.
def _parse_admin_emails(raw: str) -> set:
    """Split the ADMIN_EMAILS value into addresses.

    This is typed into a hosting dashboard by hand, so it tolerates the usual
    damage: surrounding quotes, stray spaces around the commas, and any
    casing. A value pasted as "me@example.com" with the quotes included would
    otherwise never match anything, and the only symptom would be a 404.
    """
    emails = set()
    for part in raw.split(","):
        cleaned = part.strip().strip('"').strip("'").strip().lower()
        if cleaned:
            emails.add(cleaned)
    return emails


ADMIN_EMAILS = _parse_admin_emails(os.environ.get("ADMIN_EMAILS", ""))

# Tool ids match the ones gating.py/usage.py already use, so the free-limit
# counters and these events describe the same thing by the same name.
TOOL_LABELS = {
    "excel_to_word": "Excel to Word",
    "compare": "Compare Excel Files",
    "format": "Format Report",
    "anomaly": "Detect Anomalies",
    "codebook": "Qualitative Coding",
}

EVENT_UPLOAD = "upload"
EVENT_PROCESSED = "processing_complete"
EVENT_DOWNLOAD = "download"

RECENT_SESSION_LIMIT = 200
RECENT_FAILURE_LIMIT = 50
DAILY_WINDOW_DAYS = 14
MAX_ERROR_CHARS = 500


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
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                session_id TEXT,
                tool TEXT NOT NULL,
                event_type TEXT NOT NULL,
                file_size_bytes INTEGER,
                success BOOLEAN DEFAULT 1,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # The admin view sorts by time and groups by session; without these
        # it degrades into a full scan once the table has real volume.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_created_at ON usage_events(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id)")


_init_db()

if ADMIN_EMAILS:
    logger.info(f"admin stats: /admin/stats allows {', '.join(sorted(ADMIN_EMAILS))}")
else:
    logger.warning(
        "ADMIN_EMAILS is not set — /admin/stats will 404 for everyone. "
        "Set it to a comma-separated list of the emails allowed in."
    )


def session_id(request: Request) -> str:
    """Stable per-visitor id, minted on first use and kept in the session
    cookie. Anonymous visitors get one too — that's the whole point."""
    sid = request.session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        request.session["sid"] = sid
    return sid


def log_event(request: Request, tool: str, event_type: str,
              file_size_bytes: int = None, success: bool = True,
              error_message: str = None) -> None:
    """Record one usage event. Never raises: analytics failing is not a
    reason for a user's report to fail, so problems are logged and dropped."""
    try:
        user = request.session.get("user") or {}
        user_id = str(user["id"]) if user.get("id") is not None else None
        if error_message:
            error_message = error_message[:MAX_ERROR_CHARS]

        with _db() as conn:
            conn.execute(
                """INSERT INTO usage_events
                   (user_id, session_id, tool, event_type, file_size_bytes, success, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, session_id(request), tool, event_type,
                 file_size_bytes, 1 if success else 0, error_message),
            )
    except Exception as e:
        logger.warning(f"usage logging failed ({tool}/{event_type}): {e}")


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------

def _user_labels(conn) -> dict:
    """user_id -> something human ('email', or the name/id if no email)."""
    labels = {}
    for row in conn.execute("SELECT id, email, name FROM users").fetchall():
        labels[str(row[0])] = row[1] or row[2] or f"user #{row[0]}"
    return labels


def collect_stats() -> dict:
    with _db() as conn:
        totals_row = conn.execute("""
            SELECT COUNT(*),
                   COUNT(DISTINCT session_id),
                   COUNT(DISTINCT user_id),
                   SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),
                   MIN(created_at),
                   MAX(created_at)
            FROM usage_events
        """, (EVENT_PROCESSED,)).fetchone()

        totals = {
            "events": totals_row[0] or 0,
            "sessions": totals_row[1] or 0,
            "signed_in_users": totals_row[2] or 0,
            "files_processed": totals_row[3] or 0,
            "failures": totals_row[4] or 0,
            "first_event": totals_row[5],
            "last_event": totals_row[6],
        }

        by_tool = [
            {
                "tool": row[0],
                "label": TOOL_LABELS.get(row[0], row[0]),
                "uploads": row[1],
                "processed": row[2],
                "downloads": row[3],
                "failures": row[4],
                "sessions": row[5],
            }
            for row in conn.execute("""
                SELECT tool,
                       SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT session_id)
                FROM usage_events
                GROUP BY tool
                ORDER BY 3 DESC, 2 DESC
            """, (EVENT_UPLOAD, EVENT_PROCESSED, EVENT_DOWNLOAD)).fetchall()
        ]

        daily = [
            {"date": row[0], "events": row[1], "sessions": row[2], "processed": row[3]}
            for row in conn.execute(f"""
                SELECT DATE(created_at),
                       COUNT(*),
                       COUNT(DISTINCT session_id),
                       SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END)
                FROM usage_events
                WHERE created_at >= DATETIME('now', '-{DAILY_WINDOW_DAYS} days')
                GROUP BY DATE(created_at)
                ORDER BY 1 DESC
            """, (EVENT_PROCESSED,)).fetchall()
        ]

        labels = _user_labels(conn)
        sessions = [
            {
                "session_id": row[0],
                "user_id": row[1],
                "user": labels.get(row[1], f"user #{row[1]}") if row[1] else None,
                "tools": [TOOL_LABELS.get(t, t) for t in (row[2] or "").split(",") if t],
                "events": row[3],
                "processed": row[4],
                "failures": row[5],
                "first_seen": row[6],
                "last_seen": row[7],
            }
            for row in conn.execute("""
                SELECT session_id,
                       MAX(user_id),
                       GROUP_CONCAT(DISTINCT tool),
                       COUNT(*),
                       SUM(CASE WHEN event_type=? AND success=1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),
                       MIN(created_at),
                       MAX(created_at)
                FROM usage_events
                GROUP BY session_id
                ORDER BY MAX(created_at) DESC
                LIMIT ?
            """, (EVENT_PROCESSED, RECENT_SESSION_LIMIT)).fetchall()
        ]

        failures = [
            {
                "created_at": row[0],
                "tool": TOOL_LABELS.get(row[1], row[1]),
                "event_type": row[2],
                "session_id": row[3],
                "user": labels.get(row[4], f"user #{row[4]}") if row[4] else None,
                "error_message": row[5],
            }
            for row in conn.execute("""
                SELECT created_at, tool, event_type, session_id, user_id, error_message
                FROM usage_events
                WHERE success=0
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, (RECENT_FAILURE_LIMIT,)).fetchall()
        ]

    return {
        "totals": totals,
        "by_tool": by_tool,
        "daily": daily,
        "sessions": sessions,
        "recent_failures": failures,
    }


# ---------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------

def _require_admin(request: Request):
    """404 rather than 403 — an unauthorised visitor shouldn't learn that
    this route exists at all.

    Hiding the reason from a stranger also hides it from the owner, who then
    can't tell an unset variable from a mistyped one from signing in with the
    wrong account. The response stays a bare 404; the server log carries what
    it can't say.
    """
    user = request.session.get("user")
    email = ((user or {}).get("email") or "").strip().lower()

    if user and email in ADMIN_EMAILS:
        return user

    if not ADMIN_EMAILS:
        logger.warning(
            "/admin/stats refused: ADMIN_EMAILS is empty. Set it to "
            f"{email or 'the address you sign in with'} to grant access."
        )
    elif user:
        logger.warning(
            f"/admin/stats refused for {email or '(no email on this account)'} — "
            f"ADMIN_EMAILS currently allows {', '.join(sorted(ADMIN_EMAILS))}"
        )
    else:
        logger.info("/admin/stats refused: not signed in")

    raise HTTPException(404)


@router.get("/stats")
async def admin_stats(request: Request):
    _require_admin(request)
    return HTMLResponse(_render(collect_stats()))


@router.get("/stats.json")
async def admin_stats_json(request: Request):
    _require_admin(request)
    return collect_stats()


# ---------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------

_STYLES = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 24px 64px; background: #f6f7f9; color: #1c1f23;
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em;
     color: #6b7280; margin: 40px 0 12px; }
.sub { color: #6b7280; margin: 0 0 8px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 24px; }
.card { background: #fff; border: 1px solid #e3e6ea; border-radius: 10px; padding: 16px; }
.card .n { font-size: 26px; font-weight: 650; letter-spacing: -.02em; }
.card .l { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: .05em; margin-top: 4px; }
.card.bad .n { color: #b42318; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #e3e6ea; border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #eef0f3; font-size: 14px; }
th { background: #fafbfc; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: #384049; }
td.muted, .muted { color: #9aa1a9; }
td.bad { color: #b42318; }
.scroll { overflow-x: auto; }
.bar { height: 8px; background: #2563eb; border-radius: 4px; min-width: 2px; }
.empty { color: #6b7280; background: #fff; border: 1px dashed #d7dbe0;
         border-radius: 10px; padding: 20px; text-align: center; font-size: 14px; }
.pill { display: inline-block; background: #eef2ff; color: #3730a3; border-radius: 20px;
        padding: 1px 9px; font-size: 12px; margin: 0 4px 2px 0; }
.anon { background: #f1f3f5; color: #6b7280; }
@media (prefers-color-scheme: dark) {
  body { background: #14171a; color: #e6e8ea; }
  .card, table, .empty { background: #1c2024; border-color: #2c3238; }
  th { background: #22272c; color: #98a2ac; }
  th, td { border-color: #262c31; }
  td.mono { color: #b9c2cb; }
  .pill { background: #23304d; color: #b6c8f0; }
  .anon { background: #262c31; color: #98a2ac; }
}
"""


def _fmt(value) -> str:
    return escape("—" if value in (None, "") else str(value))


def _table(headers, rows, classes=None, empty="Nothing logged yet.") -> str:
    """headers/classes are parallel lists; every cell is escaped here so no
    caller can inject markup into the page."""
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    classes = classes or [""] * len(headers)
    head = "".join(f'<th class="{c}">{escape(str(h))}</th>' for h, c in zip(headers, classes))
    body = ""
    for row in rows:
        cells = "".join(f'<td class="{c}">{_fmt(v)}</td>' for v, c in zip(row, classes))
        body += f"<tr>{cells}</tr>"
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _daily_table(daily) -> str:
    if not daily:
        return '<p class="empty">Nothing logged in the last two weeks.</p>'
    peak = max(d["events"] for d in daily) or 1
    rows = ""
    for d in daily:
        width = max(2, round(d["events"] / peak * 100))
        rows += (
            f'<tr><td class="mono">{_fmt(d["date"])}</td>'
            f'<td class="num">{d["events"]}</td>'
            f'<td class="num">{d["sessions"]}</td>'
            f'<td class="num">{d["processed"] or 0}</td>'
            f'<td style="width:45%"><div class="bar" style="width:{width}%"></div></td></tr>'
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Date (UTC)</th><th class="num">Events</th>'
        '<th class="num">Sessions</th><th class="num">Files processed</th><th></th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _render(stats: dict) -> str:
    t = stats["totals"]

    cards = "".join(
        f'<div class="card{cls}"><div class="n">{value:,}</div><div class="l">{escape(label)}</div></div>'
        for label, value, cls in (
            ("Sessions", t["sessions"], ""),
            ("Files processed", t["files_processed"], ""),
            ("Events logged", t["events"], ""),
            ("Signed-in users", t["signed_in_users"], ""),
            ("Failures", t["failures"], " bad" if t["failures"] else ""),
        )
    )

    tool_rows = [
        (r["label"], r["sessions"], r["uploads"], r["processed"], r["downloads"], r["failures"])
        for r in stats["by_tool"]
    ]

    session_rows = ""
    for s in stats["sessions"]:
        who = (
            f'<span class="pill">{escape(s["user"])}</span>'
            if s["user"] else '<span class="pill anon">anonymous</span>'
        )
        tools = "".join(f'<span class="pill">{escape(name)}</span>' for name in s["tools"]) or "—"
        failures = f'<td class="num bad">{s["failures"]}</td>' if s["failures"] else '<td class="num muted">0</td>'
        session_rows += (
            f'<tr><td class="mono">{_fmt(s["session_id"])}</td><td>{who}</td><td>{tools}</td>'
            f'<td class="num">{s["events"]}</td><td class="num">{s["processed"] or 0}</td>{failures}'
            f'<td class="mono">{_fmt(s["first_seen"])}</td><td class="mono">{_fmt(s["last_seen"])}</td></tr>'
        )
    sessions_table = (
        '<div class="scroll"><table><thead><tr><th>Session ID</th><th>Who</th><th>Tools used</th>'
        '<th class="num">Events</th><th class="num">Processed</th><th class="num">Failures</th>'
        '<th>First seen (UTC)</th><th>Last seen (UTC)</th></tr></thead>'
        f"<tbody>{session_rows}</tbody></table></div>"
        if stats["sessions"] else '<p class="empty">No sessions logged yet.</p>'
    )

    failure_rows = [
        (f["created_at"], f["tool"], f["event_type"], f["session_id"],
         f["user"] or "anonymous", f["error_message"])
        for f in stats["recent_failures"]
    ]

    span = ""
    if t["first_event"]:
        span = f'<p class="sub">Logging since {escape(str(t["first_event"]))} UTC · last event {escape(str(t["last_event"]))} UTC</p>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>DataExact · Usage stats</title>
<style>{_STYLES}</style>
</head><body><div class="wrap">
<h1>Usage stats</h1>
{span}
<div class="cards">{cards}</div>

<h2>By tool</h2>
{_table(
    ["Tool", "Sessions", "Uploads", "Processed", "Downloads", "Failures"],
    tool_rows,
    ["", "num", "num", "num", "num", "num"],
    empty="No tool activity logged yet.",
)}

<h2>Last {DAILY_WINDOW_DAYS} days</h2>
{_daily_table(stats["daily"])}

<h2>Sessions · most recent {RECENT_SESSION_LIMIT}</h2>
<p class="sub">One row per visitor session. Anonymous sessions are tracked by
the session cookie only; signed-in ones also carry the account.</p>
{sessions_table}

<h2>Recent failures</h2>
{_table(
    ["When (UTC)", "Tool", "Stage", "Session ID", "Who", "Error"],
    failure_rows,
    ["mono", "", "", "mono", "", ""],
    empty="No failures logged. ",
)}
</div></body></html>"""
