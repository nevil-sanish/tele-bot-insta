"""All settings come from environment variables, so model names and limits
can change without a code change (Render → Environment)."""

import os
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    """Env value with stray whitespace and wrapping quotes removed (common copy-paste slips)."""
    return (os.getenv(name) or default).strip().strip("'\"").strip()


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in _str(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --- Telegram ---
TELEGRAM_TOKEN = _str("TELEGRAM_TOKEN")
WEBHOOK_SECRET = _str("WEBHOOK_SECRET")
ALLOWED_USER_IDS = {int(x) for x in _list("ALLOWED_USER_IDS") if x.lstrip("-").isdigit()}
# Render sets RENDER_EXTERNAL_URL automatically; PUBLIC_URL overrides it.
PUBLIC_URL = _str("PUBLIC_URL") or _str("RENDER_EXTERNAL_URL")
TELEGRAM_MAX_FILE_MB = 20

# --- Keys ---
GEMINI_API_KEY = _str("GEMINI_API_KEY")
GROQ_API_KEY = _str("GROQ_API_KEY")
APIFY_TOKEN = _str("APIFY_TOKEN")
COBALT_URL = _str("COBALT_URL")
COBALT_API_KEY = _str("COBALT_API_KEY")
NOTION_TOKEN = _str("NOTION_TOKEN")
NOTION_DATABASE_ID = _str("NOTION_DATABASE_ID")

# --- Fetch ---
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "apify~instagram-scraper")
APIFY_TIMEOUT = _float("APIFY_TIMEOUT", 30)
YTDLP_ENABLED = os.getenv("YTDLP_ENABLED", "1") == "1"
YTDLP_TIMEOUT = _float("YTDLP_TIMEOUT", 8)
COBALT_TIMEOUT = _float("COBALT_TIMEOUT", 8)
INSTALOADER_TIMEOUT = _float("INSTALOADER_TIMEOUT", 5)
DOWNLOAD_TIMEOUT = _float("DOWNLOAD_TIMEOUT", 30)
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 100)
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/reel-brain"))

# --- Analysis (Gemini) ---
# First model that answers wins. The "-latest" aliases track Google's current
# Flash / Flash-Lite, so new releases need no redeploy.
GEMINI_MODELS = _list(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash"
)
VIDEO_FPS = _float("VIDEO_FPS", 0.5)
VIDEO_MAX_SECONDS = _int("VIDEO_MAX_SECONDS", 90)
INLINE_LIMIT_MB = _float("INLINE_LIMIT_MB", 19)
GEMINI_TIMEOUT = _float("GEMINI_TIMEOUT", 120)

# --- Groq (backup analysis + chat) ---
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
GROQ_CHAT_MODELS = _list("GROQ_CHAT_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b")
CHAT_HISTORY_TURNS = _int("CHAT_HISTORY_TURNS", 4)
# Rough cap on prompt size (chars ≈ tokens × 4), keeps us under ~4K tokens.
PROMPT_CHAR_BUDGET = _int("PROMPT_CHAR_BUDGET", 14000)
SEARCH_TOP_K = _int("SEARCH_TOP_K", 3)

# --- Worker ---
WORKER_CONCURRENCY = _int("WORKER_CONCURRENCY", 2)


REQUIRED = [
    "TELEGRAM_TOKEN", "WEBHOOK_SECRET", "ALLOWED_USER_IDS", "GEMINI_API_KEY",
    "GROQ_API_KEY", "APIFY_TOKEN", "NOTION_TOKEN", "NOTION_DATABASE_ID",
]


def missing_required() -> list[str]:
    """Names (never values) of required settings that are unset or empty."""
    return [name for name in REQUIRED if not _str(name)]
