"""All settings come from environment variables, so model names and limits
can change without a code change (Render → Environment)."""

import os
from pathlib import Path

from services.spec import CHAT, GEMINI


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


# --- Instagram (Meta "Instagram API with Instagram Login") ---
# App secret: signs webhook payloads (X-Hub-Signature-256).
INSTAGRAM_APP_SECRET = _str("INSTAGRAM_APP_SECRET")
# Any string you choose; Meta echoes it when you register the webhook URL.
INSTAGRAM_VERIFY_TOKEN = _str("INSTAGRAM_VERIFY_TOKEN")
# Long-lived Instagram user token of the bot account (refreshed automatically, stored in the DB).
INSTAGRAM_ACCESS_TOKEN = _str("INSTAGRAM_ACCESS_TOKEN")
GRAPH_API_URL = _str("GRAPH_API_URL", "https://graph.instagram.com/v25.0")
# Instagram-scoped user IDs (IGSIDs) that get the unlimited plan: you and testers.
ADMIN_IG_IDS = set(_list("ADMIN_IG_IDS"))
# Render sets RENDER_EXTERNAL_URL automatically; PUBLIC_URL overrides it.
PUBLIC_URL = _str("PUBLIC_URL") or _str("RENDER_EXTERNAL_URL")
DM_MAX_BYTES = 1000  # Meta's limit per text message
SEND_CONCURRENCY = _int("SEND_CONCURRENCY", 5)

# --- Keys ---
GEMINI_API_KEY = _str("GEMINI_API_KEY")
GROQ_API_KEY = _str("GROQ_API_KEY")
APIFY_TOKEN = _str("APIFY_TOKEN")
# Several keys per provider (comma-separated) are rotated; the singular
# variable still works for one key.
GEMINI_API_KEYS = _list("GEMINI_API_KEYS") or ([GEMINI_API_KEY] if GEMINI_API_KEY else [])
GROQ_API_KEYS = _list("GROQ_API_KEYS") or ([GROQ_API_KEY] if GROQ_API_KEY else [])

# --- Database + accounts ---
DATABASE_URL = _str("DATABASE_URL")  # Neon Postgres connection string
# Direct (non-pooled) connection for LISTEN/NOTIFY; derived from DATABASE_URL if unset.
DATABASE_LISTEN_URL = _str("DATABASE_LISTEN_URL")
APP_SECRET = _str("APP_SECRET")  # signs app login tokens (any long random string)
# OAuth client IDs whose Google ID tokens we accept (Android + web client IDs).
GOOGLE_CLIENT_IDS = _list("GOOGLE_CLIENT_IDS")
MAX_PENDING_JOBS = _int("MAX_PENDING_JOBS", 5)  # per account
CLIENT_UPLOADS_PER_DAY = _int("CLIENT_UPLOADS_PER_DAY", 100)  # phone-made results shared per account
REPORTS_TO_HIDE = _int("REPORTS_TO_HIDE", 3)  # "wrong summary" reports before a reel is re-analyzed
SESSION_DAYS = _int("SESSION_DAYS", 30)

APIFY_TOKENS = _list("APIFY_TOKENS") or ([APIFY_TOKEN] if APIFY_TOKEN else [])

# --- Credits ---
# Balances are whole "units" so a question can cost a fraction of a reel.
UNITS_PER_REEL = _int("UNITS_PER_REEL", 10)  # new or cached, same price
UNITS_PER_FETCH = _int("UNITS_PER_FETCH", 3)  # app users with their own key, server fetch only
UNITS_PER_QUESTION = _int("UNITS_PER_QUESTION", 1)
FREE_UNITS = _int("FREE_UNITS", 20)  # once per person (= 2 reels)

# --- Plans (monthly; units are reset each billing month, no rollover) ---
PLAN_BASIC_UNITS = _int("PLAN_BASIC_UNITS", 300)  # 30 reels
PLAN_BASIC_PRICE_INR = _int("PLAN_BASIC_PRICE_INR", 99)
PLAN_PRO_UNITS = _int("PLAN_PRO_UNITS", 1500)  # 150 reels
PLAN_PRO_PRICE_INR = _int("PLAN_PRO_PRICE_INR", 299)

