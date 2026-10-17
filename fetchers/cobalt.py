"""Optional fetcher: a self-hosted Cobalt instance (video only, no caption).
Skipped unless COBALT_URL is set."""

import httpx

from app import config

from .base import FetchError, FetchResult, download


def enabled() -> bool:
    return bool(config.COBALT_URL)


async def fetch(url: str) -> FetchResult:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if config.COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {config.COBALT_API_KEY}"
    async with httpx.AsyncClient(timeout=config.COBALT_TIMEOUT) as client:
        r = await client.post(config.COBALT_URL, headers=headers, json={"url": url})
    data = r.json() if r.content else {}
    status = data.get("status")

    if status in ("tunnel", "redirect"):
        media_url = data["url"]
    elif status == "picker":
        items = data.get("picker") or []
        video = next((i for i in items if i.get("type") == "video"), None)
        media_url = (video or (items[0] if items else {})).get("url")
    else:
        raise FetchError(f"cobalt: {data.get('error') or status or r.status_code}")
    if not media_url:
        raise FetchError("cobalt: no media URL")

    media = await download(media_url, "video/mp4", timeout=config.COBALT_TIMEOUT * 3)
    return FetchResult(source="cobalt", media=[media], caption=None)
