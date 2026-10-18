"""Everything about captions that code (not an LLM) can do: shortcodes,
hashtags, URLs and mentions."""

import re

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
