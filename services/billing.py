"""Monthly plans and the two ways to pay for them.

- Instagram users: Razorpay Subscriptions. The bot DMs the subscription's
  hosted payment link; Razorpay's webhook tells us about every paid cycle.
- App users: Google Play subscriptions (required by Play for in-app digital
  goods). The app sends the purchase token; we verify it with the Play
  Developer API, and Play's real-time notifications keep it in sync.

Either way a paid cycle becomes db.apply_renewal(): the plan until the period
end, topped up to the plan's units, exactly once per payment (idempotency key).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app import config

from . import db

log = logging.getLogger(__name__)

RAZORPAY_API = "https://api.razorpay.com/v1"
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
# Play states in which the user has paid for the current period.
PLAY_ENTITLED = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"}


class BillingError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    key: str  # basic | pro
    title: str
    units: int
    price_inr: int
    razorpay_plan_id: str
    play_product_id: str

    @property
    def reels(self) -> int:
        return self.units // config.UNITS_PER_REEL


def plans() -> dict[str, Plan]:
    """Read at call time so prices and IDs can change with an env var (and in tests)."""
    return {
        "basic": Plan("basic", "Basic", config.PLAN_BASIC_UNITS, config.PLAN_BASIC_PRICE_INR,
                      config.RAZORPAY_PLAN_BASIC, config.PLAY_PRODUCT_BASIC),
        "pro": Plan("pro", "Pro", config.PLAN_PRO_UNITS, config.PLAN_PRO_PRICE_INR,
                    config.RAZORPAY_PLAN_PRO, config.PLAY_PRODUCT_PRO),
    }


def plan_by(field: str, value: str | None) -> Plan | None:
    return next((p for p in plans().values() if value and getattr(p, field) == value), None)


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _day(when: datetime | None) -> str:
    return when.strftime("%d %b %Y") if when else "the end of this month"


# ---------------------------------------------------------------- Razorpay


def razorpay_enabled() -> bool:
    return bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET and any(p.razorpay_plan_id for p in plans().values()))


def verify_razorpay_signature(body: bytes, header: str | None) -> bool:
    if not (config.RAZORPAY_WEBHOOK_SECRET and header):
        return False
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


async def _razorpay(method: str, path: str, client: httpx.AsyncClient | None = None, **kwargs) -> dict:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.request(
            method, f"{RAZORPAY_API}{path}", auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET), **kwargs
        )
    finally:
        if own:
            await client.aclose()
    if r.status_code >= 400:
        raise BillingError(f"razorpay HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


async def razorpay_checkout(account_id: int, plan: Plan, client: httpx.AsyncClient | None = None) -> str:
    """Create a subscription and return Razorpay's hosted payment link for it."""
    if not plan.razorpay_plan_id:
        raise BillingError(f"RAZORPAY_PLAN_{plan.key.upper()} is not set")
    sub = await _razorpay("POST", "/subscriptions", client, json={
        "plan_id": plan.razorpay_plan_id,
        "total_count": config.RAZORPAY_TOTAL_COUNT,
        "customer_notify": True,
        "notes": {"account_id": str(account_id), "plan": plan.key},
    })
    await db.upsert_subscription(account_id, "razorpay", sub["id"], plan.key, sub.get("status") or "created")
    return sub["short_url"]


async def razorpay_cancel(subscription_id: str, client: httpx.AsyncClient | None = None) -> dict:
    """Stop renewing; the plan stays until the current period ends."""
    return await _razorpay("POST", f"/subscriptions/{subscription_id}/cancel", client,
                           json={"cancel_at_cycle_end": True})


async def stop_renewals(account_id: int) -> None:
    """Cancel a renewing Razorpay plan (e.g. before deleting the account). Play plans can
    only be cancelled by the user in Google Play."""
    current = await db.live_subscription(account_id, "razorpay")
    if current and current["status"] in ("active", "authenticated", "pending") and razorpay_enabled():
        try:
            await razorpay_cancel(current["external_id"])
        except BillingError as e:
            log.warning("could not cancel %s: %s", current["external_id"], e)


