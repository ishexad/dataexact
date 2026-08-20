"""
Shared "can this request generate a report right now" gate.

Every generating tool (Excel to Word, Compare, Format Report, Detect
Anomalies) goes through the same rule: anonymous visitors get their own
3-free-files allowance *per tool* (usage.py), logged-in visitors need an
active subscription (billing.py) — one subscription unlocks unlimited use
of every tool. One place for this so the tools can't quietly drift apart.
"""

from fastapi import HTTPException, Request

import billing
import usage

FREE_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PRO_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def max_upload_bytes(request: Request) -> int:
    """Per-file upload cap: bigger for subscribed users, matching the
    pricing page's "Files up to 25 MB" claim on Pro/Business."""
    return PRO_MAX_UPLOAD_BYTES if billing.is_subscribed(request) else FREE_MAX_UPLOAD_BYTES


def gate(request: Request, tool: str) -> bool:
    """Raise 402 if this request can't generate right now. Returns whether
    the caller is logged in, so the caller knows whether record_use applies."""
    logged_in = bool(request.session.get("user"))
    if not logged_in:
        usage.enforce_limit(request, tool)
    elif not billing.is_subscribed(request):
        raise HTTPException(402, {
            "reason": "subscription_required",
            "message": "Subscribe to keep generating reports.",
        })
    return logged_in


def require_subscription(request: Request) -> None:
    """Raise 402 unless this request comes from an active subscriber.

    Different rule from gate(): that one gives anonymous visitors a free
    allowance of the tool itself. This is for extras that are paid outright,
    where the free tier gets the tool but not the extra — currently the
    Excel export on Detect Anomalies. Reasons are separated so the UI knows
    whether to offer sign-in or checkout.
    """
    if not request.session.get("user"):
        raise HTTPException(402, {
            "reason": "signin_required",
            "message": "Sign in and subscribe to export your results.",
        })
    if not billing.is_subscribed(request):
        raise HTTPException(402, {
            "reason": "subscription_required",
            "message": "Subscribe to export your results.",
        })


def record_use(request: Request, tool: str, logged_in: bool) -> None:
    if not logged_in:
        usage.record_use(request, tool)
