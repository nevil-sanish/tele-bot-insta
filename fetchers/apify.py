"""Main fetcher: Apify's Instagram scraper actor (uses its own proxies).

Uses the run-sync-get-dataset-items endpoint: one HTTP call starts the actor,
waits for it and returns the scraped items.
"""

import logging

import httpx

from app import config
from services import keys

from .base import FetchError, FetchResult, MediaFile, download

log = logging.getLogger(__name__)

API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
MAX_IMAGES = 5  # carousel posts: enough for Gemini to "see" the post


def enabled() -> bool:
    return bool(keys.apify)


def _media_urls(item: dict) -> list[tuple[str, str]]:
    """(url, default_mime) pairs to download, video first."""
    if item.get("videoUrl"):
        return [(item["videoUrl"], "video/mp4")]
    children = item.get("childPosts") or []
    videos = [c["videoUrl"] for c in children if c.get("videoUrl")]
    if videos:
        return [(videos[0], "video/mp4")]
    images = item.get("images") or [c.get("displayUrl") for c in children]
    images = [u for u in images if u] or ([item["displayUrl"]] if item.get("displayUrl") else [])
    return [(u, "image/jpeg") for u in images[:MAX_IMAGES]]


async def fetch(url: str) -> FetchResult:
    run_input = {
        "directUrls": [url],
        "resultsType": "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }
    r = await _run_actor(run_input)
    items = [i for i in (r.json() or []) if isinstance(i, dict) and not i.get("error")]
    if not items:
        raise FetchError("apify returned no items")
    item = items[0]

    urls = _media_urls(item)
    if not urls:
        raise FetchError("apify item has no media URL")

    media: list[MediaFile] = []
    try:
        for media_url, mime in urls:
            media.append(await download(media_url, mime))
    except Exception:
        for m in media:
            m.path.unlink(missing_ok=True)
        raise

    return FetchResult(
        source="apify",
        media=media,
        caption=item.get("caption") or "",
        author=item.get("ownerUsername"),
        date=item.get("timestamp"),
    )


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
