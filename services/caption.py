"""Everything about captions that code (not an LLM) can do: shortcodes,
hashtags, URLs, mentions, and the instaloader fallback."""

import asyncio
import logging
import re

from app import config

log = logging.getLogger(__name__)

IG_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)[^\s]*",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
HASHTAG_RE = re.compile(r"(?<![\w#])#(\w+)", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w@])@([\w.]+)")


def find_ig_url(text: str | None) -> str | None:
    m = IG_URL_RE.search(text or "")
    return m.group(0) if m else None


def extract_shortcode(url: str | None) -> str | None:
    m = IG_URL_RE.search(url or "")
    return m.group(1) if m else None


def canonical_url(shortcode: str) -> str:
    return f"https://www.instagram.com/reel/{shortcode}/"


def extract_hashtags(text: str | None) -> list[str]:
    return _unique(HASHTAG_RE.findall(text or ""))


def extract_urls(text: str | None) -> list[str]:
    return _unique(u.rstrip(".,;:!?") for u in URL_RE.findall(text or ""))


def extract_mentions(text: str | None) -> list[str]:
    return _unique(m.rstrip(".") for m in MENTION_RE.findall(text or ""))


def _unique(items) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _instaloader_caption(shortcode: str) -> dict | None:
    import instaloader

    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        save_metadata=False,
        max_connection_attempts=1,
        request_timeout=config.INSTALOADER_TIMEOUT,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return {
        "caption": post.caption or "",
        "author": post.owner_username,
        "date": post.date_utc.isoformat() if post.date_utc else None,
    }


async def instaloader_caption(shortcode: str) -> dict | None:
    """Caption + author + date via instaloader, or None. Never raises."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_instaloader_caption, shortcode),
            timeout=config.INSTALOADER_TIMEOUT,
        )
    except Exception as e:  # rate limits, login walls, timeouts are all normal here
        log.info("instaloader caption failed for %s: %s", shortcode, e)
        return None
