"""REST API for the Android app (/v1).

Sign-in: the app gets a Google ID token (Credential Manager) and sends it to
POST /v1/auth/google; we verify it with Google and return our own session token
(a signed JWT) for the Authorization: Bearer header.

Money: a reel costs UNITS_PER_REEL whether it's new or already in the global
library; a question costs UNITS_PER_QUESTION. Units are held when the job is
queued and refunded if it fails. New accounts get FREE_UNITS once per person.

Own-key mode (free): the phone fetches and analyzes the reel itself and uploads
the result (POST /v1/reels/{shortcode}/result). If the phone can't fetch it,
POST /v1/fetch has the server find the media for UNITS_PER_FETCH.
"""

import asyncio
import re
import time
from typing import Annotated, Literal

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from services import billing, db
from services import caption as cap
from services.models import Reel

router = APIRouter(prefix="/v1", tags=["app"])
CHANNEL = "app"


# ---------------------------------------------------------------- auth


def verify_google_token(token: str) -> dict:
    """Verify a Google ID token's signature, expiry, issuer and audience."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(token, google_requests.Request())
    if claims.get("aud") not in config.GOOGLE_CLIENT_IDS:
        raise ValueError("token was issued for a different app")
    if not claims.get("email_verified", False):
        raise ValueError("Google email not verified")
    return claims


def issue_session(account_id: int) -> str:
    now = int(time.time())
    payload = {"sub": str(account_id), "iat": now, "exp": now + config.SESSION_DAYS * 86400}
    return jwt.encode(payload, config.APP_SECRET, algorithm="HS256")


async def current_account(authorization: str = Header(default="")) -> db.Account:
    if not config.APP_SECRET:
        raise HTTPException(503, "App login is not configured on the server")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = jwt.decode(token, config.APP_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired session") from None
    account = await db.get_account(int(claims["sub"]))
    if not account:
        # Linking from the app side keeps this account; this only happens after a deletion.
        raise HTTPException(401, "Account not found")
    return account


CurrentAccount = Annotated[db.Account, Depends(current_account)]


class GoogleLogin(BaseModel):
    id_token: str


@router.post("/auth/google")
async def login_google(body: GoogleLogin) -> dict:
    if not (config.APP_SECRET and config.GOOGLE_CLIENT_IDS):
        raise HTTPException(503, "Google login is not configured on the server")
    try:
        claims = await asyncio.to_thread(verify_google_token, body.id_token)
    except Exception as e:
        raise HTTPException(401, f"Google sign-in failed: {e}") from None
    account, created = await db.google_account(claims["sub"], claims.get("email"), claims.get("name"))
    return {"token": issue_session(account.id), "account": account.to_dict(), "new_account": created}


@router.get("/me")
async def me(account: CurrentAccount) -> dict:
    ids = await db.identities(account.id)
    return {**account.to_dict(), "linked": sorted({i["kind"] for i in ids})}


@router.delete("/me")
async def delete_me(account: CurrentAccount) -> dict:
    """Erase the account and everything linked to it (Play subscriptions are cancelled in Google Play)."""
    await billing.stop_renewals(account.id)
    await db.delete_account(account.id)
    return {"deleted": True}


@router.post("/link/code")
async def link_code(account: CurrentAccount) -> dict:
    """Code for the user to DM to the bot ("link 123456") to join their Instagram to this account."""
    return {"code": await db.new_link_code(account.id), "expires_in": db.LINK_CODE_MINUTES * 60}


# ---------------------------------------------------------------- reels


class ReelRequest(BaseModel):
    url: str


async def _queue(account: db.Account, request: Request, **job) -> db.Job:
    if await db.pending_jobs(account.id) >= config.MAX_PENDING_JOBS:
        raise HTTPException(429, "Too much in progress. Wait for something to finish.")
    created = await db.create_job(account.id, channel=CHANNEL, priority=account.priority, **job)
    if created is None:
        raise HTTPException(402, "Out of credits")
    request.app.state.worker.wake()
    return created


@router.post("/jobs")
async def analyze_reel(body: ReelRequest, request: Request, account: CurrentAccount) -> dict:
    shortcode = cap.extract_shortcode(cap.find_ig_url(body.url))
    if not shortcode:
        raise HTTPException(400, "That isn't an Instagram reel or post link")
    stage = "deliver" if await db.get_reel(shortcode) else "fetch"
    job = await _queue(account, request, kind="reel", stage=stage, units=config.UNITS_PER_REEL, shortcode=shortcode)
    return {"job_id": job.id, "status": job.status}


class Question(BaseModel):
    question: str


@router.post("/chat")
async def ask(body: Question, request: Request, account: CurrentAccount) -> dict:
    """Queue a question (about the last reel, else the library); poll /v1/jobs/{id} for `reply`."""
    if not body.question.strip():
        raise HTTPException(400, "Empty question")
    job = await _queue(
        account, request, kind="chat", stage="chat", units=config.UNITS_PER_QUESTION,
        payload={"question": body.question[:2000]},
    )
    return {"job_id": job.id, "status": job.status}


@router.get("/jobs/{job_id}")
async def job_status(job_id: int, account: CurrentAccount) -> dict:
    job = await db.get_job(job_id)
    if not job or job.account_id != account.id:
        raise HTTPException(404, "Job not found")
    out = {"job_id": job.id, "kind": job.kind, "stage": job.stage, "status": job.status, "shortcode": job.shortcode}
    if job.status == "failed":
        out["error"] = job.error
    if job.status == "done" and job.kind == "reel":
        reel = await db.get_reel(job.shortcode)
        out["reel"] = reel.to_dict() if reel else None
    if job.status == "done" and job.kind == "chat":
        out["reply"] = job.payload.get("reply")
    if job.status == "done" and job.kind == "fetch":
        if job.payload.get("cached"):
            reel = await db.get_reel(job.shortcode)
            out["reel"] = reel.to_dict() if reel else None
        else:
            out["media"] = [{"url": u, "mime": m} for u, m in job.payload.get("media") or []]
            out.update({k: job.payload.get(k) for k in ("caption", "author", "date")})
    return out


@router.get("/library")
async def my_library(account: CurrentAccount, q: str = "", limit: int = 20, offset: int = 0) -> dict:
    limit = min(max(limit, 1), 100)
    if q.strip():
        reels = await db.search_library(account.id, q, limit=limit)
    else:
        reels = await db.library(account.id, limit=limit, offset=max(offset, 0))
    return {"reels": [r.to_dict() for r in reels]}


@router.get("/reels/{shortcode}")
async def get_reel(shortcode: str, account: CurrentAccount) -> dict:
    if not await db.in_library(account.id, shortcode):
        raise HTTPException(404, "Not in your library")
    reel = await db.get_reel(shortcode)
    if not reel:
        raise HTTPException(404, "Reel not available")
    return reel.to_dict()


# ---------------------------------------------------------------- own-key mode


SHORTCODE_RE = re.compile(r"^(?:[A-Za-z0-9_-]{5,40}|app_[0-9a-f]{16})$")  # Instagram, or a file picked on the phone
Short = Annotated[str, Field(max_length=300)]


class ClientResult(BaseModel):
    """A reel analyzed on the phone with the user's own key (same fields as spec/analysis.json)."""

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    key_ideas: list[Short] = Field(default_factory=list, max_length=10)
    on_screen_text: str = Field(default="", max_length=5000)
    transcript: str = Field(default="", max_length=20000)
    tools: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=20)
    tags: list[Annotated[str, Field(max_length=50)]] = Field(default_factory=list, max_length=15)
    category: str = Field(default="", max_length=50)
    caption: str = Field(default="", max_length=5000)
    author: str = Field(default="", max_length=60, pattern=r"^[\w.]*$")
    date: str | None = Field(default=None, max_length=40)
    analysis: Literal["full", "audio-only", "caption-only"] = "full"
    model: str = Field(default="", max_length=100)


