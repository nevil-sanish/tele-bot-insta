import asyncpg
import pytest

from app import config
from services.models import Reel


async def _units(pg, account_id):
    return (await pg.get_account(account_id)).units


async def test_new_identity_gets_free_units_once(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 20)
    a, created = await pg.ig_account("ig-1")
    b, created_again = await pg.ig_account("ig-1")
    assert created and not created_again and a.id == b.id
    assert b.units == 20 and b.reels_left() == 2 and not b.unlimited
    ledger = await pg.pool().fetch("SELECT delta, kind FROM ledger WHERE account_id = $1", a.id)
    assert [(r["delta"], r["kind"]) for r in ledger] == [(20, "grant")]


async def test_admins_are_unlimited(pg):
    admin, _ = await pg.ig_account("admin-1")
    assert admin.unlimited and admin.reels_left() is None


async def test_google_account_keeps_latest_name(pg):
    a, _ = await pg.google_account("g-1", "a@b.com", "A")
    b, _ = await pg.google_account("g-1", "a@b.com", "A B")
    assert a.id == b.id and b.name == "A B" and b.email == "a@b.com"


async def test_create_job_reserves_units_and_refuses_when_short(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 15)
    acc, _ = await pg.ig_account("ig-2")
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, shortcode="AAA", recipient="ig-2")
    assert job.reserved == 10 and await _units(pg, acc.id) == 5
    assert await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, shortcode="BBB") is None
    assert await _units(pg, acc.id) == 5  # nothing held for the refused job
    assert await pg.pool().fetchval("SELECT count(*) FROM jobs") == 1
    row = await pg.pool().fetchrow("SELECT delta, job_id FROM ledger WHERE kind = 'reserve'")
    assert (row["delta"], row["job_id"]) == (-10, job.id)


async def test_failure_refunds_once_unless_told_not_to(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 20)
    acc, _ = await pg.ig_account("ig-3")
    a = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, shortcode="A")
    b = await pg.create_job(acc.id, "reel", "deliver", "ig", units=10, shortcode="B")
    assert await _units(pg, acc.id) == 0
    await pg.fail_job(a.id, "fetch: nope")
    await pg.fail_job(a.id, "again")  # double failure must not refund twice
    await pg.fail_job(b.id, "deliver: window closed", refund=False)
    assert await _units(pg, acc.id) == 10
    assert (await pg.get_job(a.id)).reserved == 0


async def test_unlimited_plan_holds_nothing(pg):
    admin, _ = await pg.ig_account("admin-1")
    job = await pg.create_job(admin.id, "reel", "fetch", "ig", units=10, shortcode="X")
    assert job.reserved == 0


async def test_stages_advance_and_payload_merges(pg):
    acc, _ = await pg.ig_account("ig-4")
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=0, shortcode="S1", payload={"a": 1})
    claimed = await pg.claim_job()
    assert claimed.id == job.id and claimed.attempts == 1
    await pg.advance(claimed, "analyze", media=[["u", "video/mp4"]])
    again = await pg.claim_job()
    assert again.stage == "analyze" and again.attempts == 1 and again.payload == {"a": 1, "media": [["u", "video/mp4"]]}
    await pg.save_analysis(again, Reel(shortcode="S1", title="T"))
    delivered = await pg.claim_job()
    assert delivered.stage == "deliver" and await pg.in_library(acc.id, "S1")
    await pg.finish_job(delivered.id, reply="x")
    done = await pg.get_job(job.id)
    assert done.status == "done" and done.payload["reply"] == "x"


async def test_fetch_jobs_can_be_claimed_in_a_batch(pg):
    acc, _ = await pg.ig_account("ig-5")
    for code in ("F1", "F2", "F3"):
        await pg.create_job(acc.id, "reel", "fetch", "ig", units=0, shortcode=code)
    await pg.create_job(acc.id, "chat", "chat", "ig", units=0)
    batch = await pg.claim_jobs("fetch", 10)
    assert [j.shortcode for j in batch] == ["F1", "F2", "F3"]
    assert (await pg.claim_job()).stage == "chat"


async def test_same_reel_is_never_analyzed_twice_at_once(pg):
    acc, _ = await pg.ig_account("ig-6")
    first = await pg.create_job(acc.id, "reel", "analyze", "ig", units=0, shortcode="SAME")
    second = await pg.create_job(acc.id, "reel", "analyze", "ig", units=0, shortcode="SAME")
    other = await pg.create_job(acc.id, "reel", "analyze", "ig", units=0, shortcode="OTHER")
    assert (await pg.claim_job()).id == first.id
    assert (await pg.claim_job()).id == other.id  # SAME is busy, so it's skipped
    assert await pg.claim_job() is None
    await pg.finish_job(first.id)
    assert (await pg.claim_job()).id == second.id
    with pytest.raises(asyncpg.UniqueViolationError):  # what a racing second machine would hit
        await pg.pool().execute("UPDATE jobs SET status = 'running' WHERE id = $1", first.id)


