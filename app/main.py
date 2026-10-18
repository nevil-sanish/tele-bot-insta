"""FastAPI entry point: Instagram and payment webhooks, the app API (/v1),
public pages (privacy, terms, data deletion) and /health."""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app import config, inbound
from app.api import router as api_router
from app.pages import router as pages_router
from app.worker import Worker
from channels import instagram
from services import billing, db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-brain")

_missing = config.missing_required()
if _missing:
    log.warning("missing environment variables: %s", ", ".join(_missing))
if not config.DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set. Add your Neon connection string in Render → Environment.")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    await db.connect()  # also applies the schema
    out = instagram.Messenger()
    worker = Worker(out)  # in ROLE=web it only wakes; worker services process
    fastapi_app.state.out = out
    fastapi_app.state.worker = worker
    if config.ROLE == "all":
        worker.start()  # picks up jobs left over from before a restart
    try:
        yield
    finally:
        await worker.stop()  # running jobs are put back on the queue
        await out.close()
        await db.close()


app = FastAPI(title="Reel Brain", lifespan=lifespan)
app.include_router(api_router)
app.include_router(pages_router)


@app.get("/health")
@app.head("/health")
async def health() -> dict:
    # No database query here, so pings don't keep Neon awake.
    return {"ok": True}


@app.get("/webhooks/instagram")
async def instagram_verify(request: Request) -> PlainTextResponse:
    """Meta's one-time check when you register the webhook URL."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and config.INSTAGRAM_VERIFY_TOKEN and q.get("hub.verify_token") == config.INSTAGRAM_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


@app.post("/webhooks/instagram")
async def instagram_webhook(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    if not instagram.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403)
    payload = json.loads(body)
    fresh = []
    for msg in instagram.parse(payload):
        # Meta retries deliveries; the raw payload is kept a few days to debug message shapes.
        if msg.mid and await db.first_delivery("ig", msg.mid, payload):
            fresh.append(msg)
        if msg.attachments:
            log.info("ig attachments from %s: %s", msg.sender, [(a.type, sorted(a.payload)) for a in msg.attachments])
    # Answer Meta at once; handle the messages right after, in order.
    background.add_task(_handle_all, request.app, fresh)
    return {"ok": True}


async def _handle_all(fastapi_app: FastAPI, messages: list[instagram.Inbound]) -> None:
    for msg in messages:
        try:
            await inbound.handle(msg, fastapi_app.state.out)
        except Exception:
            log.exception("handling message %s failed", msg.mid)
    if messages:
        fastapi_app.state.worker.wake()


# ---------------------------------------------------------------- payments


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    if not billing.verify_razorpay_signature(body, request.headers.get("X-Razorpay-Signature")):
        raise HTTPException(403)
    event = json.loads(body)
    event_id = request.headers.get("x-razorpay-event-id") or f"{event.get('event')}:{event.get('created_at')}"
    if not await db.first_payment_event("razorpay", event_id, event):
        return {"ok": True}  # a retry of something we already applied
    try:
        account_id, message = await billing.handle_razorpay(event)
    except Exception:
        await db.forget_payment_event("razorpay", event_id)  # let Razorpay's retry apply it
        raise
    if account_id and message:
        background.add_task(_notify, request.app, account_id, message)
    return {"ok": True}


@app.post("/v1/webhooks/play")
async def play_webhook(request: Request, token: str = "") -> dict:
    """Google Play real-time developer notifications (Pub/Sub push subscription)."""
    if not config.PLAY_RTDN_TOKEN or token != config.PLAY_RTDN_TOKEN:
        raise HTTPException(403)
    body = await request.json()
    message_id = (body.get("message") or {}).get("messageId") or ""
    if message_id and not await db.first_payment_event("play", message_id, body):
        return {"ok": True}
    try:
        await billing.handle_play_notification(body)
    except billing.BillingError as e:
        log.warning("play notification failed: %s", e)  # permanent (bad token, other account): don't retry
    except Exception:
        if message_id:
            await db.forget_payment_event("play", message_id)  # temporary: Pub/Sub retries
        raise
    return {"ok": True}


async def _notify(fastapi_app: FastAPI, account_id: int, text: str) -> None:
    """Tell the person on Instagram, if Meta still lets us message them."""
    recipient = await db.ig_recipient(account_id)
    if recipient:
        try:
            await fastapi_app.state.out.send_text(recipient, text)
        except instagram.SendError as e:
            log.warning("payment notice to %s failed: %s", recipient, e)
