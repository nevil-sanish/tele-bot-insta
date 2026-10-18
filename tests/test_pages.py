import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import FastAPI

from app import config, pages


@pytest.fixture
async def client(pg):
    app = FastAPI()
    app.include_router(pages.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def signed_request(payload: dict, secret: str = "app-s3cret") -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{sig}.{body}"


async def test_public_pages(client, monkeypatch):
    monkeypatch.setattr(config, "BOT_USERNAME", "@reelbrain.ai")
    for path in ("/", "/privacy", "/terms", "/data-deletion"):
        r = await client.get(path)
        assert r.status_code == 200 and "<html" in r.text
    privacy = (await client.get("/privacy")).text
    assert "Apify" in privacy and "Razorpay" in privacy and "ig.me/m/reelbrain.ai" in privacy


async def test_meta_deletion_callback(client, pg):
    acc, _ = await pg.ig_account("ig-del")
    bad = await client.post("/meta/data-deletion", data={"signed_request": signed_request({"user_id": "ig-del"}, "nope")})
    assert bad.status_code == 400 and await pg.get_account(acc.id)
    r = await client.post("/meta/data-deletion", data={"signed_request": signed_request({"user_id": "ig-del"})})
    body = r.json()
    assert r.status_code == 200 and body["confirmation_code"] and "/data-deletion?code=" in body["url"]
    assert await pg.get_account(acc.id) is None
    status = await client.get(f"/data-deletion?code={body['confirmation_code']}")
    assert "completed" in status.text


async def test_admin_stats_needs_token(client, pg, monkeypatch):
    assert (await client.get("/admin/stats")).status_code == 401
    monkeypatch.setattr(config, "ADMIN_TOKEN", "t0ken")
    assert (await client.get("/admin/stats", headers={"Authorization": "Bearer wrong"})).status_code == 401
    await pg.ig_account("someone")
    stats = (await client.get("/admin/stats", headers={"Authorization": "Bearer t0ken"})).json()
    assert stats["accounts"] == 1 and stats["accounts_by_plan"] == {"free": 1} and stats["queue"] == 0
