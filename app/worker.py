"""Background processing on the persistent, staged Postgres job queue.

Jobs live in the `jobs` table, so a crash or redeploy never loses a reel:
interrupted jobs are put back (or taken over once their heartbeat stops) and
resume at the stage they were in. What each stage does: app/pipeline.py.
"""

import asyncio
import logging

from app import config, pipeline
from channels.instagram import Messenger
from services import db
from services.db import Job

log = logging.getLogger(__name__)


class Worker:
    """Runs `n` concurrent job loops. Any number of Worker processes can share the
    queue; jobs queued anywhere wake them through Postgres NOTIFY."""

    def __init__(self, out: Messenger | None) -> None:
        self.out = out
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.idle_poll = config.QUEUE_IDLE_POLL

    def start(self, n: int = config.WORKER_CONCURRENCY, listen: bool = True) -> None:
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(n)]
        if listen:
            self._tasks.append(asyncio.create_task(self._listen()))
        self._tasks.append(asyncio.create_task(self._housekeeping()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _listen(self) -> None:
        """Keep a LISTEN connection open; if that's impossible, poll the queue often."""
        while True:
            con = None
            try:
                con = await db.listen_for_jobs(self.wake)
                self.idle_poll = config.QUEUE_IDLE_POLL
                self.wake()  # catch up on anything queued while we weren't listening
                while not con.is_closed():
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("job notifications unavailable (%s); polling every %ss", e, config.QUEUE_FALLBACK_POLL)
                self.idle_poll = config.QUEUE_FALLBACK_POLL
                self.wake()
            finally:
                if con is not None and not con.is_closed():
                    await con.close()
            await asyncio.sleep(30)  # then try to reconnect

    async def _housekeeping(self) -> None:
        """Daily: refresh the Instagram token (about weekly) and prune old webhook records."""
        while True:
            try:
                if self.out and config.INSTAGRAM_ACCESS_TOKEN:
                    await self.out.refresh_token()
                await db.prune_inbound()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("housekeeping failed: %s", e)
            await asyncio.sleep(86400)

    async def _claim(self) -> list[Job]:
        """One job; a fetch job brings up to APIFY_BATCH-1 more fetch jobs along (one Apify run)."""
        async with self._claim_lock:
            job = await db.claim_job()
            if job is None:
                return []
            if job.stage == "fetch" and config.APIFY_BATCH > 1:
                return [job] + await db.claim_jobs("fetch", config.APIFY_BATCH - 1)
            return [job]

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                jobs = await self._claim()
            except Exception as e:
                log.warning("queue claim failed: %s", e)
                await asyncio.sleep(5)
                continue
            if not jobs:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll)
                except TimeoutError:
                    pass
                continue
            await self._run(jobs)

    async def _run(self, jobs: list[Job]) -> None:
        beats = [asyncio.create_task(self._heartbeat(j.id)) for j in jobs]
        try:
            await pipeline.run(jobs, self.out)
        except asyncio.CancelledError:
            # Shutting down (redeploy): hand the jobs back so the next instance resumes them.
            await asyncio.shield(asyncio.gather(*(db.requeue(j.id) for j in jobs)))
            raise
        except Exception as e:
            log.exception("jobs %s crashed", [j.id for j in jobs])
            for j in jobs:
                current = await db.get_job(j.id)
                if current and current.status == "running":
                    await db.fail_job(j.id, f"crash: {type(e).__name__}: {e}", refund=j.stage != "deliver")
        finally:
            for b in beats:
                b.cancel()
            self.wake()  # jobs waiting on this reel can run now

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(config.JOB_HEARTBEAT)
            try:
                await db.heartbeat(job_id)
            except Exception as e:
                log.warning("heartbeat failed for job %s: %s", job_id, e)
