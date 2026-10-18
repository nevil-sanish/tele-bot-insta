"""Instagram DMs through Meta's official "Instagram API with Instagram Login".

Only official endpoints, only replies inside the 24-hour window the user
opened, never cold messages: the bot account can't be banned for automation
it doesn't do. See docs/ARCHITECTURE.md §2–3.

- verify_signature(): checks Meta's X-Hub-Signature-256 on every webhook.
- parse(): webhook JSON → Inbound messages (echoes, reads, reactions dropped).
- Messenger: Send API client with retries and a concurrency cap.
"""

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app import config
from services import db

log = logging.getLogger(__name__)

TOKEN_KEY = "instagram_token"
TOKEN_REFRESH_AFTER = timedelta(days=7)  # long-lived tokens last 60 days
# Meta error codes worth retrying: rate limits and temporary errors.
RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613}
WINDOW_CLOSED_SUBCODE = 2534022  # replying more than 24 h after the user's last message


def verify_signature(body: bytes, header: str | None) -> bool:
    if not (config.INSTAGRAM_APP_SECRET and header and header.startswith("sha256=")):
        return False
    expected = hmac.new(config.INSTAGRAM_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


# ---------------------------------------------------------------- inbound


@dataclass
class Attachment:
    type: str  # ig_reel | reel | share | video | image | audio | file | story_mention | …
    url: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class Inbound:
    sender: str  # IGSID
    mid: str
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    quick_reply: str | None = None  # payload of a tapped quick reply or postback button
    timestamp: int = 0


def parse(body: dict) -> list[Inbound]:
    """Messages people sent us, oldest first. Our own echoes and non-message events are skipped."""
    out = []
    if body.get("object") != "instagram":
        return out
    for entry in body.get("entry") or []:
        for ev in entry.get("messaging") or []:
            sender = (ev.get("sender") or {}).get("id")
            if not sender:
                continue
            msg, postback = ev.get("message"), ev.get("postback")
            if msg:
                if msg.get("is_echo") or msg.get("is_deleted") or msg.get("is_unsupported"):
                    continue
                attachments = [
                    Attachment(a.get("type", ""), (a.get("payload") or {}).get("url"), a.get("payload") or {})
                    for a in msg.get("attachments") or []
                ]
                out.append(Inbound(
                    sender=sender, mid=msg.get("mid", ""), text=(msg.get("text") or "").strip(),
                    attachments=attachments, quick_reply=(msg.get("quick_reply") or {}).get("payload"),
                    timestamp=ev.get("timestamp", 0),
                ))
            elif postback:
                out.append(Inbound(
                    sender=sender, mid=postback.get("mid") or f"pb-{sender}-{ev.get('timestamp', 0)}",
                    text=(postback.get("title") or "").strip(), quick_reply=postback.get("payload"),
                    timestamp=ev.get("timestamp", 0),
                ))
    return sorted(out, key=lambda m: m.timestamp)


# ---------------------------------------------------------------- outbound


class SendError(Exception):
    def __init__(self, message: str, retryable: bool = False, window_closed: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.window_closed = window_closed


def split_text(text: str, limit: int = config.DM_MAX_BYTES) -> list[str]:
    """Split into messages of at most `limit` UTF-8 bytes, preferring line breaks."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line.encode()) > limit:  # one very long line: cut it on characters
            cut = len(line.encode()[:limit].decode(errors="ignore"))
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:]
        if len((current + line).encode()) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def quick_replies(options: list[tuple[str, str]]) -> list[dict]:
    """(title, payload) pairs → Meta quick reply objects (titles max 20 chars)."""
    return [{"content_type": "text", "title": t[:20], "payload": p} for t, p in options[:13]]


class Messenger:
    """Send API client. One per process; safe to share between tasks."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=20)
        self._slots = asyncio.Semaphore(config.SEND_CONCURRENCY)
        self._token: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def token(self) -> str:
        if self._token is None:
            stored = await db.kv_get(TOKEN_KEY)
            self._token = stored[0] if stored else config.INSTAGRAM_ACCESS_TOKEN
        if not self._token:
            raise SendError("INSTAGRAM_ACCESS_TOKEN is not set")
        return self._token

    async def send_text(self, recipient: str, text: str, options: list[tuple[str, str]] | None = None) -> None:
        """Send text (split to fit Meta's limit); quick replies go on the last part."""
        parts = split_text(text) or ["…"]
        for i, part in enumerate(parts):
            message: dict = {"text": part}
            if options and i == len(parts) - 1:
                message["quick_replies"] = quick_replies(options)
            await self._post({"recipient": {"id": recipient}, "message": message})

    async def typing(self, recipient: str) -> None:
        """Best effort "typing…" indicator; never raises."""
        try:
            await self._post({"recipient": {"id": recipient}, "sender_action": "typing_on"}, attempts=1)
        except Exception as e:
            log.debug("typing indicator failed: %s", e)

    async def _post(self, body: dict, attempts: int = 3) -> dict:
        delay = 1.0
        for attempt in range(attempts):
            async with self._slots:
                r = await self._client.post(
                    f"{config.GRAPH_API_URL}/me/messages",
                    headers={"Authorization": f"Bearer {await self.token()}"}, json=body,
                )
            if r.status_code < 400:
                return r.json()
            err = _error(r)
            if err.retryable and attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 4
                continue
            raise err
        raise SendError("unreachable")

    async def refresh_token(self, force: bool = False) -> bool:
        """Swap the long-lived token for a fresh 60-day one about weekly; stored in the DB
        so every instance uses it. Returns True if refreshed."""
        stored = await db.kv_get(TOKEN_KEY)
        if stored and not force and datetime.now(UTC) - stored[1] < TOKEN_REFRESH_AFTER:
            return False
        host = config.GRAPH_API_URL.split("/v")[0]
        r = await self._client.get(
            f"{host}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": await self.token()},
        )
        if r.status_code >= 400:
            log.warning("Instagram token refresh failed: %s", _error(r))
            return False
        self._token = r.json()["access_token"]
        await db.kv_set(TOKEN_KEY, self._token)
        log.info("Instagram token refreshed")
        return True


def _error(r: httpx.Response) -> SendError:
    try:
        e = r.json().get("error") or {}
    except ValueError:
        e = {}
    code, sub = e.get("code"), e.get("error_subcode")
    text = f"HTTP {r.status_code} code={code} subcode={sub}: {e.get('message') or r.text[:200]}"
    return SendError(
        text,
        retryable=r.status_code >= 500 or code in RETRYABLE_CODES or e.get("is_transient", False),
        window_closed=sub == WINDOW_CLOSED_SUBCODE,
    )
