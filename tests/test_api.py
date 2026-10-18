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
    monkeypatch.setattr(config, "FREE_UNITS", 20)
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
    r = await client.post("/v1/auth/google", json={"id_token": name})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()


async def test_login_creates_account_with_free_reels(client):
    headers, body = await _login(client)
    assert body["new_account"] and body["account"]["reels_left"] == 2
    me = (await client.get("/v1/me", headers=headers)).json()
    assert me["email"] == "nevil@x.com" and me["linked"] == ["google"]
    _, again = await _login(client)
    assert not again["new_account"]


async def test_auth_required(client):
    assert (await client.get("/v1/me")).status_code == 401
    assert (await client.get("/v1/me", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_reel_job_lifecycle(client, pg):
    headers, _ = await _login(client)
    assert (await client.post("/v1/jobs", json={"url": "hello"}, headers=headers)).status_code == 400

    r = (await client.post("/v1/jobs", json={"url": URL}, headers=headers)).json()
    status = (await client.get(f"/v1/jobs/{r['job_id']}", headers=headers)).json()
    assert status["status"] == "queued" and status["stage"] == "fetch" and status["shortcode"] == "APIREEL"
    assert (await client.get("/v1/me", headers=headers)).json()["units"] == 10  # held while it runs

    job = await pg.claim_job()
    await pg.save_analysis(job, Reel(shortcode="APIREEL", title="Done"))
    await pg.finish_job(job.id)
    status = (await client.get(f"/v1/jobs/{r['job_id']}", headers=headers)).json()
    assert status["status"] == "done" and status["reel"]["title"] == "Done"
    lib = (await client.get("/v1/library", headers=headers)).json()["reels"]
    assert [x["shortcode"] for x in lib] == ["APIREEL"]
    assert (await client.get("/v1/library?q=done", headers=headers)).json()["reels"][0]["title"] == "Done"
    assert (await client.get("/v1/reels/APIREEL", headers=headers)).json()["title"] == "Done"


async def test_cached_reel_costs_the_same_and_skips_fetch(client, pg):
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="APIREEL", title="Saved"))
    headers, _ = await _login(client)
    r = (await client.post("/v1/jobs", json={"url": URL}, headers=headers)).json()
    assert (await client.get(f"/v1/jobs/{r['job_id']}", headers=headers)).json()["stage"] == "deliver"
    assert (await client.get("/v1/me", headers=headers)).json()["units"] == 10


async def test_out_of_credits(client):
    headers, _ = await _login(client)
    for code in ("A1", "A2"):
        r = await client.post("/v1/jobs", json={"url": f"https://www.instagram.com/reel/{code}/"}, headers=headers)
        assert r.status_code == 200
    r = await client.post("/v1/jobs", json={"url": "https://www.instagram.com/reel/A3/"}, headers=headers)
    assert r.status_code == 402


async def test_questions_are_queued(client, pg):
    headers, _ = await _login(client)
    assert (await client.post("/v1/chat", json={"question": " "}, headers=headers)).status_code == 400
    r = (await client.post("/v1/chat", json={"question": "what tools?"}, headers=headers)).json()
    job = await pg.claim_job()
    assert job.id == r["job_id"] and job.payload == {"question": "what tools?"}
    await pg.finish_job(job.id, reply="Python")
    assert (await client.get(f"/v1/jobs/{r['job_id']}", headers=headers)).json()["reply"] == "Python"


async def test_link_code(client, pg):
    headers, body = await _login(client)
    code = (await client.post("/v1/link/code", headers=headers)).json()["code"]
    ig, _ = await pg.ig_account("ig-x")
    assert (await pg.link_with_code(code, ig.id)).id == body["account"]["id"]
    assert (await client.get("/v1/me", headers=headers)).json()["linked"] == ["google", "ig"]


async def test_other_people_cannot_see_my_jobs(client):
    mine, _ = await _login(client, "me")
    theirs, _ = await _login(client, "them")
    job_id = (await client.post("/v1/jobs", json={"url": URL}, headers=mine)).json()["job_id"]
    assert (await client.get(f"/v1/jobs/{job_id}", headers=theirs)).status_code == 404
    assert (await client.get("/v1/reels/APIREEL", headers=theirs)).status_code == 404
