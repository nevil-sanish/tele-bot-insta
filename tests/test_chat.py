import json

from services import chat
from services.models import Reel


async def _save(pg, *reels):
    async with pg.pool().acquire() as con:
        for r in reels:
            await pg.save_reel(con, r)


async def test_history_is_capped_and_reset(pg, monkeypatch):
    calls = []

    async def fake_complete(messages, max_tokens=700):
        calls.append(messages)
        return "answer"

    monkeypatch.setattr(chat, "complete", fake_complete)
    reel = Reel(shortcode="a", title="T", summary="S")
    await _save(pg, reel)
    await chat.set_active(1, reel)
    for i in range(10):
        await chat.ask_active(1, f"q{i}")
    # system + 4 turns (8 messages) + new question
    assert len(calls[-1]) == 1 + 8 + 1
    assert calls[-1][1]["content"] == "q5"
    await chat.reset(1)
    assert "No active reel" in await chat.ask_active(1, "hi")


async def test_new_active_reel_clears_history(pg, monkeypatch):
    async def fake_complete(messages, max_tokens=700):
        return "a"

    monkeypatch.setattr(chat, "complete", fake_complete)
    one, two = Reel(shortcode="one", title="1"), Reel(shortcode="two", title="2")
    await _save(pg, one, two)
    await chat.set_active(5, one)
    await chat.ask_active(5, "q")
    await chat.set_active(5, two)
    active, history, _ = await pg.get_chat(5)
    assert active.shortcode == "two" and history == []


async def test_pending_link_is_taken_once(pg):
    await chat.set_pending(3, "https://www.instagram.com/reel/X/")
    assert await chat.pop_pending(3) == "https://www.instagram.com/reel/X/"
    assert await chat.pop_pending(3) is None
    assert await chat.pop_pending(999) is None


def test_reel_context_respects_budget():
    reel = Reel(shortcode="a", title="T", summary="S", transcript="w " * 10000, caption="c " * 5000)
    ctx = chat.reel_context(reel, 3000)
    assert len(ctx) <= 3000
    assert json.loads(ctx)["title"] == "T"


async def test_ask_library_searches_only_my_reels(pg, monkeypatch):
    me = await pg.telegram_user(1)
    other = await pg.telegram_user(2)
    await _save(
        pg,
        Reel(shortcode="py", title="Python tips", summary="Use list comprehensions", tags=["python"]),
        Reel(shortcode="leg", title="Leg day", summary="Squats and lunges", tags=["fitness"]),
        Reel(shortcode="theirs", title="Python secrets", summary="decorators"),
    )
    for code in ("py", "leg"):
        await pg.add_to_library(me.id, code)
    await pg.add_to_library(other.id, "theirs")
    seen = {}

    async def fake_complete(messages, max_tokens=700):
        seen["prompt"] = messages[-1]["content"]
        return "Use comprehensions [1]"

    monkeypatch.setattr(chat, "complete", fake_complete)
    answer, hits = await chat.ask_library("python comprehension tips", me.id)
    assert [h.shortcode for h in hits] == ["py"]  # stemming matches "comprehension"; not other users' reels
    assert "[1] Python tips" in seen["prompt"]
    assert [h.shortcode for h in await pg.search_library(me.id, "squat", 3)] == ["leg"]
    assert await pg.search_library(me.id, "quantum", 3) == []
    assert await pg.search_library(me.id, "the of", 3) == []  # stopwords only
