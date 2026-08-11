"""
Shared "can this request generate a report right now" gate.

Every generating tool (Excel to Word, Format Report, ...) goes through the
same rule: anonymous visitors get a shared pool of free reports (usage.py),
logged-in visitors need an active subscription (billing.py). One place for
this so the tools can't quietly drift apart.
"""

from fastapi import HTTPException, Request

import billing
import usage


def gate(request: Request) -> bool:
    """Raise 402 if this request can't generate right now. Returns whether
    the caller is logged in, so the caller knows whether record_use applies."""
    logged_in = bool(request.session.get("user"))
    if not logged_in:
        usage.enforce_limit(request)
    elif not billing.is_subscribed(request):
        raise HTTPException(402, {
            "reason": "subscription_required",
            "message": "Subscribe to keep generating reports.",
        })
    return logged_in


def record_use(request: Request, logged_in: bool) -> None:
    if not logged_in:
        usage.record_use(request)
