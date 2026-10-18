"""Turn one Instagram DM into an action: queue a reel, queue a question, or
answer a command. Runs right after the webhook returns 200 to Meta; only fast
database work and short replies happen here, everything slow is a job.
"""

import hashlib
import logging

from app import config
from channels.instagram import Attachment, Inbound, Messenger, SendError
from services import caption as cap
from services import db

log = logging.getLogger(__name__)

CHANNEL = "ig"
REEL_TYPES = {"ig_reel", "reel", "share", "ig_post"}

HELP = (
    "👋 I'm Reel Brain. Share any reel with me (➤ Share → send to me) and I'll summarize it "
    "and save it to your library.\n\n"
    "Then just ask questions — about the last reel, or anything you've saved.\n\n"
    "Commands:\n• credits — what you have left\n• new — start fresh\n• link 123456 — connect the app\n• help"
)
WELCOME = "👋 Welcome to Reel Brain! You have {reels} free reels to try.\n\n" + HELP
PAYWALL = (
    "You're out of credits. {plans}\n\n"
    "Already have the app with your own AI key? It's free there."
)
PLANS_SOON = "Paid plans are coming very soon."
BUSY = "⏳ You already have a few reels in progress — I'll get to this one if you send it again in a minute."
QUEUED = "📥 Got it — analyzing…"


def stand_in_key(prefix: str, value: str) -> str:
    """Cache key for a reel whose shortcode we don't know (e.g. a reel shared without a link)."""
    return f"{prefix}_{hashlib.sha1(value.encode()).hexdigest()[:16]}"


def reel_from_attachment(att: Attachment) -> tuple[str | None, str | None]:
    """(shortcode or stand-in key, media URL) for a shared reel/post."""
    shortcode = cap.extract_shortcode(att.url) or cap.extract_shortcode(str(att.payload.get("link") or ""))
    media_url = att.url if att.url and not cap.extract_shortcode(att.url) else None
    if not shortcode:
        video_id = att.payload.get("reel_video_id") or att.payload.get("video_id") or att.payload.get("id")
        if video_id:
            shortcode = f"igv_{video_id}"
        elif media_url:
            shortcode = stand_in_key("igv", media_url.split("?")[0])
    return shortcode, media_url


async def handle(msg: Inbound, out: Messenger) -> None:
    account, created = await db.ig_account(msg.sender)
    if created:
        log.info("new Instagram user %s → account %s", msg.sender, account.id)  # IGSID for ADMIN_IG_IDS
    await db.touch_inbound(account.id, CHANNEL)
    reply = await route(msg, account, created)
    if reply:
        try:
            await out.send_text(msg.sender, reply)
        except SendError as e:
            log.warning("reply to %s failed: %s", msg.sender, e)


async def route(msg: Inbound, account: db.Account, created: bool) -> str | None:
    """Do what the message asks; returns the immediate reply text (or None)."""
    welcome = WELCOME.format(reels=account.reels_left()) if created else None
    # A command is a tapped quick reply or a whole one-word message ("credits"), so a
    # question that happens to start with "hi" is still a question.
    words = msg.text.lower().strip(" .!?").split()
    command = (msg.quick_reply or (words[0] if len(words) == 1 else "")).lower()
    if len(words) == 2 and words[0] == "link":
        command = "link"

    if command in ("help", "start", "hi", "hello"):
        return welcome or HELP
    if command in ("credits", "balance", "plan"):
        return credits_text(account)
    if command in ("new", "reset"):
        await db.reset_conversation(account.id, CHANNEL)
        return "🆕 Fresh start. Share a reel, or ask about anything in your library."
    if command == "ask":
        return "Sure — type your question about the reel."
    if command == "link":
        return await link(msg.text, account)

    for att in msg.attachments:
        if att.type in REEL_TYPES:
            shortcode, media_url = reel_from_attachment(att)
            if shortcode:
                caption = att.payload.get("title") or ""
                return _with_welcome(welcome, await start_reel(account, msg.sender, shortcode, media_url, caption, "meta"))
        if att.type == "video" and att.url:
            # The video we asked for after a failed fetch, or just a video file.
            shortcode = await db.pop_pending(account.id, CHANNEL) or stand_in_key("igu", msg.mid)
            return _with_welcome(welcome, await start_reel(account, msg.sender, shortcode, att.url, "", "upload"))
    if msg.attachments:
        return welcome or "Send me a reel (➤ Share → send to me) or a reel link, and I'll analyze it."

    url = cap.find_ig_url(msg.text)
    if url:
        return _with_welcome(welcome, await start_reel(account, msg.sender, cap.extract_shortcode(url), None, "", "apify"))
    if created or not msg.text:
        return welcome or HELP
    return await start_question(account, msg.sender, msg.text)


def _with_welcome(welcome: str | None, reply: str | None) -> str | None:
    return "\n\n".join(x for x in (welcome, reply) if x) or None


def credits_text(account: db.Account) -> str:
    if account.unlimited:
        return "♾️ You're on the unlimited plan."
    units, per_reel = account.units, config.UNITS_PER_REEL
    questions = units // max(config.UNITS_PER_QUESTION, 1)
    return (
        f"🧾 You have {units // per_reel} reels left ({units} credits).\n"
        f"A reel costs {per_reel} credits, a question {config.UNITS_PER_QUESTION} — so that's up to {questions} questions.\n\n"
        f"{PLANS_SOON}"
    )


async def link(text: str, account: db.Account) -> str:
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return "Open the Reel Brain app → Profile → Link Instagram, then send me: link 123456"
    merged = await db.link_with_code(parts[1], account.id)
    if not merged:
        return "That code is wrong or expired. Get a new one in the app."
    return "✅ Linked! Reels you share here now show up in the app, and you have one shared balance."


async def start_reel(
    account: db.Account, recipient: str, shortcode: str, media_url: str | None, caption: str, source: str
) -> str | None:
    if await db.pending_jobs(account.id) >= config.MAX_PENDING_JOBS:
        return BUSY
    if await db.get_reel(shortcode):  # analyzed before: still costs a reel (decision #3), but instant
        stage, payload = "deliver", {}
    elif media_url:
        stage = "analyze"
        payload = {"media": [(media_url, "video/mp4")], "caption": caption, "source": source}
    else:
        stage, payload = "fetch", {}
    job = await db.create_job(
        account.id, "reel", stage, CHANNEL, units=config.UNITS_PER_REEL, recipient=recipient,
        shortcode=shortcode, payload=payload, priority=account.priority,
    )
    if job is None:
        return PAYWALL.format(plans=PLANS_SOON)
    return None if stage == "deliver" else QUEUED


async def start_question(account: db.Account, recipient: str, question: str) -> str | None:
    if await db.pending_jobs(account.id) >= config.MAX_PENDING_JOBS:
        return BUSY
    job = await db.create_job(
        account.id, "chat", "chat", CHANNEL, units=config.UNITS_PER_QUESTION, recipient=recipient,
        payload={"question": question[:2000]}, priority=account.priority,
    )
    return None if job else PAYWALL.format(plans=PLANS_SOON)
