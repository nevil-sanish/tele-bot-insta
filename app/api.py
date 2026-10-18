"""REST API for the Android app.

Sign-in: the app gets a Google ID token (Credential Manager) and sends it to
POST /api/auth/google; we verify it with Google and return our own session token
(a signed JWT) for the Authorization: Bearer header.

Credits: a reel already in the global library costs 0. A new analysis costs 1
credit, charged only when it succeeds. New accounts get FREE_CREDITS once.
"""

import asyncio
import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app import config
from services import caption as cap, db

router = APIRouter(prefix="/api", tags=["app"])


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


def issue_session(user_id: int) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + config.SESSION_DAYS * 86400}
    return jwt.encode(payload, config.APP_SECRET, algorithm="HS256")


async def current_user(authorization: str = Header(default="")) -> db.User:
    if not config.APP_SECRET:
        raise HTTPException(503, "App login is not configured on the server")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = jwt.decode(token, config.APP_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired session") from None
    user = await db.get_user(int(claims["sub"]))
    if not user:
        raise HTTPException(401, "Account not found")
    return user


CurrentUser = Annotated[db.User, Depends(current_user)]


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
    user, created = await db.google_user(claims["sub"], claims.get("email"), claims.get("name"))
    return {"token": issue_session(user.id), "user": user.to_dict(), "new_account": created}


@router.get("/me")
async def me(user: CurrentUser) -> dict:
    return user.to_dict()


# ---------------------------------------------------------------- reels


class ReelRequest(BaseModel):
    url: str


@router.post("/reels")
async def analyze_reel(body: ReelRequest, request: Request, user: CurrentUser) -> dict:
    url = cap.find_ig_url(body.url)
    shortcode = cap.extract_shortcode(url)
    if not shortcode:
        raise HTTPException(400, "That isn't an Instagram reel or post link")

    if await db.get_reel(shortcode):  # already analyzed by someone: free
        reel, _ = await request.app.state.worker.submit(user.id, shortcode, url=url)
        return {"status": "done", "credits_used": 0, "reel": reel.to_dict()}

    pending = await db.pending_jobs(user.id)
    if pending >= config.MAX_PENDING_JOBS:
        raise HTTPException(429, "Too many reels in progress. Wait for one to finish.")
    if not user.unlimited and user.credits - pending < 1:
        raise HTTPException(402, "Out of credits")

    reel, job = await request.app.state.worker.submit(user.id, shortcode, url=url)
    if reel:  # analyzed by someone else a moment ago
        return {"status": "done", "credits_used": 0, "reel": reel.to_dict()}
    return {"status": "queued", "job_id": job.id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: int, user: CurrentUser) -> dict:
    job = await db.get_job(job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(404, "Job not found")
    out = {"job_id": job.id, "status": job.status, "shortcode": job.shortcode}
    if job.status == "failed":
        out["error"] = job.error
    if job.status == "done":
        reel = await db.get_reel(job.shortcode)
        out["reel"] = reel.to_dict() if reel else None
    return out


@router.get("/library")
async def my_library(user: CurrentUser, limit: int = 20, offset: int = 0) -> dict:
    reels = await db.library(user.id, limit=min(max(limit, 1), 100), offset=max(offset, 0))
    return {"reels": [r.to_dict() for r in reels]}


@router.get("/reels/{shortcode}")
async def get_reel(shortcode: str, user: CurrentUser) -> dict:
    if not await db.in_library(user.id, shortcode):
        raise HTTPException(404, "Not in your library")
    reel = await db.get_reel(shortcode)
    return reel.to_dict()
