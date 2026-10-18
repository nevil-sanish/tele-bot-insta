import httpx
import pytest
from fastapi import FastAPI

from app import api, config, worker
from services.models import Reel

URL = "https://www.instagram.com/reel/APIREEL/?igsh=x"


@pytest.fixture
async def client(pg, monkeypatch):
    monkeypatch.setattr(config, "APP_SECRET", "test-secret-that-is-long-enough-for-hs256")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_IDS", ["android-client"])
    monkeypatch.setattr(config, "FREE_CREDITS", 2)
    monkeypatch.setattr(
        api, "verify_google_token",
        lambda token: {"sub": f"sub-{token}", "email": f"{token}@x.com", "name": token, "aud": "android-client"},
    )
    app = FastAPI()
    app.include_router(api.router)
    app.state.worker = worker.Worker(None)  # not started: jobs just wait in the queue
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _login(client, name="nevil"):
    r = await client.post("/api/auth/google", json={"id_token": name})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()


async def test_login_creates_account_with_free_credits(client):
    headers, body = await _login(client)
    assert body["new_account"] and body["user"]["credits"] == 2
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["email"] == "nevil@x.com"
    _, again = await _login(client)
    assert not again["new_account"]


async def test_auth_required(client):
    assert (await client.get("/api/me")).status_code == 401
    assert (await client.get("/api/me", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_new_reel_is_queued_then_reported(client, pg):
    headers, _ = await _login(client)
    assert (await client.post("/api/reels", json={"url": "hello"}, headers=headers)).status_code == 400

    r = (await client.post("/api/reels", json={"url": URL}, headers=headers)).json()
    assert r["status"] == "queued"
    status = (await client.get(f"/api/jobs/{r['job_id']}", headers=headers)).json()
    assert status["status"] == "queued" and status["shortcode"] == "APIREEL"

    job = await pg.claim_job()
    await pg.finish_job(job, Reel(shortcode="APIREEL", title="Done"), analyzed=True)
    status = (await client.get(f"/api/jobs/{r['job_id']}", headers=headers)).json()
    assert status["status"] == "done" and status["reel"]["title"] == "Done"
    assert (await client.get("/api/me", headers=headers)).json()["credits"] == 1
    lib = (await client.get("/api/library", headers=headers)).json()["reels"]
    assert [x["shortcode"] for x in lib] == ["APIREEL"]
    assert (await client.get("/api/reels/APIREEL", headers=headers)).json()["title"] == "Done"


async def test_cached_reel_costs_nothing(client, pg):
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="APIREEL", title="Saved"))
    headers, _ = await _login(client)
    r = (await client.post("/api/reels", json={"url": URL}, headers=headers)).json()
    assert r["status"] == "done" and r["credits_used"] == 0 and r["reel"]["title"] == "Saved"
    assert (await client.get("/api/me", headers=headers)).json()["credits"] == 2


async def test_out_of_credits(client):
    headers, _ = await _login(client)
    for code in ("A1", "A2"):
        r = await client.post("/api/reels", json={"url": f"https://www.instagram.com/reel/{code}/"}, headers=headers)
        assert r.json()["status"] == "queued"
    # 2 credits, 2 analyses pending: a third would overspend.
    r = await client.post("/api/reels", json={"url": "https://www.instagram.com/reel/A3/"}, headers=headers)
    assert r.status_code == 402


async def test_other_users_cannot_see_my_jobs(client):
    mine, _ = await _login(client, "me")
    theirs, _ = await _login(client, "them")
    job_id = (await client.post("/api/reels", json={"url": URL}, headers=mine)).json()["job_id"]
    assert (await client.get(f"/api/jobs/{job_id}", headers=theirs)).status_code == 404
    assert (await client.get("/api/reels/APIREEL", headers=theirs)).status_code == 404