# --- Razorpay (Instagram users pay through a link in the DM) ---
RAZORPAY_KEY_ID = _str("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = _str("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = _str("RAZORPAY_WEBHOOK_SECRET")
# Plan IDs (plan_…) from scripts/create_razorpay_plans.py or the Razorpay dashboard.
RAZORPAY_PLAN_BASIC = _str("RAZORPAY_PLAN_BASIC")
RAZORPAY_PLAN_PRO = _str("RAZORPAY_PLAN_PRO")
RAZORPAY_TOTAL_COUNT = _int("RAZORPAY_TOTAL_COUNT", 60)  # monthly cycles before the mandate ends

# --- Google Play Billing (app users) ---
PLAY_PACKAGE_NAME = _str("PLAY_PACKAGE_NAME", "app.reelbrain")
PLAY_PRODUCT_BASIC = _str("PLAY_PRODUCT_BASIC", "reelbrain_basic")
PLAY_PRODUCT_PRO = _str("PLAY_PRODUCT_PRO", "reelbrain_pro")
# Service account JSON (one line) with access to the Play Developer API.
PLAY_SERVICE_ACCOUNT_JSON = _str("PLAY_SERVICE_ACCOUNT_JSON")
# Shared secret in the Pub/Sub push URL: /v1/webhooks/play?token=…
PLAY_RTDN_TOKEN = _str("PLAY_RTDN_TOKEN")

# --- Launch / ops ---
CONTACT_EMAIL = _str("CONTACT_EMAIL")  # shown on the privacy page
BOT_USERNAME = _str("BOT_USERNAME")  # the bot's Instagram username, for links on public pages
ADMIN_TOKEN = _str("ADMIN_TOKEN")  # bearer token for /admin/stats

# --- Fetch ---
# Our servers never fetch from Instagram themselves: media comes from the URL
# Meta puts in the webhook, from Apify, or from the user sending the file.
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "apify~instagram-scraper")
APIFY_TIMEOUT = _float("APIFY_TIMEOUT", 60)
APIFY_BATCH = _int("APIFY_BATCH", 10)  # reels per actor run; one run per reel costs more
DOWNLOAD_TIMEOUT = _float("DOWNLOAD_TIMEOUT", 30)
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 100)
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/reel-brain"))

# --- Analysis (Gemini) ---
# First model that answers wins; each has its own free daily quota. 3.5 Flash is
# first: ~10 s per reel vs ~40 s on 3.8 Flash, with the same extraction quality.
# (gemini-2.5-flash is closed to new API keys.)
GEMINI_MODELS = _list("GEMINI_MODELS", ",".join(GEMINI["models"]))
VIDEO_FPS = _float("VIDEO_FPS", GEMINI["video_fps"])
VIDEO_MAX_SECONDS = _int("VIDEO_MAX_SECONDS", GEMINI["video_max_seconds"])
INLINE_LIMIT_MB = _float("INLINE_LIMIT_MB", GEMINI["inline_limit_mb"])
GEMINI_TIMEOUT = _float("GEMINI_TIMEOUT", 120)

# --- Groq (backup analysis + chat) ---
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
GROQ_CHAT_MODELS = _list("GROQ_CHAT_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b")
CHAT_HISTORY_TURNS = _int("CHAT_HISTORY_TURNS", CHAT["history_turns"])
# Rough cap on prompt size (chars ≈ tokens × 4), keeps us under ~4K tokens.
PROMPT_CHAR_BUDGET = _int("PROMPT_CHAR_BUDGET", CHAT["prompt_char_budget"])
SEARCH_TOP_K = _int("SEARCH_TOP_K", CHAT["search_top_k"])
# Plain-text questions go to the last delivered reel for this long, then to the whole library.
ACTIVE_REEL_MINUTES = _int("ACTIVE_REEL_MINUTES", 30)

# --- Concurrency limits (per process) ---
GEMINI_CONCURRENCY = _int("GEMINI_CONCURRENCY", 8)
GROQ_CONCURRENCY = _int("GROQ_CONCURRENCY", 8)
APIFY_CONCURRENCY = _int("APIFY_CONCURRENCY", 5)
# Videos are held in memory while being sent to the AI; this caps RAM use.
ANALYZE_CONCURRENCY = _int("ANALYZE_CONCURRENCY", 4)

# --- Worker / job queue ---
# all: web + worker in one process (default, simplest). For scale, run one or more
# ROLE=web services (Instagram webhook + app API) and ROLE=worker services.
ROLE = _str("ROLE", "all").lower()
WORKER_CONCURRENCY = _int("WORKER_CONCURRENCY", 4)
JOB_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", 3)  # crashes/redeploys before a job is marked failed
JOB_LOCK_TIMEOUT = _int("JOB_LOCK_TIMEOUT", 180)  # s without a heartbeat before a job is taken over
JOB_HEARTBEAT = _int("JOB_HEARTBEAT", 30)
# Idle re-check of the queue. Long, so Neon can scale to zero while nothing happens.
QUEUE_IDLE_POLL = _int("QUEUE_IDLE_POLL", 600)
# Used instead when job notifications can't be received (e.g. only a pooled URL works).
QUEUE_FALLBACK_POLL = _int("QUEUE_FALLBACK_POLL", 5)


REQUIRED = [
    "INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN", "INSTAGRAM_ACCESS_TOKEN",
    "GEMINI_API_KEY", "GROQ_API_KEY", "APIFY_TOKEN", "DATABASE_URL",
]


def missing_required() -> list[str]:
    """Names (never values) of required settings that are unset or empty.
    A key list (e.g. GEMINI_API_KEYS) counts for its singular name."""
    plural = {"GEMINI_API_KEY": "GEMINI_API_KEYS", "GROQ_API_KEY": "GROQ_API_KEYS", "APIFY_TOKEN": "APIFY_TOKENS"}
    return [name for name in REQUIRED if not (_str(name) or _str(plural.get(name, "")))]
