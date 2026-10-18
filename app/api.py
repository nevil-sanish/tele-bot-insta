"""REST API for the Android app (/v1).

Sign-in: the app gets a Google ID token (Credential Manager) and sends it to
POST /v1/auth/google; we verify it with Google and return our own session token
(a signed JWT) for the Authorization: Bearer header.

Money: a reel costs UNITS_PER_REEL whether it's new or already in the global
library; a question costs UNITS_PER_QUESTION. Units are held when the job is
queued and refunded if it fails. New accounts get FREE_UNITS once per person.
"""

import asyncio
import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app import config
from services import caption as cap
from services import db

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
