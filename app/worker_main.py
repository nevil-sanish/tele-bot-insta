"""Standalone worker process (ROLE=worker): fetches, analyzes and delivers
queued reels and answers questions. Run as many as you need; they share the
Postgres queue.

    python -m app.worker_main
"""

import asyncio
import logging
import signal

from app import config
from app.worker import Worker
from channels.instagram import Messenger
from services import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-brain.worker")


async def main() -> None:
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL must be set.")
    missing = config.missing_required()
    if missing:
        log.warning("missing environment variables: %s", ", ".join(missing))
    await db.connect()
    out = Messenger()
    worker = Worker(out)
    worker.start(config.WORKER_CONCURRENCY)
    log.info("worker started: %d concurrent jobs", config.WORKER_CONCURRENCY)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down: running jobs go back on the queue")
    await worker.stop()
    await out.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
