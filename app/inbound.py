"""Turn one Instagram DM into an action: queue a reel, queue a question, or
answer a command. Runs right after the webhook returns 200 to Meta; only fast
database work and short replies happen here, everything slow is a job.
"""

import hashlib
import logging
from dataclasses import dataclass

from app import config
from channels.instagram import Attachment, Inbound, Messenger, SendError
from services import billing, db
from services import caption as cap

log = logging.getLogger(__name__)

CHANNEL = "ig"
REEL_TYPES = {"ig_reel", "reel", "share", "ig_post"}

HELP = (
    "👋 I'm Reel Brain. Share any reel with me (➤ Share → send to me) and I'll summarize it "
    "and save it to your library.\n\n"
    "Then just ask questions — about the last reel, or anything you've saved.\n\n"
    "Commands:\n• credits — what you have left\n• plans — upgrade\n• new — start fresh\n"
    "• link 123456 — connect the app\n• delete — erase your data\n• help"
)
WELCOME = "👋 Welcome to Reel Brain! You have {reels} free reels to try.\n\n" + HELP
OUT_OF_CREDITS = "You're out of credits."
APP_HINT = "Have the Reel Brain app with your own AI key? It's free there."
PLANS_SOON = "Paid plans are coming very soon."
BUSY = "⏳ You already have a few reels in progress — I'll get to this one if you send it again in a minute."
QUEUED = "📥 Got it — analyzing…"
DELETE_ASK = (
    "This erases your library, questions, credits and everything else we store about you. "
    "It can't be undone. Send \"delete confirm\" to go ahead."
)
DELETED = "🗑 Done — your data is erased. If you message me again you'll start as a new user."


@dataclass
class Reply:
    text: str
    options: list[tuple[str, str]] | None = None  # quick replies: (title, payload)


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
            await out.send_text(msg.sender, reply.text, reply.options)
        except SendError as e:
            log.warning("reply to %s failed: %s", msg.sender, e)


def _command(msg: Inbound) -> str:
    """A tapped quick reply, or a whole short message that is a command ("credits",
    "delete confirm", "link 123456"), so a question starting with "hi" stays a question."""
    if msg.quick_reply:
        return msg.quick_reply.lower()
    words = msg.text.lower().strip(" .!?").split()
    if len(words) == 1:
        return words[0]
    if len(words) == 2 and words[0] == "link":
        return "link"
    if words == ["delete", "confirm"]:
        return "delete_confirm"
    return ""


async def route(msg: Inbound, account: db.Account, created: bool) -> Reply | None:
    """Do what the message asks; returns the immediate reply (or None)."""
    welcome = WELCOME.format(reels=account.reels_left()) if created else None
    command = _command(msg)

    if command in ("help", "start", "hi", "hello"):
        return Reply(welcome or HELP)
    if command in ("credits", "balance"):
        return await credits_reply(account)
    if command in ("plans", "plan", "upgrade", "subscribe", "pricing"):
        return plans_reply(account)
    if command.startswith("plan_"):
        return await checkout(account, command.removeprefix("plan_"))
    if command in ("cancel", "unsubscribe"):
        return await cancel(account)
    if command in ("new", "reset"):
        await db.reset_conversation(account.id, CHANNEL)
        return Reply("🆕 Fresh start. Share a reel, or ask about anything in your library.")
    if command == "ask":
        return Reply("Sure — type your question about the reel.")
    if command == "link":
        return Reply(await link(msg.text, account))
    if command == "delete":
        return Reply(DELETE_ASK, [("Yes, delete it", "DELETE_CONFIRM"), ("Keep my data", "HELP")])
    if command == "delete_confirm":
        await billing.stop_renewals(account.id)
        await db.delete_account(account.id)
        return Reply(DELETED)

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
        return Reply(welcome or "Send me a reel (➤ Share → send to me) or a reel link, and I'll analyze it.")

    url = cap.find_ig_url(msg.text)
    if url:
        return _with_welcome(welcome, await start_reel(account, msg.sender, cap.extract_shortcode(url), None, "", "apify"))
    if created or not msg.text:
        return Reply(welcome or HELP)
    return await start_question(account, msg.sender, msg.text)


def _with_welcome(welcome: str | None, reply: Reply | None) -> Reply | None:
    if not welcome:
        return reply
    return Reply(f"{welcome}\n\n{reply.text}" if reply else welcome, reply.options if reply else None)


# ---------------------------------------------------------------- credits + plans


def _plan_options() -> list[tuple[str, str]]:
    return [(f"{p.title} ₹{p.price_inr}/mo", f"PLAN_{p.key.upper()}")
            for p in billing.plans().values() if p.razorpay_plan_id]


