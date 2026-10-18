"""Background processing on a persistent Postgres job queue.

The webhook returns 200 at once; jobs live in the `jobs` table, so a crash or
redeploy never loses a reel: interrupted jobs are put back (or taken over once
their heartbeat stops) and resume, editing the same Telegram status message.
"""

import asyncio
import html
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest

from app import config
from fetchers import chain
from fetchers.base import FetchError, FetchResult, MediaFile, new_tmp_path
from services import analyze, caption as cap, chat, db
from services.db import Job
from services.models import Reel

log = logging.getLogger(__name__)


def shortcode_for(url: str | None, file_unique_id: str | None = None) -> str | None:
    return cap.extract_shortcode(url) or (f"tg_{file_unique_id}" if file_unique_id else None)


class Worker:
    """Runs `n` concurrent job loops. Any number of Worker processes can share the
    queue; enqueues anywhere wake them through Postgres NOTIFY."""

    def __init__(self, bot: Bot | None) -> None:
        self.bot = bot
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.idle_poll = config.QUEUE_IDLE_POLL

    def start(self, n: int = config.WORKER_CONCURRENCY, listen: bool = True) -> None:
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(n)]
        if listen:
            self._tasks.append(asyncio.create_task(self._listen()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _listen(self) -> None:
        """Keep a LISTEN connection open; if that's impossible, poll the queue often."""
        while True:
            con = None
            try:
                con = await db.listen_for_jobs(self.wake)
                self.idle_poll = config.QUEUE_IDLE_POLL
                self.wake()  # catch up on anything enqueued while we weren't listening
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

    def wake(self) -> None:
        self._wake.set()

    async def submit(self, user_id: int, shortcode: str, **job_fields) -> tuple[Reel | None, Job | None]:
        """Saved reel → (reel, None), added to the user's library at no cost.
        New reel → (None, job) queued for analysis."""
        reel = await db.get_reel(shortcode)
        if reel:
            await db.add_to_library(user_id, shortcode)
            return reel, None
        job = await db.enqueue(user_id, shortcode, **job_fields)
        self.wake()
        return None, job

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                async with self._claim_lock:
                    job = await db.claim_job()
            except Exception as e:
                log.warning("queue claim failed: %s", e)
                await asyncio.sleep(5)
                continue
            if job is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._run(job)

    async def _run(self, job: Job) -> None:
        beat = asyncio.create_task(self._heartbeat(job.id))
        try:
            await process(self.bot, job)
        except asyncio.CancelledError:
            # Shutting down (redeploy): hand the job back so the next instance resumes it.
            await asyncio.shield(db.requeue(job.id))
            raise
        except Exception as e:
            log.exception("job %s crashed", job.id)
            await db.fail_job(job.id, f"crash: {type(e).__name__}: {e}")
            await _status(self.bot, job, "❌ Something went wrong. Try /retry later.")
        finally:
            beat.cancel()
            self.wake()  # jobs waiting on this reel can run now

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(config.JOB_HEARTBEAT)
            try:
                await db.heartbeat(job_id)
            except Exception as e:
                log.warning("heartbeat failed for job %s: %s", job_id, e)


# ---------------------------------------------------------------- pipeline


async def process(bot: Bot | None, job: Job) -> None:
    # 1. Cache: analyzed before (by anyone) → reuse it, 0 tokens, no credit.
    cached = await db.get_reel(job.shortcode)
    if cached:
        await db.finish_job(job, cached, analyzed=False)
        await _show(bot, job, cached, cached=True)
        return

    # 2. Fetch (or download the forwarded file).
    await _status(bot, job, "📥 Fetching…")
    try:
        fetched = await _fetch(bot, job)
    except FetchError as e:
        log.warning("all fetchers failed for %s: %s", job.shortcode, e)
        await db.fail_job(job.id, f"fetch: {e}")
        if job.chat_id:
            await chat.set_pending(job.chat_id, job.url)
        await _status(
            bot, job,
            "😕 Couldn't fetch this reel. Forward me the video (or save it and send it here, "
            f"max {config.TELEGRAM_MAX_FILE_MB} MB) and I'll analyze it with this link.",
        )
        return

    try:
        # 3. Caption by code; instaloader only if the fetcher had none.
        caption_text = fetched.caption or ""
        if not caption_text and job.url:
            extra = await cap.instaloader_caption(job.shortcode)
            if extra:
                caption_text = extra["caption"]
                fetched.author = fetched.author or extra["author"]
                fetched.date = fetched.date or extra["date"]

        # 4. One AI call (with fallbacks).
        await _status(bot, job, "🧠 Analyzing…")
        try:
            result = await analyze.analyze(fetched.media, caption_text)
        except analyze.AnalysisError as e:
            log.warning("analysis failed for %s: %s", job.shortcode, e)
            await db.fail_job(job.id, f"analysis: {e}")
            await _status(bot, job, "❌ Analysis failed (AI quota or service error). Use /retry later.")
            return
    finally:
        # 5. Only text is kept.
        fetched.cleanup()

    # 6. Save to the global cache + the user's library; charge a credit on credit plans.
    reel = build_reel(job, fetched, caption_text, result)
    await _status(bot, job, "💾 Saving…")
    await db.finish_job(job, reel, analyzed=True)
    await _show(bot, job, reel)
    if job.chat_id and not job.url:
        await bot.send_message(
            job.chat_id,
            "Tip: send the video with the reel link as its caption to include the original caption.",
        )


def build_reel(job: Job, fetched: FetchResult, caption_text: str, result) -> Reel:
    d = result.data
    hashtags = cap.extract_hashtags(caption_text)
    # Topic tags from the AI first, then the author's hashtags, max 10.
    tags = list(dict.fromkeys([t.lower().lstrip("#") for t in d.tags] + [h.lower() for h in hashtags]))[:10]
    return Reel(
        shortcode=job.shortcode,
        link=job.link,
        title=d.title or "Untitled reel",
        summary=d.summary,
        key_ideas=d.key_ideas[:5],
        on_screen_text=d.on_screen_text,
        transcript=d.transcript,
        tools=d.tools,
        tags=tags,
        category=d.category.lower(),
        caption=caption_text,
        hashtags=hashtags,
        urls=cap.extract_urls(caption_text),
        author=fetched.author or "",
        date=fetched.date,
        analysis=result.mode,
        source=fetched.source,
        model=result.model,
    )


async def _fetch(bot: Bot | None, job: Job) -> FetchResult:
    if job.file_id:
        tg_file = await bot.get_file(job.file_id)
        ext = ".mp4" if job.mime_type.startswith("video/") else ".jpg"
        path = new_tmp_path(ext)
        await tg_file.download_to_drive(path)
        return FetchResult(source="forwarded", media=[MediaFile(path, job.mime_type)], caption=None)
    return await chain.fetch(job.url)


# ---------------------------------------------------------------- telegram output


def format_result(reel: Reel, cached: bool = False) -> str:
    e = html.escape
    lines = [f"🎬 <b>{e(reel.title)}</b>"]
    if reel.author:
        lines[0] += f"  <i>@{e(reel.author)}</i>"
    if reel.summary:
        lines.append(e(reel.summary))
    if reel.key_ideas:
        lines.append("\n💡 <b>Key ideas</b>")
        lines += [f"• {e(k)}" for k in reel.key_ideas[:5]]
    extras = []
    if reel.tools:
        extras.append("🔧 Tools: " + e(", ".join(reel.tools[:8])))
    if reel.urls:
        extras.append("🔗 Links: " + " ".join(e(u) for u in reel.urls[:5]))
    if extras:
        lines.append("")
        lines += extras
    if reel.tags:
        lines.append("🏷 " + " ".join("#" + e(t.replace(" ", "_")) for t in reel.tags[:8]))
    notes = []
    if cached:
        notes.append("from the library")
    if reel.analysis and reel.analysis != "full":
        notes.append(f"analysis: {reel.analysis}")
    if notes:
        lines.append(f"\n<i>({e(', '.join(notes))})</i>")
    lines.append("\n💬 Ask me anything about this reel.")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def _buttons(reel: Reel):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Open reel", url=reel.link)]]) if reel.link else None


async def _show(bot: Bot | None, job: Job, reel: Reel, cached: bool = False) -> None:
    if job.chat_id and bot:
        await chat.set_active(job.chat_id, reel)
        await show_result(bot, job.chat_id, job.status_message_id, reel, cached)


async def show_result(bot: Bot, chat_id: int, message_id: int | None, reel: Reel, cached: bool = False) -> None:
    await _edit(
        bot, chat_id, message_id, format_result(reel, cached),
        parse_mode=ParseMode.HTML, reply_markup=_buttons(reel),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _status(bot: Bot | None, job: Job, text: str) -> None:
    """Live status for Telegram jobs; app jobs are polled instead."""
    if job.chat_id and bot:
        await _edit(bot, job.chat_id, job.status_message_id, text)


async def _edit(bot: Bot, chat_id: int, message_id: int | None, text: str, **kwargs) -> None:
    """Edit the status message; fall back to a new message if that fails."""
    try:
        if message_id is None:
            raise BadRequest("no status message")
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await bot.send_message(chat_id, text, **kwargs)
