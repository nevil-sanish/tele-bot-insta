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

# --- Database + accounts ---
DATABASE_URL = _str("DATABASE_URL")  # Neon Postgres connection string
APP_SECRET = _str("APP_SECRET")  # signs app login tokens (any long random string)
# OAuth client IDs whose Google ID tokens we accept (Android + web client IDs).
GOOGLE_CLIENT_IDS = _list("GOOGLE_CLIENT_IDS")
FREE_CREDITS = _int("FREE_CREDITS", 10)  # given once to each new app account
MAX_PENDING_JOBS = _int("MAX_PENDING_JOBS", 5)  # per app user
SESSION_DAYS = _int("SESSION_DAYS", 30)

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
# First model that answers wins; each has its own free daily quota. 3.5 Flash is
# first: ~10 s per reel vs ~40 s on 3.8 Flash, with the same extraction quality.
# (gemini-2.5-flash is closed to new API keys.)
GEMINI_MODELS = _list("GEMINI_MODELS", "gemini-3.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite")
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

# --- Worker / job queue ---
WORKER_CONCURRENCY = _int("WORKER_CONCURRENCY", 2)
JOB_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", 3)  # crashes/redeploys before a job is marked failed
JOB_LOCK_TIMEOUT = _int("JOB_LOCK_TIMEOUT", 180)  # s without a heartbeat before a job is taken over
JOB_HEARTBEAT = _int("JOB_HEARTBEAT", 30)
# Idle re-check of the queue. Long, so Neon can scale to zero while nothing happens.
QUEUE_IDLE_POLL = _int("QUEUE_IDLE_POLL", 600)


REQUIRED = [
    "TELEGRAM_TOKEN", "WEBHOOK_SECRET", "ALLOWED_USER_IDS", "GEMINI_API_KEY",
    "GROQ_API_KEY", "APIFY_TOKEN", "DATABASE_URL",
]


def missing_required() -> list[str]:
    """Names (never values) of required settings that are unset or empty."""
    return [name for name in REQUIRED if not _str(name)]
