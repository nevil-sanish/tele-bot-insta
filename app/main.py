"""FastAPI entry point: /webhook (Telegram) and /health (keep-alive pings)."""

import asyncio
import hmac
import logging
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from telegram import Update

from app import config
from app.bot import build_application
from app.worker import Worker, store
from services.search import index

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-brain")

_missing = config.missing_required()
if _missing:
    log.warning("missing environment variables: %s", ", ".join(_missing))
if not config.TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN is not set. Add it in Render → Environment, then redeploy.")

application = build_application()
# Telegram re-sends an update if a cold start makes it time out; remember recent IDs to drop repeats.
_seen_updates: deque[int] = deque(maxlen=500)


async def load_index() -> None:
    """Rebuild the in-memory BM25 index from Notion (runs after every restart)."""
    if not store.enabled:
        return
    try:
        reels = await store.load_all()
        index.load(reels)
        log.info("search index loaded: %d reels", len(reels))
    except Exception as e:
        log.warning("could not load search index: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await application.initialize()
    await application.start()
    worker = Worker(application.bot)
    application.bot_data["worker"] = worker
    worker.start()
    index_task = asyncio.create_task(load_index())
    if config.PUBLIC_URL:
        await application.bot.set_webhook(
            url=f"{config.PUBLIC_URL.rstrip('/')}/webhook",
            secret_token=config.WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("webhook set to %s/webhook", config.PUBLIC_URL)
    try:
        yield
    finally:
        index_task.cancel()
        await worker.stop()
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Reel Brain", lifespan=lifespan)


@app.get("/health")
@app.head("/health")
async def health() -> dict:
    return {"ok": True, "queued": application.bot_data["worker"].queue.qsize(), "library": len(index)}


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    if config.WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, config.WEBHOOK_SECRET):
            raise HTTPException(status_code=403)
    update = Update.de_json(await request.json(), application.bot)
    if update.update_id in _seen_updates:
        return {"ok": True}
    _seen_updates.append(update.update_id)
    # Hand off and return 200 immediately, so Telegram doesn't retry.
    await application.update_queue.put(update)
    return {"ok": True}
