"""What each job stage does. A reel job moves fetch → analyze → deliver; a
question is a single `chat` stage; a `fetch` job (app users with their own AI
key) stops after fetch and returns the media URLs to the phone. Every stage only needs the database and the
job row, so stages of one job can run on different worker machines.

Money: units are reserved when the job is created (app/inbound.py, app/api.py).
A job that fails before the user gets their result is refunded; a delivery
that fails after the reel is saved to their library is not.
"""

import logging

from app import config
from channels.instagram import Messenger, SendError
from fetchers import apify
from fetchers.base import FetchError, FetchResult, MediaFile, download
from services import analyze, chat, db
from services import caption as cap
from services.db import Job
from services.models import Reel

log = logging.getLogger(__name__)

ASK_FOR_VIDEO = (
    "😕 I couldn't get that reel. Please send me the video itself: open the reel → ⋯ → Download "
    "(or save it), then send the file here and I'll analyze it."
)
ANALYSIS_FAILED = "❌ I couldn't analyze that reel right now (AI service busy). Your credit was refunded — please try again in a bit."
CHAT_FAILED = "❌ I couldn't answer that right now. Your credit was refunded — please try again in a bit."
REEL_OPTIONS = [("💬 Ask a question", "ASK"), ("🧾 My credits", "CREDITS"), ("❓ Help", "HELP")]


def is_real_shortcode(shortcode: str | None) -> bool:
    """Stand-in keys (see app/inbound.py) start with ig*_ and can't be looked up on Apify."""
    return bool(shortcode) and not shortcode.startswith(("igv_", "igu_"))


async def run(jobs: list[Job], out: Messenger | None) -> None:
    """Run one claimed job, or a batch of fetch jobs."""
    stage = jobs[0].stage
    if stage == "fetch":
        await fetch(jobs, out)
    elif stage == "analyze":
        await analyze_reel(jobs[0], out)
    elif stage == "deliver":
        await deliver(jobs[0], out)
    elif stage == "chat":
        await answer(jobs[0], out)
    else:
        await db.fail_job(jobs[0].id, f"unknown stage {stage}")


# ---------------------------------------------------------------- fetch


async def fetch(jobs: list[Job], out: Messenger | None) -> None:
    """Find media for many reels with one Apify run."""
    todo = []
    for job in jobs:
        if not await db.get_reel(job.shortcode):
            todo.append(job)
        elif job.kind == "fetch":  # the phone wanted media, but the reel is already analyzed: free
            await db.add_to_library(job.account_id, job.shortcode, job.channel)
            await db.refund_job(job.id)
            await db.finish_job(job.id, cached=True)
        else:  # analyzed by someone while this waited
            await db.advance(job, "deliver")
    if not todo:
        return
    try:
        found = await apify.lookup(list({j.shortcode for j in todo}))
    except FetchError as e:
        log.warning("apify lookup failed for %d reels: %s", len(todo), e)
        found = {}
    for job in todo:
        meta = found.get(job.shortcode)
        if meta and job.kind == "fetch":
            # App users with their own AI key: hand the media back; the phone analyzes it.
            await db.finish_job(job.id, source="apify", **meta.to_payload())
        elif meta:
            await db.advance(job, "analyze", source="apify", **meta.to_payload())
        else:
            await _ask_for_video(job, out, "fetch: apify could not resolve the reel")


async def _ask_for_video(job: Job, out: Messenger | None, error: str) -> None:
    await db.fail_job(job.id, error)
    if job.channel == "ig" and out and job.recipient:
        await db.set_pending(job.account_id, job.channel, job.shortcode if is_real_shortcode(job.shortcode) else None)
        await _send(out, job, ASK_FOR_VIDEO)


# ---------------------------------------------------------------- analyze


async def analyze_reel(job: Job, out: Messenger | None) -> None:
    if await db.get_reel(job.shortcode):
        await db.advance(job, "deliver")
        return
    p = job.payload
    try:
        fetched = await _download(p)
    except Exception as e:
        log.warning("download failed for %s: %s", job.shortcode, e)
        # A webhook media URL can expire; Apify can find the reel again if we know its shortcode.
        if p.get("source") == "meta" and is_real_shortcode(job.shortcode) and not p.get("tried_apify"):
            await db.advance(job, "fetch", tried_apify=True)
        else:
            await _ask_for_video(job, out, f"download: {e}")
        return

    try:
        if job.channel == "ig" and out and job.recipient:
            await out.typing(job.recipient)
        try:
            result = await analyze.analyze(fetched.media, fetched.caption)
        except analyze.AnalysisError as e:
            log.warning("analysis failed for %s: %s", job.shortcode, e)
            await db.fail_job(job.id, f"analysis: {e}")
            await _send(out, job, ANALYSIS_FAILED)
            return
    finally:
        fetched.cleanup()  # only text is kept

    await db.save_analysis(job, build_reel(job.shortcode, fetched, result))