@router.post("/reels/{shortcode}/result")
async def upload_result(shortcode: str, body: ClientResult, account: CurrentAccount) -> dict:
    """Share a phone-made analysis. Free. If the server already has this reel, its version is
    kept and returned (and added to the library) — the app should show what comes back."""
    if not SHORTCODE_RE.match(shortcode):
        raise HTTPException(400, "Bad shortcode")
    if await db.client_uploads_today(account.id) >= config.CLIENT_UPLOADS_PER_DAY:
        raise HTTPException(429, "Daily upload limit reached")
    is_ig = not shortcode.startswith("app_")
    hashtags = cap.extract_hashtags(body.caption)
    reel = Reel(
        shortcode=shortcode, link=cap.canonical_url(shortcode) if is_ig else "",
        title=body.title, summary=body.summary, key_ideas=body.key_ideas[:5], on_screen_text=body.on_screen_text,
        transcript=body.transcript, tools=body.tools,
        tags=list(dict.fromkeys([t.lower().lstrip("#") for t in body.tags] + [h.lower() for h in hashtags]))[:10],
        category=body.category.lower(), caption=body.caption, hashtags=hashtags, urls=cap.extract_urls(body.caption),
        author=body.author, date=body.date, analysis=body.analysis, source="phone", model=body.model,
    )
    stored = await db.save_client_reel(account.id, reel)
    return stored.to_dict()


