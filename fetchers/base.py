"""Shared types and helpers for fetchers."""

import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app import config

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


@dataclass
class MediaFile:
    path: Path
    mime_type: str

    @property
    def is_video(self) -> bool:
        return self.mime_type.startswith("video/")


@dataclass
class FetchResult:
    source: str  # apify / yt-dlp / cobalt / forwarded
    media: list[MediaFile] = field(default_factory=list)
    caption: str | None = None  # None = fetcher had no caption info
    author: str | None = None
    date: str | None = None  # ISO 8601

    def cleanup(self) -> None:
        for m in self.media:
            m.path.unlink(missing_ok=True)


def new_tmp_path(suffix: str) -> Path:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    return config.TMP_DIR / f"{uuid.uuid4().hex}{suffix}"


def _guess_mime(url: str, header: str | None, default: str) -> str:
    if header:
        mime = header.split(";")[0].strip().lower()
        if mime.startswith(("video/", "image/")):
            return mime
    guessed, _ = mimetypes.guess_type(url.split("?")[0])
    return guessed or default


async def download(url: str, default_mime: str = "video/mp4", timeout: float | None = None) -> MediaFile:
    """Stream a URL to /tmp, refusing anything bigger than MAX_DOWNLOAD_MB."""
    limit = config.MAX_DOWNLOAD_MB * 1024 * 1024
    timeout = timeout or config.DOWNLOAD_TIMEOUT
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            mime = _guess_mime(url, r.headers.get("content-type"), default_mime)
            ext = mimetypes.guess_extension(mime) or ".bin"
            path = new_tmp_path(ext)
            size = 0
            try:
                with path.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > limit:
                            raise FetchError(f"file larger than {config.MAX_DOWNLOAD_MB} MB")
                        f.write(chunk)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
    if size == 0:
        path.unlink(missing_ok=True)
        raise FetchError("empty download")
    return MediaFile(path, mime)
