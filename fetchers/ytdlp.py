"""Optional fetcher: yt-dlp. Best effort only, since Instagram usually blocks
data-center IPs. Runs as a subprocess so the timeout really kills it."""

import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime, timezone

from app import config

from .base import FetchError, FetchResult, MediaFile, new_tmp_path

log = logging.getLogger(__name__)


def enabled() -> bool:
    return config.YTDLP_ENABLED


async def fetch(url: str) -> FetchResult:
    base = new_tmp_path("")
    out = base.with_suffix(".mp4")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--quiet", "--no-warnings", "--no-playlist",
        "--socket-timeout", str(int(config.YTDLP_TIMEOUT)),
        "--max-filesize", f"{config.MAX_DOWNLOAD_MB}M",
        "-f", "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/b",
        "--merge-output-format", "mp4",
        "--no-simulate", "--print", "%()j",  # metadata JSON on stdout
        "-o", str(out),
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, err = await asyncio.wait_for(proc.communicate(), timeout=config.YTDLP_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _cleanup(base)
        raise FetchError("yt-dlp timed out") from None

    if proc.returncode != 0 or not out.exists():
        _cleanup(base)
        raise FetchError(f"yt-dlp failed: {err.decode(errors='ignore')[-200:]}")

    info: dict = {}
    for line in reversed(stdout.decode(errors="ignore").splitlines()):
        try:
            info = json.loads(line)
            break
        except ValueError:
            continue

    date = None
    if info.get("timestamp"):
        date = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).isoformat()
    return FetchResult(
        source="yt-dlp",
        media=[MediaFile(out, "video/mp4")],
        caption=info.get("description") or "",
        author=info.get("channel") or info.get("uploader_id") or info.get("uploader"),
        date=date,
    )


def _cleanup(base) -> None:
    for p in base.parent.glob(base.name + "*"):
        if p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
