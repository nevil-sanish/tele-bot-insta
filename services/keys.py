"""Pools of API keys per provider.

With several keys (separate paid projects, or users' own keys) the pipeline
rotates between them: the least-used available key goes first, a key that
hits a rate limit rests (until the stated retry time, ~1 min for per-minute
limits, until the daily reset for per-day limits) and the next key is tried.
Each pool also caps how many calls run at once.

Keys are never logged or stored; usage stats use a short hash of the key.
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import config

log = logging.getLogger(__name__)

ANY = "*"  # cooldown scope that covers every model


@dataclass
class Key:
    value: str
    id: str
    uses: int = 0
    resting_until: dict[str, float] = field(default_factory=dict)  # scope -> monotonic time

    def available(self, scope: str, now: float) -> bool:
        return self.resting_until.get(scope, 0) <= now and self.resting_until.get(ANY, 0) <= now


def key_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


class KeyPool:
    def __init__(self, provider: str, values: list[str], max_concurrent: int) -> None:
        self.provider = provider
        self.keys = [Key(v, key_id(v)) for v in dict.fromkeys(v for v in values if v)]
        self._max_concurrent = max(1, max_concurrent)
        self._sem: asyncio.Semaphore | None = None

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    def candidates(self, scope: str = ANY) -> list[Key]:
        """Keys that aren't resting for this scope (usually a model), least used first."""
        now = time.monotonic()
        return sorted((k for k in self.keys if k.available(scope, now)), key=lambda k: k.uses)

    def rest(self, key: Key, scope: str, seconds: float) -> None:
        key.resting_until[scope] = time.monotonic() + seconds
        log.warning("%s key %s resting %.0fs for %s", self.provider, key.id, seconds, scope)

    def slot(self) -> asyncio.Semaphore:
        """Concurrency limit for calls to this provider (async context manager)."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)
        return self._sem

    def used(self, key: Key) -> None:
        key.uses += 1


def seconds_until_daily_reset() -> float:
    """Google's per-day quotas reset at midnight Pacific time."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (tomorrow - now).total_seconds()


_RETRY_RE = re.compile(r"(?:retry in|retrydelay['\": ]+|try again in)\s*'?\"?([\d.]+)\s*(ms|s|m)?", re.I)


def rest_seconds(error: Exception) -> float | None:
    """How long to rest a key after this error, or None if it isn't a rate/quota limit."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    msg = str(error).lower()
    if code != 429 and "resource_exhausted" not in msg and "rate limit" not in msg:
        return None
    if any(t in msg for t in ("perday", "per day", "per_day", "(rpd)", "(tpd)", "daily")):
        return seconds_until_daily_reset()
    m = _RETRY_RE.search(msg)
    if m:
        value = float(m.group(1))
        unit = (m.group(2) or "s").lower()
        return max(5.0, value / 1000 if unit == "ms" else value * 60 if unit == "m" else value)
    return 60.0


gemini = KeyPool("gemini", config.GEMINI_API_KEYS, config.GEMINI_CONCURRENCY)
groq = KeyPool("groq", config.GROQ_API_KEYS, config.GROQ_CONCURRENCY)
apify = KeyPool("apify", config.APIFY_TOKENS, config.APIFY_CONCURRENCY)


async def record(provider: str, key: Key, model: str, ok: bool, input_tokens: int = 0, output_tokens: int = 0) -> None:
    """Best-effort usage counter in Postgres (api_usage); never fails the caller."""
    from services import db

    try:
        await db.record_usage(provider, key.id, model, ok, input_tokens or 0, output_tokens or 0)
    except Exception as e:  # no database in tests/scripts, or a transient error
        log.debug("usage not recorded: %s", e)
