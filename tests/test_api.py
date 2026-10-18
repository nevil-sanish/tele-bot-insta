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


RESULT = {"title": "Phone title", "summary": "S", "key_ideas": ["a"], "tags": ["Python"], "caption": "Hi #tips https://x.io",
          "author": "bob", "model": "gemini-3.5-flash"}


async def test_own_key_upload_is_shared_but_server_wins(client, pg):
    headers, _ = await _login(client)
    r = await client.post("/v1/reels/OWNKEY1/result", json=RESULT, headers=headers)
    assert r.status_code == 200 and r.json()["title"] == "Phone title" and r.json()["source"] == "phone"
    assert r.json()["tags"] == ["python", "tips"] and r.json()["urls"] == ["https://x.io"]
    assert (await client.get("/v1/me", headers=headers)).json()["units"] == 20  # free
    assert await pg.in_library((await client.get("/v1/me", headers=headers)).json()["id"], "OWNKEY1")

    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="SERVER1", title="Server title"))
    r = await client.post("/v1/reels/SERVER1/result", json=RESULT, headers=headers)
    assert r.json()["title"] == "Server title"  # the server's analysis is kept and returned

    assert (await client.post("/v1/reels/bad!code/result", json=RESULT, headers=headers)).status_code in (400, 404)
    bad = {**RESULT, "author": "x<script>"}
    assert (await client.post("/v1/reels/OWNKEY2/result", json=bad, headers=headers)).status_code == 422
    long = {**RESULT, "key_ideas": ["k"] * 50}
    assert (await client.post("/v1/reels/OWNKEY2/result", json=long, headers=headers)).status_code == 422


async def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config, "CLIENT_UPLOADS_PER_DAY", 1)
    headers, _ = await _login(client)
    assert (await client.post("/v1/reels/LIMIT1/result", json=RESULT, headers=headers)).status_code == 200
    assert (await client.post("/v1/reels/LIMIT2/result", json=RESULT, headers=headers)).status_code == 429


async def test_reports_hide_and_requeue(client, pg, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_TO_HIDE", 2)
    people = [await _login(client, f"r{i}") for i in range(2)]
    for headers, _ in people:
        await client.post("/v1/reels/WRONG1/result", json=RESULT, headers=headers)
    first = (await client.post("/v1/reels/WRONG1/report", json={"reason": "wrong"}, headers=people[0][0])).json()
    again = (await client.post("/v1/reels/WRONG1/report", json={}, headers=people[0][0])).json()
    assert first == {"reported": True, "reanalyzing": False} and again["reanalyzing"] is False  # once per person
    second = (await client.post("/v1/reels/WRONG1/report", json={}, headers=people[1][0])).json()
    assert second["reanalyzing"] is True
    assert await pg.get_reel("WRONG1") is None  # hidden
    job = await pg.claim_job()
    assert (job.stage, job.shortcode, job.reserved) == ("fetch", "WRONG1", 0)


async def test_server_fetch_for_own_key_users(client, pg):
    headers, body = await _login(client)
    r = (await client.post("/v1/fetch", json={"url": URL}, headers=headers)).json()
    assert (await client.get("/v1/me", headers=headers)).json()["units"] == 20 - config.UNITS_PER_FETCH
    job = await pg.claim_job()
    assert job.kind == "fetch"
    await pg.finish_job(job.id, media=[["https://cdn/v.mp4", "video/mp4"]], caption="c", author="bob", date=None)
    status = (await client.get(f"/v1/jobs/{r['job_id']}", headers=headers)).json()
    assert status["media"] == [{"url": "https://cdn/v.mp4", "mime": "video/mp4"}] and status["author"] == "bob"


async def test_plans_and_play_not_configured(client):
    plans = (await client.get("/v1/plans")).json()
    assert [p["key"] for p in plans["plans"]] == ["basic", "pro"] and plans["plans"][0]["play_product_id"]
    headers, _ = await _login(client)
    r = await client.post("/v1/billing/play", json={"purchase_token": "x" * 20}, headers=headers)
    assert r.status_code == 503


async def test_delete_account(client, pg):
    headers, body = await _login(client)
    assert (await client.delete("/v1/me", headers=headers)).json() == {"deleted": True}
    assert (await client.get("/v1/me", headers=headers)).status_code == 401
    assert await pg.get_account(body["account"]["id"]) is None


async def test_question_about_a_specific_reel(client, pg):
    headers, body = await _login(client)
    await client.post("/v1/reels/ABOUT1/result", json=RESULT, headers=headers)
    assert (await client.post("/v1/chat", json={"question": "q", "shortcode": "NOPE1"}, headers=headers)).status_code == 404
    await client.post("/v1/chat", json={"question": "q", "shortcode": "ABOUT1"}, headers=headers)
    conv = await pg.get_conversation(body["account"]["id"], "app")
    assert conv.active.shortcode == "ABOUT1"
