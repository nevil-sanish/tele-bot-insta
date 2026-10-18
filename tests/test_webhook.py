import json

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.test_instagram import sign, webhook


@pytest.fixture
def client():
    return TestClient(main.app)  # no `with`: lifespan (DB, workers) not run


def test_verification_handshake(client):
    ok = client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_unsigned_posts_are_rejected(client):
    body = json.dumps(webhook()).encode()
    assert client.post("/webhooks/instagram", content=body).status_code == 403
    r = client.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sign(body, "wrong")})
    assert r.status_code == 403


def test_messages_are_deduped_and_handled_after_200(client, monkeypatch):
    seen_mids, handled = set(), []

    async def first_delivery(platform, mid, payload):
        new = mid not in seen_mids
        seen_mids.add(mid)
        return new

    async def handle(msg, out):
        handled.append(msg.mid)

    class FakeWorker:
        def wake(self):
            pass

    monkeypatch.setattr(main.db, "first_delivery", first_delivery)
    monkeypatch.setattr(main.inbound, "handle", handle)
    main.app.state.out = None
    main.app.state.worker = FakeWorker()
    event = {"sender": {"id": "u1"}, "recipient": {"id": "BOT"}, "timestamp": 1, "message": {"mid": "m1", "text": "hi"}}
    body = json.dumps(webhook(event)).encode()
    for _ in range(2):  # Meta retries the same delivery
        r = client.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sign(body)})
        assert r.status_code == 200
    assert handled == ["m1"]


def test_razorpay_webhook_checks_signature_and_dedupes(client, monkeypatch):
    import hashlib
    import hmac as _hmac

    from app import config

    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    seen, handled = set(), []

    async def first(provider, event_id, payload):
        new = event_id not in seen
        seen.add(event_id)
        return new

    async def handle(event):
        handled.append(event["event"])
        return None, None

    monkeypatch.setattr(main.db, "first_payment_event", first)
    monkeypatch.setattr(main.billing, "handle_razorpay", handle)
    body = json.dumps({"event": "subscription.activated", "payload": {}}).encode()
    sig = _hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": "bad"}).status_code == 403
    for _ in range(2):
        r = client.post("/webhooks/razorpay", content=body,
                        headers={"X-Razorpay-Signature": sig, "x-razorpay-event-id": "evt_1"})
        assert r.status_code == 200
    assert handled == ["subscription.activated"]


def test_play_webhook_needs_token(client):
    assert client.post("/v1/webhooks/play?token=nope", json={}).status_code == 403
