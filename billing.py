"""
Stripe subscriptions, gating what happens once someone's used their free
anonymous reports (see usage.py) and signed in (see auth.py).

Design:
- Two plans, Pro and Business, each a single monthly Stripe Price.
- Checkout and the billing portal are both plain redirects (same pattern as
  auth.py's OAuth login) — no Stripe.js/client secret handling needed here.
- Subscription state (plan, status, current_period_end) lives on the `users`
  row in users.db and is kept in sync purely by webhook events. The app
  never polls Stripe on the request path.

Required environment variables (set in Render, never commit them):
  STRIPE_SECRET_KEY       sk_live_... / sk_test_...
  STRIPE_WEBHOOK_SECRET   whsec_... from the webhook endpoint in the Stripe
                          dashboard (Developers > Webhooks), pointed at
                          BASE_URL + /billing/webhook
  STRIPE_PRICE_PRO        price_... id for the Pro monthly plan
  STRIPE_PRICE_BUSINESS   price_... id for the Business monthly plan

BASE_URL is reused from auth.py.
"""

import os

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import auth

router = APIRouter()

BASE_URL = auth.BASE_URL

PLANS = {
    "pro": {"price_id": os.environ.get("STRIPE_PRICE_PRO"), "name": "Pro"},
    "business": {"price_id": os.environ.get("STRIPE_PRICE_BUSINESS"), "name": "Business"},
}

if os.environ.get("STRIPE_SECRET_KEY"):
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


def is_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _is_active(user: dict | None) -> bool:
    return bool(user) and user.get("subscription_status") in ("active", "trialing")


def is_subscribed(request: Request) -> bool:
    session_user = request.session.get("user")
    if not session_user:
        return False
    return _is_active(auth.get_user(session_user["id"]))


@router.get("/billing/status")
async def status(request: Request):
    session_user = request.session.get("user")
    if not session_user:
        return {"logged_in": False}
    user = auth.get_user(session_user["id"])
    return {
        "logged_in": True,
        "subscribed": _is_active(user),
        "plan": user.get("plan") if user else None,
    }


@router.get("/billing/checkout/{plan}")
async def checkout(plan: str, request: Request):
    if not is_configured():
        return JSONResponse({"error": "billing isn't configured yet"}, status_code=404)
    plan_info = PLANS.get(plan)
    if not plan_info or not plan_info["price_id"]:
        return JSONResponse({"error": f"plan '{plan}' isn't configured yet"}, status_code=404)

    session_user = request.session.get("user")
    if not session_user:
        return JSONResponse({"error": "sign in first"}, status_code=401)

    user = auth.get_user(session_user["id"])
    customer_id = user.get("stripe_customer_id") if user else None
    if not customer_id:
        customer = stripe.Customer.create(
            email=user.get("email"),
            name=user.get("name"),
            metadata={"user_id": str(user["id"])},
        )
        customer_id = customer.id
        auth.set_stripe_customer_id(user["id"], customer_id)

    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": plan_info["price_id"], "quantity": 1}],
        success_url=f"{BASE_URL}/app/?checkout=success",
        cancel_url=f"{BASE_URL}/app/?checkout=cancelled",
        subscription_data={"metadata": {"user_id": str(user["id"]), "plan": plan}},
        metadata={"user_id": str(user["id"]), "plan": plan},
    )
    return RedirectResponse(url=checkout_session.url, status_code=303)


@router.get("/billing/portal")
async def portal(request: Request):
    if not is_configured():
        return JSONResponse({"error": "billing isn't configured yet"}, status_code=404)
    session_user = request.session.get("user")
    if not session_user:
        return JSONResponse({"error": "sign in first"}, status_code=401)

    user = auth.get_user(session_user["id"])
    if not user or not user.get("stripe_customer_id"):
        return JSONResponse({"error": "no billing account on file yet"}, status_code=404)

    portal_session = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=f"{BASE_URL}/app/",
    )
    return RedirectResponse(url=portal_session.url, status_code=303)


@router.post("/billing/webhook")
async def webhook(request: Request):
    if not is_configured():
        return JSONResponse({"error": "billing isn't configured yet"}, status_code=404)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Invalid webhook signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        plan = (obj.get("metadata") or {}).get("plan")
        if customer_id and subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            auth.update_subscription(customer_id, plan, sub["status"], sub["current_period_end"])

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        plan = (obj.get("metadata") or {}).get("plan")
        auth.update_subscription(customer_id, plan, obj.get("status"), obj.get("current_period_end"))

    return {"received": True}