async def handle_razorpay(event: dict) -> tuple[int | None, str | None]:
    """Apply one webhook event. Returns (account_id, message for the user or None)."""
    name = event.get("event", "")
    payload = event.get("payload") or {}
    sub = (payload.get("subscription") or {}).get("entity") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}
    sub_id = sub.get("id")
    if not sub_id:
        return None, None
    known = await db.subscription("razorpay", sub_id)
    notes = sub.get("notes") if isinstance(sub.get("notes"), dict) else {}
    # A known subscription follows its account through linking; notes are the fallback.
    account_id = known["account_id"] if known else (int(notes["account_id"]) if notes.get("account_id") else None)
    plan = plan_by("razorpay_plan_id", sub.get("plan_id")) or plans().get(notes.get("plan") or (known or {}).get("plan", ""))
    if account_id is None or plan is None or await db.get_account(account_id) is None:
        log.warning("razorpay %s for unknown account/plan (sub %s)", name, sub_id)
        return None, None

    period_end = _ts(sub.get("current_end"))
    await db.upsert_subscription(account_id, "razorpay", sub_id, plan.key, sub.get("status") or name.split(".")[-1], period_end)

    if name == "subscription.charged" and payment.get("id"):
        applied = await db.apply_renewal(
            account_id, plan.key, plan.units, period_end, f"razorpay:{payment['id']}", f"{plan.title} via Razorpay"
        )
        if applied:
            return account_id, (
                f"✅ Payment received — you're on {plan.title} until {_day(period_end)}: "
                f"{plan.reels} reels this month. Thank you!"
            )
    elif name == "subscription.halted":
        return account_id, "⚠️ Your plan's renewal payment failed. Update your payment method from Razorpay's email to keep it."
    elif name == "subscription.cancelled":
        return account_id, f"Your {plan.title} plan won't renew. It stays active until {_day(period_end)}."
    return account_id, None


# ---------------------------------------------------------------- Google Play

_play_credentials = None


def play_enabled() -> bool:
    return bool(config.PLAY_SERVICE_ACCOUNT_JSON)


def _play_token_sync() -> str:
    global _play_credentials
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    if _play_credentials is None:
        info = json.loads(config.PLAY_SERVICE_ACCOUNT_JSON)
        _play_credentials = service_account.Credentials.from_service_account_info(info, scopes=[PLAY_SCOPE])
    if not _play_credentials.valid:
        _play_credentials.refresh(Request())
    return _play_credentials.token


async def _play(method: str, url: str, client: httpx.AsyncClient | None = None) -> dict:
    token = await asyncio.to_thread(_play_token_sync)
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                 json={} if method == "POST" else None)
    finally:
        if own:
            await client.aclose()
    if r.status_code >= 400:
        raise BillingError(f"play HTTP {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


async def play_sync(account_id: int, purchase_token: str, client: httpx.AsyncClient | None = None) -> db.Account:
    """Verify a Play subscription purchase with Google and apply any paid cycle we haven't yet."""
    owner = await db.subscription("play", purchase_token)
    if owner and owner["account_id"] != account_id:
        raise BillingError("this purchase belongs to another account")
    pkg = config.PLAY_PACKAGE_NAME
    data = await _play("GET", f"{PLAY_API}/{pkg}/purchases/subscriptionsv2/tokens/{purchase_token}", client)
    obfuscated = (data.get("externalAccountIdentifiers") or {}).get("obfuscatedExternalAccountId")
    if obfuscated and obfuscated != str(account_id):
        raise BillingError("purchase was made for another account")
    item = (data.get("lineItems") or [{}])[0]
    plan = plan_by("play_product_id", item.get("productId"))
    if plan is None:
        raise BillingError(f"unknown product {item.get('productId')}")
    state = data.get("subscriptionState", "")
    expiry = datetime.fromisoformat(item["expiryTime"].replace("Z", "+00:00")) if item.get("expiryTime") else None
    status = state.removeprefix("SUBSCRIPTION_STATE_").lower() or "unknown"
    await db.upsert_subscription(account_id, "play", purchase_token, plan.key, status, expiry)

    order = item.get("latestSuccessfulOrderId") or data.get("latestOrderId")
    if state in PLAY_ENTITLED and order and expiry and expiry > datetime.now(UTC):
        await db.apply_renewal(account_id, plan.key, plan.units, expiry, f"play:{order}", f"{plan.title} via Google Play")
    if data.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING" and state in PLAY_ENTITLED:
        # Unacknowledged purchases are refunded by Google after 3 days.
        await _play("POST", f"{PLAY_API}/{pkg}/purchases/subscriptions/{item.get('productId')}"
                            f"/tokens/{purchase_token}:acknowledge", client)
    return await db.get_account(account_id)


def parse_play_notification(body: dict) -> str | None:
    """Pub/Sub push body → the purchase token it is about (None for test pings / other types)."""
    try:
        data = json.loads(base64.b64decode((body.get("message") or {}).get("data", "")))
    except (ValueError, TypeError):
        return None
    return (data.get("subscriptionNotification") or {}).get("purchaseToken")


async def handle_play_notification(body: dict) -> None:
    token = parse_play_notification(body)
    if not token:
        return
    known = await db.subscription("play", token)
    if not known:  # the app hasn't reported this purchase yet; it will call /v1/billing/play
        log.info("play notification for unknown purchase token")
        return
    await play_sync(known["account_id"], token)
