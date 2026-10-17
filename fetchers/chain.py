"""Run fetchers in order; first success wins. Every fetcher has a hard timeout
so an optional source can never hold up the reply."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app import config

from . import apify, cobalt, ytdlp
from .base import FetchError, FetchResult

log = logging.getLogger(__name__)


@dataclass
class Fetcher:
    name: str
    fetch: Callable[[str], Awaitable[FetchResult]]
    enabled: Callable[[], bool]
    timeout: float


def default_fetchers() -> list[Fetcher]:
    # The timeout here covers the whole step, including the media download.
    return [
        Fetcher("apify", apify.fetch, apify.enabled, config.APIFY_TIMEOUT + config.DOWNLOAD_TIMEOUT),
        Fetcher("yt-dlp", ytdlp.fetch, ytdlp.enabled, config.YTDLP_TIMEOUT + 2),
        Fetcher("cobalt", cobalt.fetch, cobalt.enabled, config.COBALT_TIMEOUT * 4),
    ]


async def fetch(url: str, fetchers: list[Fetcher] | None = None) -> FetchResult:
    """Return the first successful FetchResult, or raise FetchError listing why
    each fetcher failed (the caller then asks the user to forward the video)."""
    errors = []
    for f in fetchers if fetchers is not None else default_fetchers():
        if not f.enabled():
            continue
        try:
            result = await asyncio.wait_for(f.fetch(url), timeout=f.timeout)
            log.info("fetch ok: source=%s url=%s", f.name, url)
            return result
        except asyncio.TimeoutError:
            errors.append(f"{f.name}: timeout")
        except Exception as e:
            errors.append(f"{f.name}: {e}")
        log.warning("fetch failed: %s", errors[-1])
    raise FetchError("; ".join(errors) or "no fetchers enabled")
