"""Standalone worker process (ROLE=worker): analyzes queued reels and posts
results to Telegram. Run as many as you need; they share the Postgres queue.

    python -m app.worker_main
"""

import asyncio
import logging
import signal

from telegram import Bot

from app import config
from app.worker import Worker
from services import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-brain.worker")


async def main() -> None:
    if not (config.TELEGRAM_TOKEN and config.DATABASE_URL):
        raise SystemExit("TELEGRAM_TOKEN and DATABASE_URL must be set.")
    await db.connect()
    bot = Bot(config.TELEGRAM_TOKEN)  # only used to post status updates and results
    await bot.initialize()
    worker = Worker(bot)
    worker.start(config.WORKER_CONCURRENCY)
    log.info("worker started: %d concurrent jobs", config.WORKER_CONCURRENCY)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down: running jobs go back on the queue")
    await worker.stop()
    await bot.shutdown()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