async def test_stale_jobs_are_taken_over_then_given_up_with_refund(pg, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(config, "FREE_UNITS", 10)
    acc, _ = await pg.ig_account("ig-7")
    job = await pg.create_job(acc.id, "reel", "analyze", "ig", units=10, shortcode="CRASH")
    stale = "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = $1"
    assert (await pg.claim_job()).attempts == 1
    assert await pg.claim_job() is None  # heartbeat still fresh
    await pg.pool().execute(stale, job.id)
    assert (await pg.claim_job()).attempts == 2  # process died: taken over
    await pg.pool().execute(stale, job.id)
    assert await pg.claim_job() is None  # out of attempts
    assert (await pg.get_job(job.id)).status == "failed"
    assert await _units(pg, acc.id) == 10


async def test_requeue_does_not_count_the_attempt(pg):
    acc, _ = await pg.ig_account("ig-8")
    job = await pg.create_job(acc.id, "chat", "chat", "ig", units=0)
    await pg.claim_job()
    await pg.requeue(job.id)
    j = await pg.get_job(job.id)
    assert j.status == "queued" and j.attempts == 0


async def test_higher_priority_jobs_run_first(pg):
    acc, _ = await pg.ig_account("ig-9")
    low = await pg.create_job(acc.id, "chat", "chat", "ig", units=0, priority=0)
    high = await pg.create_job(acc.id, "chat", "chat", "ig", units=0, priority=10)
    assert (await pg.claim_job()).id == high.id
    assert (await pg.claim_job()).id == low.id


async def test_reel_round_trip_and_server_beats_client(pg):
    reel = Reel(
        shortcode="RRR", link="https://www.instagram.com/reel/RRR/", title="T", summary="S",
        key_ideas=["a", "b"], tools=["Python"], tags=["x"], hashtags=["y"], urls=["https://z.io"],
        transcript="t" * 5000, date="2026-10-18T06:00:00+00:00", analysis="audio-only", model="m",
    )
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, reel)
        await pg.save_reel(con, Reel(shortcode="RRR", title="phone"), origin="client")  # ignored
    assert await pg.get_reel("RRR") == reel
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="CLI", title="phone"), origin="client")
        await pg.save_reel(con, Reel(shortcode="CLI", title="server"))  # replaces the phone result
    assert (await pg.get_reel("CLI")).title == "server"


async def test_conversation_state(pg):
    acc, _ = await pg.ig_account("ig-10")
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="one", title="1"))
    assert (await pg.get_conversation(acc.id, "ig")).active is None
    await pg.set_active_reel(acc.id, "ig", "one")
    await pg.save_history(acc.id, "ig", [{"role": "user", "content": "q"}])
    conv = await pg.get_conversation(acc.id, "ig")
    assert conv.active.shortcode == "one" and conv.history == [{"role": "user", "content": "q"}]
    await pg.set_pending(acc.id, "ig", "PEND")
    assert await pg.pop_pending(acc.id, "ig") == "PEND"
    assert await pg.pop_pending(acc.id, "ig") is None
    await pg.reset_conversation(acc.id, "ig")
    assert (await pg.get_conversation(acc.id, "ig")).active is None


async def test_linking_merges_accounts_with_one_free_trial(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 20)
    app_acc, _ = await pg.google_account("g-9", "n@x.com", "Nevil")
    ig_acc, _ = await pg.ig_account("ig-11")
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="MINE", title="m"))
    await pg.add_to_library(ig_acc.id, "MINE")
    await pg.grant(ig_acc.id, 100, "purchase", "test", idem_key="pay-1")

    code = await pg.new_link_code(app_acc.id)
    assert await pg.link_with_code("000000" if code != "000000" else "111111", ig_acc.id) is None
    merged = await pg.link_with_code(code, ig_acc.id)
    assert merged.id == app_acc.id
    assert merged.units == 20 + 100  # the second free trial is not added
    assert await pg.in_library(app_acc.id, "MINE")
    assert {i["kind"] for i in await pg.identities(app_acc.id)} == {"google", "ig"}
    assert await pg.get_account(ig_acc.id) is None
    again, _ = await pg.ig_account("ig-11")  # the Instagram identity now resolves to the app account
    assert again.id == app_acc.id
    assert await pg.link_with_code(code, ig_acc.id) is None  # codes are single use


async def test_grants_are_idempotent(pg):
    acc, _ = await pg.ig_account("ig-12")
    before = acc.units
    assert await pg.grant(acc.id, 50, "purchase", "plan", idem_key="evt-1")
    assert not await pg.grant(acc.id, 50, "purchase", "plan", idem_key="evt-1")  # webhook replay
    assert await _units(pg, acc.id) == before + 50


async def test_first_delivery_dedupes(pg):
    assert await pg.first_delivery("ig", "m1", {"x": 1})
    assert not await pg.first_delivery("ig", "m1", {"x": 1})


async def test_usage_is_counted_per_key_and_day(pg):
    await pg.record_usage("gemini", "abc", "m", True, 100, 20)
    await pg.record_usage("gemini", "abc", "m", False, 0, 0)
    row = await pg.pool().fetchrow("SELECT * FROM api_usage WHERE key_id = 'abc'")
    assert (row["requests"], row["errors"], row["input_tokens"], row["output_tokens"]) == (2, 1, 100, 20)