def plans_text() -> str:
    lines = ["💎 Monthly plans (cancel any time):"]
    for p in billing.plans().values():
        lines.append(f"• {p.title} — ₹{p.price_inr}/month: {p.reels} reels (or {p.units} questions)")
    return "\n".join(lines)


def plans_reply(account: db.Account, prefix: str = "") -> Reply:
    head = f"{prefix}\n\n" if prefix else ""
    if account.unlimited:
        return Reply(head + "♾️ You're on the unlimited plan.")
    if not billing.razorpay_enabled():
        return Reply(f"{head}{PLANS_SOON}\n\n{APP_HINT}")
    return Reply(f"{head}{plans_text()}\n\nTap a plan to get a secure Razorpay payment link.\n{APP_HINT}", _plan_options())


async def credits_reply(account: db.Account) -> Reply:
    if account.unlimited:
        return Reply("♾️ You're on the unlimited plan.")
    units, per_reel = account.units, config.UNITS_PER_REEL
    text = (
        f"🧾 You have {units // per_reel} reels left ({units} credits).\n"
        f"A reel costs {per_reel} credits, a question {config.UNITS_PER_QUESTION}."
    )
    if account.plan in db.PAID_PLANS and account.plan_until:
        sub = await db.live_subscription(account.id)
        renews = "renews" if sub and sub["status"] == "active" else "ends"
        text += f"\n\nPlan: {account.plan.title()} — {renews} {account.plan_until:%d %b %Y}. Send \"cancel\" to stop renewal."
        return Reply(text)
    return plans_reply(account, text)


async def checkout(account: db.Account, plan_key: str) -> Reply:
    plan = billing.plans().get(plan_key)
    if plan is None or not billing.razorpay_enabled():
        return plans_reply(account)
    current = await db.live_subscription(account.id, "razorpay")
    if current and current["status"] == "active":
        return Reply(f"You're already subscribed ({current['plan'].title()}). Send \"cancel\" first to switch plans.")
    try:
        url = await billing.razorpay_checkout(account.id, plan)
    except billing.BillingError as e:
        log.warning("razorpay checkout failed for account %s: %s", account.id, e)
        return Reply("😕 Payments are having trouble right now. Please try again in a few minutes.")
    return Reply(
        f"💎 {plan.title}: ₹{plan.price_inr}/month for {plan.reels} reels.\n\n"
        f"Pay securely with UPI or card here:\n{url}\n\nI'll message you as soon as it's done."
    )


async def cancel(account: db.Account) -> Reply:
    current = await db.live_subscription(account.id, "razorpay")
    if not current or current["status"] != "active":
        if await db.live_subscription(account.id, "play"):
            return Reply("Your plan was bought in the app — cancel it in Google Play → Subscriptions.")
        return Reply("You don't have an active plan to cancel.")
    try:
        await billing.razorpay_cancel(current["external_id"])
    except billing.BillingError as e:
        log.warning("razorpay cancel failed for account %s: %s", account.id, e)
        return Reply("😕 I couldn't cancel right now. Please try again in a few minutes.")
    until = f" until {account.plan_until:%d %b %Y}" if account.plan_until else ""
    return Reply(f"Done — your plan won't renew. You keep it{until}.")


async def link(text: str, account: db.Account) -> str:
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return "Open the Reel Brain app → Profile → Link Instagram, then send me: link 123456"
    merged = await db.link_with_code(parts[1], account.id)
    if not merged:
        return "That code is wrong or expired. Get a new one in the app."
    return "✅ Linked! Reels you share here now show up in the app, and you have one shared balance."


# ---------------------------------------------------------------- jobs


async def _paywall(account: db.Account) -> Reply:
    return plans_reply(account, OUT_OF_CREDITS)


async def start_reel(
    account: db.Account, recipient: str, shortcode: str, media_url: str | None, caption: str, source: str
) -> Reply | None:
    if await db.pending_jobs(account.id) >= config.MAX_PENDING_JOBS:
        return Reply(BUSY)
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
        return await _paywall(account)
    return None if stage == "deliver" else Reply(QUEUED)


async def start_question(account: db.Account, recipient: str, question: str) -> Reply | None:
    if await db.pending_jobs(account.id) >= config.MAX_PENDING_JOBS:
        return Reply(BUSY)
    job = await db.create_job(
        account.id, "chat", "chat", CHANNEL, units=config.UNITS_PER_QUESTION, recipient=recipient,
        payload={"question": question[:2000]}, priority=account.priority,
    )
    return None if job else await _paywall(account)
