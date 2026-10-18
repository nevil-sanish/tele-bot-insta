"""Q&A with Groq.

- Plain text → question about the chat's active reel (reel JSON + last 4 turns).
- /ask → full-text search over the user's library, top 3 → Groq.
Conversation state lives in Postgres, so any instance can answer. Prompts are
trimmed to a fixed budget so they never grow past ~4K tokens.
"""

import json
import logging

from app import config

from . import db
from .analyze import groq_call
from .models import Reel

log = logging.getLogger(__name__)


async def set_active(chat_id: int, reel: Reel) -> None:
    """The reel follow-up questions are about; starting a new reel clears the history."""
    await db.set_active_reel(chat_id, reel.shortcode)


async def set_pending(chat_id: int, link: str | None) -> None:
    """Remember a link we couldn't fetch, so the next forwarded video is matched to it."""
    await db.set_pending_link(chat_id, link)


async def pop_pending(chat_id: int) -> str | None:
    return await db.pop_pending_link(chat_id)


async def reset(chat_id: int) -> None:
    await db.reset_chat(chat_id)


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


SYSTEM_REEL = (
    "You answer questions about one Instagram reel using only the reel data given. "
    "If the answer isn't in the data, say so briefly. Be concise; plain text, no markdown tables."
)

SYSTEM_LIBRARY = (
    "You answer questions using the user's saved reel library. Use only the reels given. "
    "Cite the reels you use as [1], [2], … matching their numbers. If none are relevant, say so. "
    "Be concise; plain text, no markdown tables."
)


async def ask_active(chat_id: int, question: str) -> str:
    active, history, _ = await db.get_chat(chat_id)
    if not active:
        return "No active reel yet. Send me a reel link first, or use /ask to search your library."
    history_chars = sum(len(m["content"]) for m in history)
    budget = max(2000, config.PROMPT_CHAR_BUDGET - history_chars - len(question) - len(SYSTEM_REEL))
    messages = [
        {"role": "system", "content": f"{SYSTEM_REEL}\n\nReel data:\n{reel_context(active, budget)}"},
        *history,
        {"role": "user", "content": question[:2000]},
    ]
    answer = await complete(messages)
    history += [{"role": "user", "content": question[:1000]}, {"role": "assistant", "content": answer[:1500]}]
    await db.save_history(chat_id, history[-config.CHAT_HISTORY_TURNS * 2:])
    return answer


async def ask_library(question: str, user_id: int) -> tuple[str, list[Reel]]:
    hits = await db.search_library(user_id, question, limit=config.SEARCH_TOP_K)
    if not hits:
        return "I couldn't find any saved reels matching that. Try different keywords.", []
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
