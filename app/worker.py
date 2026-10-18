"""Background queue: the webhook returns 200 at once, and reels are processed
here with live status edits (Fetching… → Analyzing… → Saving…)."""

import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest

from app import config
from fetchers import chain
from fetchers.base import FetchError, FetchResult, MediaFile, new_tmp_path
from services import analyze, caption as cap, chat
from services.notion_store import NotionStore, Reel
from services.search import index

log = logging.getLogger(__name__)

store = NotionStore()


@dataclass
class Job:
    chat_id: int
    status_message_id: int
    url: str | None = None  # Instagram link
    file_id: str | None = None  # forwarded Telegram video
    file_unique_id: str | None = None
    mime_type: str = "video/mp4"

    @property
    def shortcode(self) -> str:
        return cap.extract_shortcode(self.url) or f"tg_{self.file_unique_id}"


class Worker:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        # shortcode -> jobs from other chats that asked for the same reel while it was running
        self.in_flight: dict[str, list[Job]] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self, n: int = config.WORKER_CONCURRENCY) -> None:
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(n)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def handle(self, job: Job) -> None:
        """Entry point for every reel: saved results are shown at once, new reels are queued."""
        if await show_cached(self.bot, job):
            return
        if not self.submit(job):
            await _status(self.bot, job, "⏳ This reel is being analyzed right now. I'll post the result here.")

    def submit(self, job: Job) -> bool:
        """Queue a reel. If it's already queued/running, the job waits for that run instead
        (it then gets the saved result) and this returns False."""
        if job.shortcode in self.in_flight:
            self.in_flight[job.shortcode].append(job)
            return False
        self.in_flight[job.shortcode] = []
        self.queue.put_nowait(job)
        return True

    async def _loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._run(job)
                # Chats that sent the same reel meanwhile now get the saved result (0 tokens).
                for waiter in self.in_flight.pop(job.shortcode, []):
                    await self._run(waiter)
            finally:
                self.in_flight.pop(job.shortcode, None)
                self.queue.task_done()

    async def _run(self, job: Job) -> None:
        try:
            await process(self.bot, job)
        except Exception:
            log.exception("job crashed: %s", job)
            await _status(self.bot, job, "❌ Something went wrong. Try /retry later.")


# ---------------------------------------------------------------- pipeline


async def process(bot: Bot, job: Job) -> None:
    shortcode = job.shortcode
    link = cap.canonical_url(shortcode) if job.url else ""

    # 1. Cache: already in Notion (analyzed by anyone) → show it, 0 tokens.
    if await show_cached(bot, job):
        return

    # 2. Fetch (or download the forwarded file).
    await _status(bot, job, "📥 Fetching…")
    try:
        fetched = await _fetch(bot, job)
    except FetchError as e:
        log.warning("all fetchers failed for %s: %s", shortcode, e)
        chat.state(job.chat_id).pending_link = job.url
        await _save_failed(Reel(shortcode=shortcode, link=link), f"fetch: {e}")
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
            extra = await cap.instaloader_caption(shortcode)
            if extra:
                caption_text = extra["caption"]
                fetched.author = fetched.author or extra["author"]
                fetched.date = fetched.date or extra["date"]

        # 4. One Gemini call (with fallbacks).
        await _status(bot, job, "🧠 Analyzing…")
        try:
            result = await analyze.analyze(fetched.media, caption_text)
        except analyze.AnalysisError as e:
            log.warning("analysis failed for %s: %s", shortcode, e)
            await _save_failed(
                Reel(shortcode=shortcode, link=link, author=fetched.author or "", source=fetched.source),
                f"analysis: {e}",
            )
            await _status(bot, job, "❌ Analysis failed (AI quota or service error). Use /retry later.")
            return
    finally:
        # 5. Only text is kept.
        fetched.cleanup()

    reel = build_reel(shortcode, link, fetched, caption_text, result)

    # 6. Save to Notion.
    if store.enabled:
        await _status(bot, job, "💾 Saving…")
        try:
            await store.save(reel)
        except Exception as e:
            log.exception("notion save failed")
            reel.notion_url = ""
            reel.status = f"not saved ({type(e).__name__})"
    index.add(reel)
    chat.set_active(job.chat_id, reel)
    await _status(bot, job, format_result(reel), reel=reel)
    if not job.url:
        await bot.send_message(
            job.chat_id,
            "Tip: send the video with the reel link as its caption to include the original caption.",
        )


async def show_cached(bot: Bot, job: Job) -> bool:
    """If this reel is already in the Notion library, show the saved result and return True."""
    if not store.enabled:
        return False
    try:
        cached = await store.find(job.shortcode)
    except Exception as e:
        log.warning("notion cache lookup failed: %s", e)
        return False
    if not cached:
        return False
    log.info("cache hit: %s", job.shortcode)
    chat.set_active(job.chat_id, cached)
    await _status(bot, job, format_result(cached, cached=True), reel=cached)
    return True


def build_reel(shortcode: str, link: str, fetched: FetchResult, caption_text: str, result) -> Reel:
    d = result.data
    hashtags = cap.extract_hashtags(caption_text)
    # Gemini's topic tags first, then the author's hashtags, max 10 for Notion.
    tags = list(dict.fromkeys([t.lower().lstrip("#") for t in d.tags] + [h.lower() for h in hashtags]))[:10]
    return Reel(
        shortcode=shortcode,
        link=link,
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
    )


async def _fetch(bot: Bot, job: Job) -> FetchResult:
    if job.file_id:
        tg_file = await bot.get_file(job.file_id)
        ext = ".mp4" if job.mime_type.startswith("video/") else ".jpg"
        path = new_tmp_path(ext)
        await tg_file.download_to_drive(path)
        return FetchResult(source="forwarded", media=[MediaFile(path, job.mime_type)], caption=None)
    return await chain.fetch(job.url)


async def _save_failed(reel: Reel, reason: str) -> None:
    if not store.enabled:
        return
    try:
        await store.save_failed(reel, reason)
    except Exception as e:
        log.warning("could not record failure in Notion: %s", e)


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
        notes.append("from your library")
    if reel.analysis and reel.analysis != "full":
        notes.append(f"analysis: {reel.analysis}")
    if reel.status not in ("", "done"):
        notes.append(reel.status)
    if notes:
        lines.append(f"\n<i>({e(', '.join(notes))})</i>")
    lines.append("\n💬 Ask me anything about this reel.")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def _buttons(reel: Reel | None):
    if not reel:
        return None
    row = []
    if reel.notion_url:
        row.append(InlineKeyboardButton("Open in Notion", url=reel.notion_url))
    if reel.link:
        row.append(InlineKeyboardButton("Open reel", url=reel.link))
    return InlineKeyboardMarkup([row]) if row else None


async def _status(bot: Bot, job: Job, text: str, reel: Reel | None = None) -> None:
    """Edit the job's status message; fall back to a new message if editing fails."""
    kwargs = {}
    if reel is not None:
        kwargs = {
            "parse_mode": ParseMode.HTML,
            "reply_markup": _buttons(reel),
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
    try:
        await bot.edit_message_text(text, chat_id=job.chat_id, message_id=job.status_message_id, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await bot.send_message(job.chat_id, text, **kwargs)
