import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import config, inbound
from channels.instagram import Inbound
from services import billing

NOW = datetime.now(UTC)


@pytest.fixture
def razorpay(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(config, "RAZORPAY_PLAN_BASIC", "plan_basic00000001")
    monkeypatch.setattr(config, "RAZORPAY_PLAN_PRO", "plan_pro0000000001")
    monkeypatch.setattr(config, "FREE_UNITS", 20)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/v1/subscriptions":
            return httpx.Response(200, json={"id": f"sub_{len(calls)}", "status": "created",
                                             "short_url": f"https://rzp.io/i/{len(calls)}"})
        return httpx.Response(200, json={"id": "sub_x", "status": "active"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real = billing._razorpay

    async def via_mock(method, path, client_=None, **kw):
        return await real(method, path, client, **kw)

    monkeypatch.setattr(billing, "_razorpay", via_mock)
    return calls


def charged(sub_id, payment_id, plan_id="plan_pro0000000001", account_id=None, end=NOW + timedelta(days=30)):
    notes = {"account_id": str(account_id), "plan": "pro"} if account_id else {}
    return {
        "event": "subscription.charged",
        "payload": {
            "subscription": {"entity": {"id": sub_id, "plan_id": plan_id, "status": "active",
                                        "current_end": int(end.timestamp()), "notes": notes}},
            "payment": {"entity": {"id": payment_id, "amount": 29900}},
        },
    }


async def test_signature():
    body = b'{"event":"x"}'
    config.RAZORPAY_WEBHOOK_SECRET = "whsec"
    try:
        good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        assert billing.verify_razorpay_signature(body, good)
        assert not billing.verify_razorpay_signature(body, "0" * 64)
        assert not billing.verify_razorpay_signature(body, None)
    finally:
        config.RAZORPAY_WEBHOOK_SECRET = ""


async def test_plans_and_checkout_from_the_dm(pg, razorpay):
    acc, _ = await pg.ig_account("buyer")
    reply = await inbound.route(Inbound("buyer", "m1", "plans"), acc, False)
    assert "₹99" in reply.text and "₹299" in reply.text
    assert reply.options == [("Basic ₹99/mo", "PLAN_BASIC"), ("Pro ₹299/mo", "PLAN_PRO")]

    reply = await inbound.route(Inbound("buyer", "m2", "Pro ₹299/mo", quick_reply="PLAN_PRO"), acc, False)
    assert "https://rzp.io/i/1" in reply.text
    method, path, body = razorpay[0]
    assert (method, path) == ("POST", "/v1/subscriptions")
    assert body["plan_id"] == "plan_pro0000000001" and body["notes"] == {"account_id": str(acc.id), "plan": "pro"}
    assert (await pg.live_subscription(acc.id))["status"] == "created"


async def test_paywall_offers_plans(pg, razorpay, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 0)
    acc, _ = await pg.ig_account("broke")
    reply = await inbound.route(Inbound("broke", "m1", "what was that about?"), acc, False)
    assert reply.text.startswith(inbound.OUT_OF_CREDITS) and reply.options


async def test_charge_webhook_grants_once_and_renewal_resets(pg, razorpay):
    acc, _ = await pg.ig_account("payer")
    await billing.razorpay_checkout(acc.id, billing.plans()["pro"])
    sub_id = (await pg.live_subscription(acc.id))["external_id"]

    account_id, msg = await billing.handle_razorpay(charged(sub_id, "pay_1"))
    assert account_id == acc.id and "Pro" in msg and "150 reels" in msg
    again = await billing.handle_razorpay(charged(sub_id, "pay_1"))  # retry of the same payment
    assert again == (acc.id, None)
    a = await pg.get_account(acc.id)
    assert a.plan == "pro" and a.units == 20 + 1500  # first purchase keeps leftover free units
    assert a.priority == 10

    await pg.pool().execute("UPDATE wallets SET units = 700 WHERE account_id = $1", acc.id)
    await billing.handle_razorpay(charged(sub_id, "pay_2", end=NOW + timedelta(days=60)))
    a = await pg.get_account(acc.id)
    assert a.units == 1500  # month 2: unused units expire, no rollover
    kinds = [r["kind"] for r in await pg.pool().fetch("SELECT kind FROM ledger WHERE account_id = $1 ORDER BY id", acc.id)]
    assert kinds == ["grant", "purchase", "purchase", "expire"]


async def test_unknown_subscription_uses_notes(pg, razorpay):
    acc, _ = await pg.ig_account("notes")
    account_id, msg = await billing.handle_razorpay(charged("sub_new", "pay_9", account_id=acc.id))
    assert account_id == acc.id and msg
    assert await billing.handle_razorpay({"event": "subscription.charged", "payload": {}}) == (None, None)


async def test_cancel_and_halt(pg, razorpay):
    acc, _ = await pg.ig_account("leaver")
    await billing.razorpay_checkout(acc.id, billing.plans()["basic"])
    sub_id = (await pg.live_subscription(acc.id))["external_id"]
    await billing.handle_razorpay(charged(sub_id, "pay_b", plan_id="plan_basic00000001"))
    acc = await pg.get_account(acc.id)

    reply = await inbound.route(Inbound("leaver", "m1", "cancel"), acc, False)
    assert "won't renew" in reply.text
    assert razorpay[-1][1] == f"/v1/subscriptions/{sub_id}/cancel" and razorpay[-1][2] == {"cancel_at_cycle_end": True}

    event = charged(sub_id, "pay_b", plan_id="plan_basic00000001")
    event["event"] = "subscription.halted"
    _, msg = await billing.handle_razorpay(event)
    assert "failed" in msg
    assert "Plan: Basic" in (await inbound.route(Inbound("leaver", "m2", "credits"), acc, False)).text


async def test_expired_plans_go_back_to_free(pg):
    acc, _ = await pg.ig_account("lapsed")
    await pg.apply_renewal(acc.id, "basic", 300, NOW - timedelta(days=3), "k1", "test")
    assert (await pg.get_account(acc.id)).plan == "basic"
    assert await pg.downgrade_expired() == [acc.id]
    a = await pg.get_account(acc.id)
    assert a.plan == "free" and a.units == 320  # units stay


async def test_unlimited_accounts_keep_their_plan(pg):
    admin, _ = await pg.ig_account("admin-1")
    await pg.apply_renewal(admin.id, "pro", 1500, NOW + timedelta(days=30), "k2", "test")
    assert (await pg.get_account(admin.id)).plan == "unlimited"


# ---------------------------------------------------------------- Google Play


@pytest.fixture
def play(monkeypatch):
    monkeypatch.setattr(config, "PLAY_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(billing, "_play_token_sync", lambda: "tok")
    state = {"data": {}, "calls": []}

    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json=state["data"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real = billing._play

    async def via_mock(method, url, client_=None):
        return await real(method, url, client)

    monkeypatch.setattr(billing, "_play", via_mock)
    return state


def play_data(account_id, order="GPA.1", state="SUBSCRIPTION_STATE_ACTIVE", ack="ACKNOWLEDGEMENT_STATE_PENDING"):
    return {
        "subscriptionState": state, "acknowledgementState": ack,
        "externalAccountIdentifiers": {"obfuscatedExternalAccountId": str(account_id)},
        "lineItems": [{"productId": "reelbrain_basic", "latestSuccessfulOrderId": order,
                       "expiryTime": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")}],
    }


async def test_play_purchase_grants_and_acknowledges(pg, play):
    acc, _ = await pg.google_account("g-play", "p@x.com", "P")
    play["data"] = play_data(acc.id)
    a = await billing.play_sync(acc.id, "token-123456")
    assert a.plan == "basic" and a.units == 20 + 300
    assert play["calls"][-1][0] == "POST" and play["calls"][-1][1].endswith("/tokens/token-123456:acknowledge")
    await billing.play_sync(acc.id, "token-123456")  # restore on app start: no double grant
    assert (await pg.get_account(acc.id)).units == 320

    other, _ = await pg.google_account("g-other", None, None)
    with pytest.raises(billing.BillingError):
        await billing.play_sync(other.id, "token-123456")  # someone else's purchase token


async def test_play_renewal_via_notification(pg, play):
    acc, _ = await pg.google_account("g-renew", None, None)
    play["data"] = play_data(acc.id)
    await billing.play_sync(acc.id, "token-abcdefgh")
    play["data"] = play_data(acc.id, order="GPA.1..0", ack="ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED")
    data = {"subscriptionNotification": {"notificationType": 2, "purchaseToken": "token-abcdefgh"}}
    body = {"message": {"data": base64.b64encode(json.dumps(data).encode()).decode(), "messageId": "1"}}
    assert billing.parse_play_notification(body) == "token-abcdefgh"
    await billing.handle_play_notification(body)
    assert (await pg.get_account(acc.id)).units == 300  # renewed: last month's units expired


async def test_play_rejects_purchase_for_another_account(pg, play):
    acc, _ = await pg.google_account("g-mismatch", None, None)
    play["data"] = play_data(acc.id + 999)
    with pytest.raises(billing.BillingError):
        await billing.play_sync(acc.id, "token-zzzzzzzz")
