"""Q&A with Groq.

- A question right after a reel → about that reel (reel JSON + last 4 turns).
- Otherwise → full-text search over the person's library, top 3 → Groq.
Conversation state lives in Postgres, so any instance can answer. Prompts are
trimmed to a fixed budget so they never grow past ~4K tokens.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from app import config

from . import db
from .analyze import groq_call
from .models import Reel
from .spec import CHAT

log = logging.getLogger(__name__)


def reel_context(reel: Reel, budget: int) -> str:
    """Compact JSON of the reel, cutting the long text fields first."""
    data = {
        "title": reel.title, "author": reel.author, "link": reel.link,
        "summary": reel.summary, "key_ideas": reel.key_ideas, "tools": reel.tools,
        "urls": reel.urls, "tags": reel.tags, "category": reel.category,
        "on_screen_text": reel.on_screen_text, "transcript": reel.transcript, "caption": reel.caption,
    }
    for key in ("caption", "transcript", "on_screen_text"):
        text = json.dumps(data, ensure_ascii=False)
        over = len(text) - budget
        if over <= 0:
            break
        value = data[key]
        data[key] = value[: max(0, len(value) - over - 20)] + ("…" if value else "")
    return json.dumps(data, ensure_ascii=False)[:budget]


async def complete(messages: list[dict], max_tokens: int = 700) -> str:
    last = None
    for model in config.GROQ_CHAT_MODELS:
        try:
            resp = await groq_call(model, lambda c, model=model: c.chat.completions.create(
                model=model,
                messages=messages,
                reasoning_effort="low",
                max_completion_tokens=max_tokens,
                temperature=0.3,
            ))
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last = e
            log.warning("groq chat failed on %s: %s", model, e)
    raise RuntimeError(f"Groq unavailable: {last}")


SYSTEM_REEL = CHAT["reel_system"]
SYSTEM_LIBRARY = CHAT["library_system"]


def uses_active_reel(conv: db.Conversation) -> bool:
    """Questions go to the last reel for ACTIVE_REEL_MINUTES after it (or the last question)."""
    if not (conv.active and conv.active_at):
        return False
    return datetime.now(UTC) - conv.active_at < timedelta(minutes=config.ACTIVE_REEL_MINUTES)


async def answer(account_id: int, channel: str, question: str) -> str:
    """Answer about the active reel if there is one, else from the whole library."""
    conv = await db.get_conversation(account_id, channel)
    if uses_active_reel(conv):
        return await ask_active(account_id, channel, question, conv)
    text, hits = await ask_library(question, account_id)
    if hits:
        text += "\n\n" + "\n".join(f"[{i}] {r.title}" for i, r in enumerate(hits, 1))
    return text


async def ask_active(account_id: int, channel: str, question: str, conv: db.Conversation | None = None) -> str:
    conv = conv or await db.get_conversation(account_id, channel)
    if not conv.active:
        return "No reel to talk about yet. Share a reel with me first."
    history = conv.history
    history_chars = sum(len(m["content"]) for m in history)
    budget = max(2000, config.PROMPT_CHAR_BUDGET - history_chars - len(question) - len(SYSTEM_REEL))
    messages = [
        {"role": "system", "content": f"{SYSTEM_REEL}\n\nReel data:\n{reel_context(conv.active, budget)}"},
        *history,
        {"role": "user", "content": question[:2000]},
    ]
    reply = await complete(messages)
    history += [{"role": "user", "content": question[:1000]}, {"role": "assistant", "content": reply[:1500]}]
    await db.save_history(account_id, channel, history[-config.CHAT_HISTORY_TURNS * 2:])
    return reply


async def ask_library(question: str, account_id: int) -> tuple[str, list[Reel]]:
    hits = await db.search_library(account_id, question, limit=config.SEARCH_TOP_K)
    if not hits:
        return "I couldn't find any of your saved reels about that. Try other keywords, or share a reel first.", []
    per = (config.PROMPT_CHAR_BUDGET - 1000) // len(hits)
    blocks = []
    for i, r in enumerate(hits, 1):
        body = f"[{i}] {r.title} (by {r.author or 'unknown'}; tags: {', '.join(r.tags)})\n{r.summary}"
        blocks.append(body[:per])
    messages = [
        {"role": "system", "content": SYSTEM_LIBRARY},
        {"role": "user", "content": "Reels:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question[:1000]}"},
    ]
    return await complete(messages), hits
