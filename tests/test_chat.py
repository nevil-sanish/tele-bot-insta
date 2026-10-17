import json

from services import chat
from services.notion_store import Reel


async def test_history_is_capped_and_reset(monkeypatch):
    calls = []

    async def fake_complete(messages, max_tokens=700):
        calls.append(messages)
        return "answer"

    monkeypatch.setattr(chat, "complete", fake_complete)
    chat.set_active(1, Reel(shortcode="a", title="T", summary="S"))
    for i in range(10):
        await chat.ask_active(1, f"q{i}")
    # system + 4 turns (8 messages) + new question
    assert len(calls[-1]) == 1 + 8 + 1
    assert calls[-1][1]["content"] == "q5"
    chat.reset(1)
    assert "No active reel" in await chat.ask_active(1, "hi")


def test_reel_context_respects_budget():
    reel = Reel(shortcode="a", title="T", summary="S", transcript="w " * 10000, caption="c " * 5000)
    ctx = chat.reel_context(reel, 3000)
    assert len(ctx) <= 3000
    assert json.loads(ctx)["title"] == "T"


async def test_ask_library_uses_top_hits(monkeypatch):
    from services.search import index

    index.load([Reel(shortcode="1", title="Notion setup", summary="databases and templates", notion_url="u")])
    seen = {}

    async def fake_complete(messages, max_tokens=700):
        seen["prompt"] = messages[-1]["content"]
        return "Use templates [1]"

    monkeypatch.setattr(chat, "complete", fake_complete)
    answer, hits = await chat.ask_library("notion templates")
    assert answer == "Use templates [1]" and hits[0].shortcode == "1"
    assert "[1] Notion setup" in seen["prompt"]