async def _download(payload: dict) -> FetchResult:
    media: list[MediaFile] = []
    try:
        for url, mime in payload.get("media") or []:
            media.append(await download(url, mime))
    except Exception:
        for m in media:
            m.path.unlink(missing_ok=True)
        raise
    if not media:
        raise FetchError("no media in job")
    return FetchResult(
        source=payload.get("source", ""), media=media, caption=payload.get("caption") or "",
        author=payload.get("author"), date=payload.get("date"),
    )


def build_reel(shortcode: str, fetched: FetchResult, result) -> Reel:
    d = result.data
    caption_text = fetched.caption or ""
    hashtags = cap.extract_hashtags(caption_text)
    # Topic tags from the AI first, then the author's hashtags, max 10.
    tags = list(dict.fromkeys([t.lower().lstrip("#") for t in d.tags] + [h.lower() for h in hashtags]))[:10]
    return Reel(
        shortcode=shortcode,
        link=cap.canonical_url(shortcode) if is_real_shortcode(shortcode) else "",
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


# ---------------------------------------------------------------- deliver


def format_summary(reel: Reel) -> str:
    """Plain-text DM (Instagram shows no formatting), aimed at one 1000-byte message."""
    head = f"🎬 {reel.title}" + (f" — @{reel.author}" if reel.author else "")
    lines = [head]
    if reel.summary:
        lines += ["", reel.summary]
    if reel.key_ideas:
        lines += ["", "💡 Key ideas"] + [f"• {k}" for k in reel.key_ideas[:4]]
    if reel.tools:
        lines += ["", "🔧 " + ", ".join(reel.tools[:6])]
    if reel.urls:
        lines.append("🔗 " + " ".join(reel.urls[:3]))
    if reel.tags:
        lines.append("🏷 " + " ".join("#" + t.replace(" ", "_") for t in reel.tags[:6]))
    if reel.analysis and reel.analysis != "full":
        lines.append(f"(analysis: {reel.analysis})")
    lines += ["", "Saved to your library. Ask me anything about it 👇"]
    text = "\n".join(lines)
    if len(text.encode()) > config.DM_MAX_BYTES:  # drop the ideas before cutting mid-sentence
        text = "\n".join([head, "", reel.summary, "", "Saved to your library. Ask me anything about it 👇"])
    return text


async def deliver(job: Job, out: Messenger | None) -> None:
    reel = await db.get_reel(job.shortcode)
    if not reel:
        await db.fail_job(job.id, "deliver: reel missing")
        return
    await db.add_to_library(job.account_id, reel.shortcode, job.channel)
    await db.set_active_reel(job.account_id, job.channel, reel.shortcode)
    if job.channel == "ig" and job.recipient:
        if out is None:
            raise RuntimeError("no Instagram sender configured")
        try:
            await out.send_text(job.recipient, format_summary(reel), REEL_OPTIONS)
        except SendError as e:
            # The reel is in their library either way, so this isn't refunded.
            await db.fail_job(job.id, f"deliver: {e}", refund=False)
            return
    await db.finish_job(job.id)


# ---------------------------------------------------------------- chat


async def answer(job: Job, out: Messenger | None) -> None:
    question = job.payload.get("question", "")
    try:
        if job.channel == "ig" and out and job.recipient:
            await out.typing(job.recipient)
        reply = await chat.answer(job.account_id, job.channel, question)
    except Exception as e:
        log.warning("chat failed for job %s: %s", job.id, e)
        await db.fail_job(job.id, f"chat: {e}")
        await _send(out, job, CHAT_FAILED)
        return
    if job.channel == "ig" and out and job.recipient:
        try:
            await out.send_text(job.recipient, reply)
        except SendError as e:
            await db.fail_job(job.id, f"deliver: {e}")
            return
    await db.finish_job(job.id, reply=reply)  # the app reads the reply from the job


async def _send(out: Messenger | None, job: Job, text: str) -> None:
    """Status message to an Instagram user; failures are logged, never raised."""
    if job.channel != "ig" or not (out and job.recipient):
        return
    try:
        await out.send_text(job.recipient, text)
    except SendError as e:
        log.warning("could not message %s: %s", job.recipient, e)
