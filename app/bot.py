"""Telegram handlers: links, forwarded videos, Q&A, /ask, /list, /retry, /reset."""

import html
import logging

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app import config
from app.worker import Job, Worker, store
from services import caption as cap, chat

log = logging.getLogger(__name__)

HELP = (
    "👋 <b>Reel Brain</b>\n\n"
    "Send me an Instagram reel/post link (or forward a video) and I'll extract the caption, "
    "speech, on-screen text and key ideas, save it to Notion, and answer questions about it.\n\n"
    "<b>Commands</b>\n"
    "/ask &lt;question&gt; – search your whole library\n"
    "/list – recently saved reels\n"
    "/retry – reprocess failed reels\n"
    "/reset – clear the active reel and chat history\n\n"
    "Any other message is a question about the last reel."
)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _worker(context: ContextTypes.DEFAULT_TYPE) -> Worker:
    return context.application.bot_data["worker"]


async def whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and user.id in config.ALLOWED_USER_IDS:
        return
    if update.effective_message and user:
        await update.effective_message.reply_text(
            f"⛔ This is a private bot. Your Telegram user ID is {user.id}; "
            "add it to ALLOWED_USER_IDS to get access."
        )
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    urls = list(dict.fromkeys(m.group(0) for m in cap.IG_URL_RE.finditer(text)))[:5]
    for url in urls:
        status = await msg.reply_text("⏳ Processing…")
        job = Job(chat_id=msg.chat_id, status_message_id=status.message_id, url=url)
        await _worker(context).handle(job)


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.video or msg.document or msg.animation
    if media is None:
        return
    mime = getattr(media, "mime_type", None) or "video/mp4"
    if not mime.startswith(("video/", "image/")):
        await msg.reply_text("Please send a video (or a reel link).")
        return
    if (media.file_size or 0) > config.TELEGRAM_MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(
            f"That file is over {config.TELEGRAM_MAX_FILE_MB} MB, which Telegram bots can't download. "
            "Try sending the reel link instead."
        )
        return

    # Link source, in order: the video's own caption, then a link we failed to fetch earlier.
    st = chat.state(msg.chat_id)
    url = cap.find_ig_url(msg.caption) or st.pending_link
    st.pending_link = None

    status = await msg.reply_text("⏳ Processing…")
    job = Job(
        chat_id=msg.chat_id,
        status_message_id=status.message_id,
        url=url,
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        mime_type=mime,
    )
    await _worker(context).handle(job)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    photo = msg.photo[-1]  # largest size
    st = chat.state(msg.chat_id)
    url = cap.find_ig_url(msg.caption) or st.pending_link
    st.pending_link = None
    status = await msg.reply_text("⏳ Processing…")
    job = Job(
        chat_id=msg.chat_id, status_message_id=status.message_id, url=url,
        file_id=photo.file_id, file_unique_id=photo.file_unique_id, mime_type="image/jpeg",
    )
    await _worker(context).handle(job)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        answer = await chat.ask_active(msg.chat_id, msg.text)
    except Exception as e:
        log.warning("Q&A failed: %s", e)
        answer = "⚠️ Chat is unavailable right now (Groq error). Try again in a minute."
    await msg.reply_text(answer[:4000], link_preview_options=NO_PREVIEW)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    question = " ".join(context.args or []).strip()
    if not question:
        await msg.reply_text("Usage: /ask <question>, e.g. /ask which reels talked about Python tools?")
        return
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        answer, hits = await chat.ask_library(question)
    except Exception as e:
        log.warning("/ask failed: %s", e)
        await msg.reply_text("⚠️ Chat is unavailable right now (Groq error). Try again in a minute.")
        return
    sources = [
        f"[{i}] <a href=\"{html.escape(r.notion_url or r.link)}\">{html.escape(r.title)}</a>"
        if (r.notion_url or r.link) else f"[{i}] {html.escape(r.title)}"
        for i, r in enumerate(hits, 1)
    ]
    text = html.escape(answer[:3500])
    if sources:
        text += "\n\n📚 " + "\n".join(sources)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not store.enabled:
        await msg.reply_text("Notion isn't configured.")
        return
    try:
        reels = await store.list_recent(10)
    except Exception as e:
        log.warning("/list failed: %s", e)
        await msg.reply_text("⚠️ Couldn't reach Notion.")
        return
    if not reels:
        await msg.reply_text("Your library is empty. Send me a reel link!")
        return
    lines = ["📚 <b>Recent reels</b>"]
    for r in reels:
        title = html.escape(r.title or r.shortcode)
        lines.append(f"• <a href=\"{html.escape(r.notion_url)}\">{title}</a>" if r.notion_url else f"• {title}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)


async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not store.enabled:
        await msg.reply_text("Notion isn't configured, so there's no failed list to retry.")
        return
    try:
        failed = await store.list_failed(5)
    except Exception as e:
        log.warning("/retry failed: %s", e)
        await msg.reply_text("⚠️ Couldn't reach Notion.")
        return
    retryable = [r for r in failed if r.link]
    if not retryable:
        extra = " (forwarded videos without a link must be sent again)" if failed else ""
        await msg.reply_text(f"Nothing to retry{extra}.")
        return
    for r in retryable:
        status = await msg.reply_text(f"🔁 Retrying {r.link}")
        _worker(context).submit(Job(chat_id=msg.chat_id, status_message_id=status.message_id, url=r.link))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat.reset(update.effective_message.chat_id)
    await update.effective_message.reply_text("🧹 Cleared. Send a new reel link to start again.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .updater(None)  # webhook mode: FastAPI feeds updates in
        .concurrent_updates(8)
        .build()
    )
    app.add_handler(TypeHandler(Update, whitelist), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("retry", retry))
    app.add_handler(CommandHandler("reset", reset))
    link_filter = filters.Regex(cap.IG_URL_RE) | filters.CaptionRegex(cap.IG_URL_RE)
    video_filter = filters.VIDEO | filters.ANIMATION | filters.Document.VIDEO
    app.add_handler(MessageHandler(video_filter, on_video))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(link_filter, on_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