class Report(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/reels/{shortcode}/report")
async def report(shortcode: str, body: Report, request: Request, account: CurrentAccount) -> dict:
    """"This summary is wrong." Enough reports hide the reel and queue a fresh server analysis."""
    if not await db.in_library(account.id, shortcode):
        raise HTTPException(404, "Not in your library")
    hidden = await db.report_reel(account.id, shortcode, body.reason, config.REPORTS_TO_HIDE)
    if hidden and not shortcode.startswith("app_"):
        await db.create_job(account.id, "reel", "fetch", CHANNEL, units=0, shortcode=shortcode, priority=0)
        request.app.state.worker.wake()
    return {"reported": True, "reanalyzing": hidden}


@router.post("/fetch")
async def server_fetch(body: ReelRequest, request: Request, account: CurrentAccount) -> dict:
    """The phone couldn't fetch a reel: find its media on the server (poll /v1/jobs/{id} for
    `media`). Free if the reel turns out to be analyzed already (`cached`)."""
    shortcode = cap.extract_shortcode(cap.find_ig_url(body.url))
    if not shortcode:
        raise HTTPException(400, "That isn't an Instagram reel or post link")
    job = await _queue(account, request, kind="fetch", stage="fetch", units=config.UNITS_PER_FETCH, shortcode=shortcode)
    return {"job_id": job.id, "status": job.status}


# ---------------------------------------------------------------- plans + Google Play


@router.get("/plans")
async def list_plans() -> dict:
    return {
        "unit": {"reel": config.UNITS_PER_REEL, "question": config.UNITS_PER_QUESTION, "fetch": config.UNITS_PER_FETCH},
        "plans": [
            {"key": p.key, "title": p.title, "price_inr": p.price_inr, "units": p.units, "reels": p.reels,
             "play_product_id": p.play_product_id}
            for p in billing.plans().values()
        ],
    }


class PlayPurchase(BaseModel):
    purchase_token: str = Field(min_length=10, max_length=1000)


@router.post("/billing/play")
async def play_purchase(body: PlayPurchase, account: CurrentAccount) -> dict:
    """Called by the app after a Google Play purchase (and on app start, to restore)."""
    if not billing.play_enabled():
        raise HTTPException(503, "Google Play billing is not configured on the server")
    try:
        updated = await billing.play_sync(account.id, body.purchase_token)
    except billing.BillingError as e:
        raise HTTPException(400, str(e)) from None
    return updated.to_dict()
