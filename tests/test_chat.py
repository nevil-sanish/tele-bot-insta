import json

from services import chat
from services.models import Reel


async def _save(pg, *reels):
    async with pg.pool().acquire() as con:
        for r in reels:
            await pg.save_reel(con, r)


async def test_history_is_capped(pg, monkeypatch):
    calls = []

    async def fake_complete(messages, max_tokens=700):
        calls.append(messages)
        return "answer"

    monkeypatch.setattr(chat, "complete", fake_complete)
    acc, _ = await pg.ig_account("c1")
    await _save(pg, Reel(shortcode="a", title="T", summary="S"))
    await pg.set_active_reel(acc.id, "ig", "a")
    for i in range(10):
        await chat.answer(acc.id, "ig", f"q{i}")
    # system + 4 turns (8 messages) + new question
    assert len(calls[-1]) == 1 + 8 + 1
    assert calls[-1][1]["content"] == "q5"


async def test_without_active_reel_questions_search_the_library(pg, monkeypatch):
    acc, _ = await pg.ig_account("c2")
    other, _ = await pg.ig_account("c3")
    await _save(
        pg,
        Reel(shortcode="py", title="Python tips", summary="Use list comprehensions", tags=["python"]),
        Reel(shortcode="leg", title="Leg day", summary="Squats and lunges", tags=["fitness"]),
        Reel(shortcode="theirs", title="Python secrets", summary="decorators"),
    )
    for code in ("py", "leg"):
        await pg.add_to_library(acc.id, code)
    await pg.add_to_library(other.id, "theirs")
    seen = {}

    async def fake_complete(messages, max_tokens=700):
        seen["prompt"] = messages[-1]["content"]
        return "Use comprehensions [1]"

    monkeypatch.setattr(chat, "complete", fake_complete)
    reply = await chat.answer(acc.id, "ig", "python comprehension tips")
    assert "[1] Python tips" in seen["prompt"] and "Python secrets" not in seen["prompt"]
    assert reply.endswith("[1] Python tips")  # sources listed under the answer
    assert [h.shortcode for h in await pg.search_library(acc.id, "squat", 3)] == ["leg"]
    assert await pg.search_library(acc.id, "quantum", 3) == []
    assert await pg.search_library(acc.id, "the of", 3) == []  # stopwords only
    assert "couldn't find" in await chat.answer(acc.id, "ig", "quantum physics")


async def test_active_reel_expires(pg, monkeypatch):
    acc, _ = await pg.ig_account("c4")
    await _save(pg, Reel(shortcode="a", title="T"))
    await pg.set_active_reel(acc.id, "ig", "a")
    assert chat.uses_active_reel(await pg.get_conversation(acc.id, "ig"))
    await pg.pool().execute("UPDATE conversations SET active_at = now() - interval '2 hours'")
    assert not chat.uses_active_reel(await pg.get_conversation(acc.id, "ig"))


def test_reel_context_respects_budget():
    reel = Reel(shortcode="a", title="T", summary="S", transcript="w " * 10000, caption="c " * 5000)
    ctx = chat.reel_context(reel, 3000)
    assert len(ctx) <= 3000
    assert json.loads(ctx)["title"] == "T"
