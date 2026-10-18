"""Reel lookup through Apify's Instagram scraper actor (Apify's proxies, never ours).

Batched: one actor run resolves many reels (P0 spike: 13 reels in one 13 s run,
$0.0023 per reel). Returns media URLs + caption metadata only; the analyze
stage downloads the media, so lookup and analysis can run on different machines.
"""

import logging
from dataclasses import dataclass, field

import httpx

from app import config
from services import caption as cap
from services import keys

from .base import FetchError

log = logging.getLogger(__name__)

API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
MAX_IMAGES = 5  # carousel posts: enough for Gemini to "see" the post


@dataclass
class ReelMeta:
    shortcode: str
    media: list[tuple[str, str]] = field(default_factory=list)  # (url, mime), video first
    caption: str = ""
    author: str | None = None
    date: str | None = None

    def to_payload(self) -> dict:
        return {"media": self.media, "caption": self.caption, "author": self.author, "date": self.date}


def enabled() -> bool:
    return bool(keys.apify)


def _media_urls(item: dict) -> list[tuple[str, str]]:
    if item.get("videoUrl"):
        return [(item["videoUrl"], "video/mp4")]
    children = item.get("childPosts") or []
    videos = [c["videoUrl"] for c in children if c.get("videoUrl")]
    if videos:
        return [(videos[0], "video/mp4")]
    images = item.get("images") or [c.get("displayUrl") for c in children]
    images = [u for u in images if u] or ([item["displayUrl"]] if item.get("displayUrl") else [])
    return [(u, "image/jpeg") for u in images[:MAX_IMAGES]]


async def lookup(shortcodes: list[str]) -> dict[str, ReelMeta]:
    """Resolve many reels in one actor run. Reels Apify couldn't get are missing from the result."""
    if not shortcodes:
        return {}
    run_input = {
        "directUrls": [cap.canonical_url(s) for s in shortcodes],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }
    r = await _run_actor(run_input)
    wanted, found = set(shortcodes), {}
    for item in r.json() or []:
        if not isinstance(item, dict) or item.get("error"):
            continue
        code = item.get("shortCode") or cap.extract_shortcode(item.get("url"))
        media = _media_urls(item)
        if code in wanted and media:
            found[code] = ReelMeta(
                code, media, item.get("caption") or "", item.get("ownerUsername"), item.get("timestamp")
            )
    log.info("apify lookup: %d/%d reels resolved", len(found), len(wanted))
    return found


async def _run_actor(run_input: dict) -> httpx.Response:
    """Call the actor with the least-used token, rotating past exhausted ones
    (402: monthly credit used up → rest a day; 429: rate limit → rest a minute)."""
    pool = keys.apify
    last = "no Apify token configured"
    for key in pool.candidates():
        async with pool.slot():
            pool.used(key)
            async with httpx.AsyncClient(timeout=config.APIFY_TIMEOUT + 5) as client:
                r = await client.post(
                    API.format(actor=config.APIFY_ACTOR),
                    params={"timeout": int(config.APIFY_TIMEOUT)},
                    headers={"Authorization": f"Bearer {key.value}"},
                    json=run_input,
                )
        if r.status_code in (402, 429):
            await keys.record("apify", key, config.APIFY_ACTOR, ok=False)
            pool.rest(key, keys.ANY, 86400 if r.status_code == 402 else 60)
            last = f"apify HTTP {r.status_code}: {r.text[:200]}"
            continue
        if r.status_code >= 400:
            await keys.record("apify", key, config.APIFY_ACTOR, ok=False)
            raise FetchError(f"apify HTTP {r.status_code}: {r.text[:200]}")
        await keys.record("apify", key, config.APIFY_ACTOR, ok=True)
        return r
    raise FetchError(last if pool else "no Apify token configured")
