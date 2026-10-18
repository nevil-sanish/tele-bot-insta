from app import config
from services.models import Reel


async def test_telegram_users_are_unlimited_and_idempotent(pg):
    a = await pg.telegram_user(111, "Nevil")
    b = await pg.telegram_user(111, None)
    assert a.id == b.id and a.unlimited and b.name == "Nevil"


async def test_google_signup_gets_free_credits_once(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_CREDITS", 10)
    user, created = await pg.google_user("g-1", "a@b.com", "A")
    again, created_again = await pg.google_user("g-1", "a@b.com", "A B")
    assert created and not created_again and user.id == again.id
    assert again.credits == 10 and again.name == "A B" and not again.unlimited
    ledger = await pg.pool().fetch("SELECT delta, reason FROM credit_ledger WHERE user_id = $1", user.id)
    assert [(r["delta"], r["reason"]) for r in ledger] == [(10, "signup bonus")]


async def test_finish_charges_credit_plans_only(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_CREDITS", 2)
    app_user, _ = await pg.google_user("g-2", None, None)
    tg_user = await pg.telegram_user(222)

    job = await pg.enqueue(app_user.id, "AAA", url="https://www.instagram.com/reel/AAA/")
    assert await pg.finish_job(job, Reel(shortcode="AAA", title="t"), analyzed=True)
    job2 = await pg.enqueue(tg_user.id, "BBB", url="https://www.instagram.com/reel/BBB/")
    assert not await pg.finish_job(job2, Reel(shortcode="BBB", title="t"), analyzed=True)

    assert (await pg.get_user(app_user.id)).credits == 1
    assert (await pg.get_user(tg_user.id)).credits == 0
    assert [r.shortcode for r in await pg.library(app_user.id)] == ["AAA"]
    assert (await pg.get_job(job.id)).status == "done"


async def test_cached_reel_is_free(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_CREDITS", 1)
    user, _ = await pg.google_user("g-3", None, None)
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="CCC", title="saved"))
    job = await pg.enqueue(user.id, "CCC", url="u")
    assert not await pg.finish_job(job, await pg.get_reel("CCC"), analyzed=False)
    assert (await pg.get_user(user.id)).credits == 1


async def test_reel_round_trip(pg):
    reel = Reel(
        shortcode="RRR", link="https://www.instagram.com/reel/RRR/", title="T", summary="S",
        key_ideas=["a", "b"], tools=["Python"], tags=["x"], hashtags=["y"], urls=["https://z.io"],
        transcript="t" * 5000, date="2026-10-18T06:00:00+00:00", analysis="audio-only", model="m",
    )
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, reel)
    assert await pg.get_reel("RRR") == reel


async def test_same_reel_is_never_analyzed_twice_at_once(pg):
    user = await pg.telegram_user(333)
    first = await pg.enqueue(user.id, "SAME", url="u")
    second = await pg.enqueue(user.id, "SAME", url="u")
    other = await pg.enqueue(user.id, "OTHER", url="u")

    assert (await pg.claim_job()).id == first.id
    assert (await pg.claim_job()).id == other.id  # SAME is busy, so it's skipped
    assert await pg.claim_job() is None
    await pg.finish_job(first, Reel(shortcode="SAME"), analyzed=True)
    assert (await pg.claim_job()).id == second.id


async def test_stale_jobs_are_taken_over_then_given_up(pg, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    user = await pg.telegram_user(444)
    job = await pg.enqueue(user.id, "CRASH", url="u")
    stale = "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = $1"

    assert (await pg.claim_job()).attempts == 1
    assert await pg.claim_job() is None  # heartbeat still fresh
    await pg.pool().execute(stale, job.id)
    assert (await pg.claim_job()).attempts == 2  # process died: taken over
    await pg.pool().execute(stale, job.id)
    assert await pg.claim_job() is None  # out of attempts
    assert (await pg.get_job(job.id)).status == "failed"


async def test_requeue_does_not_count_the_attempt(pg):
    user = await pg.telegram_user(555)
    job = await pg.enqueue(user.id, "RQ", url="u")
    await pg.claim_job()
    await pg.requeue(job.id)
    j = await pg.get_job(job.id)
    assert j.status == "queued" and j.attempts == 0


async def test_failed_jobs_and_retry(pg):
    user = await pg.telegram_user(666)
    a = await pg.enqueue(user.id, "F1", url="u1")
    b = await pg.enqueue(user.id, "F2", url="u2")
    await pg.fail_job(a.id, "fetch: blocked")
    await pg.fail_job(b.id, "analysis: quota")
    await pg.finish_job(await pg.enqueue(user.id, "F2", url="u2"), Reel(shortcode="F2"), analyzed=True)

    failed = await pg.failed_jobs(user.id)
    assert [j.shortcode for j in failed] == ["F1"]  # F2 succeeded later
    await pg.retry_job(a.id, chat_id=9, status_message_id=10)
    again = await pg.get_job(a.id)
    assert again.status == "queued" and again.chat_id == 9 and again.error is None
    assert await pg.failed_jobs(user.id) == []


async def test_higher_priority_jobs_run_first(pg):
    user = await pg.telegram_user(777)
    low = await pg.enqueue(user.id, "LOW", url="u", priority=0)
    high = await pg.enqueue(user.id, "HIGH", url="u", priority=10)
    assert (await pg.claim_job()).id == high.id
    assert (await pg.claim_job()).id == low.id


async def test_database_blocks_two_running_jobs_for_one_reel(pg):
    import asyncpg
    import pytest

    user = await pg.telegram_user(888)
    a = await pg.enqueue(user.id, "DUP", url="u")
    b = await pg.enqueue(user.id, "DUP", url="u")
    await pg.claim_job()
    with pytest.raises(asyncpg.UniqueViolationError):  # what a racing second machine would hit
        await pg.pool().execute("UPDATE jobs SET status = 'running' WHERE id = $1", b.id)
    assert (await pg.get_job(a.id)).status == "running"


async def test_usage_is_counted_per_key_and_day(pg):
    await pg.record_usage("gemini", "abc", "m", True, 100, 20)
    await pg.record_usage("gemini", "abc", "m", False, 0, 0)
    row = await pg.pool().fetchrow("SELECT * FROM api_usage WHERE key_id = 'abc'")
    assert (row["requests"], row["errors"], row["input_tokens"], row["output_tokens"]) == (2, 1, 100, 20)
