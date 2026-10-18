from app import config, inbound
from channels.instagram import Attachment, Inbound
from services.models import Reel


async def _route(pg, text="", attachments=None, quick_reply=None, sender="u1", mid="m1"):
    account, created = await pg.ig_account(sender)
    return await inbound.route(Inbound(sender, mid, text, attachments or [], quick_reply), account, created), account


async def _jobs(pg):
    return await pg.pool().fetch("SELECT kind, stage, shortcode, payload, reserved FROM jobs ORDER BY id")


async def test_first_message_welcomes_with_free_reels(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 20)
    reply, _ = await _route(pg, "hey there")
    assert "2 free reels" in reply and await _jobs(pg) == []


async def test_commands(pg):
    await _route(pg, "hello")  # account exists now
    assert (await _route(pg, "help"))[0] == inbound.HELP
    assert "reels left" in (await _route(pg, "Credits!"))[0]
    assert "Fresh start" in (await _route(pg, "", quick_reply="NEW"))[0]
    assert "type your question" in (await _route(pg, "Ask a question", quick_reply="ASK"))[0]
    assert "wrong or expired" in (await _route(pg, "link 123456"))[0]
    assert await _jobs(pg) == []


async def test_question_starting_with_hi_is_still_a_question(pg):
    await _route(pg, "hello")
    reply, _ = await _route(pg, "hi what tools did it use?")
    assert reply is None
    jobs = await _jobs(pg)
    assert jobs[0]["kind"] == "chat" and jobs[0]["payload"]["question"] == "hi what tools did it use?"
    assert jobs[0]["reserved"] == config.UNITS_PER_QUESTION


async def test_shared_reel_goes_straight_to_analysis(pg):
    await _route(pg, "hello")
    att = Attachment("ig_reel", "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1",
                     {"url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1", "title": "My caption", "reel_video_id": "987"})
    reply, _ = await _route(pg, attachments=[att], mid="m2")
    assert reply == inbound.QUEUED
    job = (await _jobs(pg))[0]
    assert job["stage"] == "analyze" and job["shortcode"] == "igv_987"
    assert job["payload"]["caption"] == "My caption" and job["payload"]["source"] == "meta"
    assert job["reserved"] == config.UNITS_PER_REEL


async def test_link_goes_to_fetch_and_cached_reel_to_deliver(pg):
    await _route(pg, "hello")
    reply, _ = await _route(pg, "look https://www.instagram.com/reel/ABC123/?igsh=x")
    assert reply == inbound.QUEUED
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="OLD1", title="t"))
    reply, _ = await _route(pg, "https://www.instagram.com/reel/OLD1/")
    assert reply is None  # the summary itself is the reply
    assert [(j["stage"], j["shortcode"]) for j in await _jobs(pg)] == [("fetch", "ABC123"), ("deliver", "OLD1")]


async def test_video_after_failed_fetch_keeps_the_reel_identity(pg):
    _, account = await _route(pg, "hello")
    await pg.set_pending(account.id, "ig", "LOST1")
    reply, _ = await _route(pg, attachments=[Attachment("video", "https://cdn/v.mp4")], mid="m9")
    assert reply == inbound.QUEUED
    job = (await _jobs(pg))[0]
    assert job["shortcode"] == "LOST1" and job["payload"]["source"] == "upload"
    reply, _ = await _route(pg, attachments=[Attachment("video", "https://cdn/w.mp4")], mid="m10")
    assert (await _jobs(pg))[1]["shortcode"].startswith("igu_")


async def test_out_of_credits_gets_paywall(pg, monkeypatch):
    monkeypatch.setattr(config, "FREE_UNITS", 10)
    await _route(pg, "hello")
    assert (await _route(pg, "https://www.instagram.com/reel/P1/"))[0] == inbound.QUEUED
    reply, _ = await _route(pg, "https://www.instagram.com/reel/P2/")
    assert "out of credits" in reply
    assert "out of credits" in (await _route(pg, "a question"))[0]


async def test_too_many_pending(pg, monkeypatch):
    monkeypatch.setattr(config, "MAX_PENDING_JOBS", 1)
    await _route(pg, "hello", sender="admin-1")
    assert (await _route(pg, "https://www.instagram.com/reel/Q1/", sender="admin-1"))[0] == inbound.QUEUED
    assert (await _route(pg, "https://www.instagram.com/reel/Q2/", sender="admin-1"))[0] == inbound.BUSY


async def test_linking_from_the_dm(pg):
    app_acc, _ = await pg.google_account("g-1", "n@x.com", "N")
    code = await pg.new_link_code(app_acc.id)
    await _route(pg, "hello")
    reply, _ = await _route(pg, f"link {code}")
    assert reply.startswith("✅ Linked")
    again, _ = await pg.ig_account("u1")
    assert again.id == app_acc.id


def test_stand_in_keys_are_stable():
    assert inbound.stand_in_key("igu", "m1") == inbound.stand_in_key("igu", "m1")
    assert inbound.stand_in_key("igu", "m1") != inbound.stand_in_key("igu", "m2")
    shortcode, url = inbound.reel_from_attachment(Attachment("share", "https://www.instagram.com/reel/XYZ/"))
    assert (shortcode, url) == ("XYZ", None)
