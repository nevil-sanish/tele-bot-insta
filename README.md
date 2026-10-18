# Reel Brain 🧠

A Telegram bot that turns Instagram reels into searchable notes. Send it a reel or post link (or forward a video). It pulls out the caption, speech, on-screen text and key ideas, shows the result in chat, saves it to Postgres, and answers follow-up questions with Groq. The Telegram bot is for whitelisted users; an API with Google sign-in and credits is ready for the upcoming Android app. The video is deleted right after analysis.

```
You ──► Telegram (link or forwarded video)
          │ webhook (200 returned immediately)
          ▼
FastAPI on Render ─ whitelist ─ global reel cache (Postgres) ─ persistent job queue (Postgres)
          │
          ▼
FETCH   Apify (30 s) → yt-dlp (8 s) → Cobalt (8 s, if self-hosted) → "forward me the video"
CAPTION fetcher metadata → instaloader (5 s) → none          (code, no LLM)
ANALYZE Gemini, one call: fps 0.5 · LOW res · first 90 s
        gemini-3.5-flash → 3.8-flash → 3.5-flash-lite → Groq Whisper + LLM (audio-only)
          │
          ▼
delete video ─► save to global cache + user's library ─► show result in chat
          │
          ▼
CHAT    plain text → Groq Q&A on the active reel (last 4 turns)
        /ask       → BM25 over the library → top 3 → Groq
```

## Commands

| Input | What happens |
|---|---|
| Instagram link | Fetch, analyze, save, reply (or a cached result for 0 tokens) |
| Forwarded/uploaded video or photo | Analyzed directly. Put the reel link in the caption to include the original caption |
| Any other text | Question about the active (last) reel |
| `/ask <question>` | Search the whole library and answer with citations |
| `/list` | The 10 most recent saved reels |
| `/retry` | Reprocess your failed reels |
| `/reset` | Clear the active reel and chat history |

If every fetcher fails, the bot remembers the link and asks you to forward the video. The next video you send is linked to that reel.

## Repo layout

```
app/        main.py (FastAPI /webhook, /health) · api.py (Android app API) · bot.py (handlers) · worker.py (queue + pipeline) · config.py
fetchers/   chain.py (ordered fallbacks + timeouts) · apify.py · ytdlp.py · cobalt.py · base.py (shared types, download)
services/   db.py + schema.sql (Postgres) · models.py · caption.py · analyze.py · chat.py · search.py
scripts/    phase0_test.py (measure real reels)
tests/      unit tests (no API keys needed; database tests use a throwaway Postgres)
```

## Setup

### 1. Get the keys

| Variable | Where |
|---|---|
| `TELEGRAM_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `WEBHOOK_SECRET` | Any random string made of `A-Z a-z 0-9 _ -` |
| `ALLOWED_USER_IDS` | Your Telegram user ID. Message the bot once and it replies with your ID |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| `APIFY_TOKEN` | [console.apify.com](https://console.apify.com/settings/integrations) |
| `DATABASE_URL` | [neon.tech](https://neon.tech) → new project → **Connect** → copy the connection string |
| `APP_SECRET` | Any long random string (signs app login tokens). Only needed for the Android app API |
| `GOOGLE_CLIENT_IDS` | OAuth client IDs from Google Cloud Console (Android + web). Only needed for the Android app API |

Copy `.env.example` to `.env` and fill it in.

### 2. Database (Neon Postgres)

Create a free project at [neon.tech](https://neon.tech) and copy its connection string into `DATABASE_URL`. That's all: the bot creates its tables on startup (`services/schema.sql`, safe to run repeatedly).

| Table | What it holds |
|---|---|
| `users` | Telegram users (whitelisted, `unlimited` plan) and app users (Google sign-in, credits) |
| `reels` | Global cache: each reel is analyzed once and shared by everyone who sends it |
| `user_reels` | Each user's library |
| `jobs` | Persistent queue: survives crashes and redeploys |
| `credit_ledger` | Every credit change (signup bonus, analyses) |

### 3. Phase 0: test before deploying (~1 hour)

```bash
# urls.txt: 10 reel links, one per line
python -m scripts.phase0_test urls.txt
```

For each reel it prints which fetcher worked, how long it took, the Gemini model used, and input/output tokens. Then open **AI Studio → Rate limits** and note the requests-per-day (RPD) for each model in `GEMINI_MODELS`.

### 4. Run locally (optional)

```bash
uvicorn app.main:app --reload
# expose it, e.g. `cloudflared tunnel --url http://localhost:8000`, then set PUBLIC_URL to that URL
```

When `PUBLIC_URL` is set, the bot registers its own webhook at startup.

### 5. Deploy on Render (free)

1. Push this repo to GitHub, then in Render create a **New → Blueprint** (it reads `render.yaml`), or a **Web Service** with runtime **Docker**.
2. Add the environment variables from step 1.
3. Deploy. Render sets `RENDER_EXTERNAL_URL`, so the bot calls `setWebhook` for you, with the secret token. To set it by hand instead:
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<app>.onrender.com/webhook&secret_token=<WEBHOOK_SECRET>`
4. Optional: avoid 30–60 s cold starts by pinging `https://<app>.onrender.com/health` every 10 minutes from [cron-job.org](https://cron-job.org).

## Job queue

Reels are processed from the `jobs` table, not from memory:

- **Restarts lose nothing.** On shutdown, running jobs go back to `queued`; a crashed job's heartbeat stops and another worker takes it over after `JOB_LOCK_TIMEOUT` (180 s). After `JOB_MAX_ATTEMPTS` (3) crashes it is marked `failed`.
- **One analysis per reel.** If several people send the same reel at once, only one job analyzes it; the others wait and then get the saved result for free.
- **Neon-friendly.** Workers wake instantly when a job is added and otherwise check every `QUEUE_IDLE_POLL` (10 min), so the database can scale to zero while idle. `/health` never touches the database.

## Android app API

The API under `/api` is ready for the Android app (not built yet). The app signs in with Google, then sends reel links.

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/google` `{id_token}` | Verify a Google ID token; create the account (with `FREE_CREDITS`, default 10) on first sign-in; returns a session token |
| `GET /api/me` | Plan and credits |
| `POST /api/reels` `{url}` | Saved reel → returned at once, 0 credits. New reel → queued; `402` if out of credits, `429` if too many in progress |
| `GET /api/jobs/{id}` | `queued` / `running` / `done` (with the reel) / `failed` (with the reason) |
| `GET /api/library?limit=&offset=` | The user's saved reels, newest first |
| `GET /api/reels/{shortcode}` | One reel from the user's library |

Send the session token as `Authorization: Bearer <token>`. A new analysis costs 1 credit, charged only when it succeeds. Whitelisted Telegram users are on the `unlimited` plan.

## Configuration

Model names and limits are environment variables, so you can change them without editing code:

| Variable | Default |
|---|---|
| `GEMINI_MODELS` | `gemini-3.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite` |
| `GROQ_CHAT_MODELS` | `openai/gpt-oss-120b,openai/gpt-oss-20b` |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` |
| `APIFY_ACTOR` | `apify~instagram-scraper` |
| `VIDEO_FPS` / `VIDEO_MAX_SECONDS` | `0.5` / `90` |
| `YTDLP_ENABLED` | `1` (set `0` to skip yt-dlp) |
| `COBALT_URL`, `COBALT_API_KEY` | unset, so Cobalt is skipped |
| `WORKER_CONCURRENCY` | `2` |

When Google releases newer models, update `GEMINI_MODELS` in Render → Environment; no code change is needed. (`gemini-2.5-flash` is closed to new API keys.)

## Token savers in the code

- **Cache:** a reel that's already in the global `reels` table costs 0 tokens and 0 credits, for every user.
- **Captions, hashtags, URLs, author and date** come from code (fetcher metadata, instaloader, regex), not an LLM.
- **Gemini video settings:** `VideoMetadata(fps=0.5, start_offset="0s", end_offset="90s")` plus `media_resolution=LOW` (66 instead of 258 tokens per frame) and minimal thinking. Gemini downsamples the video, so Render never re-encodes.
- **Inline upload** below 19 MB; larger files go through the Files API and are deleted afterwards.
- **Compact output:** a JSON schema with all fields required, a summary of at most 4 sentences, at most 5 key ideas, and a verbatim transcript only when it's 150 words or fewer.
- **Chat:** Groq with `reasoning_effort="low"`. Each prompt holds the active reel plus the last 4 turns, trimmed to about 14K characters (around 3.5K tokens). `/ask` sends only the BM25 top 3.

## Where this differs from the original plan

- **Gemini clip settings:** these use the SDK's real `VideoMetadata(fps, start_offset, end_offset)`. The `"processing": {"type": "static"}` form in the plan isn't an actual API field.
- **Apify:** called through the `run-sync-get-dataset-items` REST endpoint with httpx. That's one HTTP call and one fewer dependency than `apify-client`.
- **`Analysis` has a third value, `caption-only`:** used for photo posts or silent videos when Gemini is down.
- **The author's hashtags are merged into `Tags`** after Gemini's topic tags (10 at most), so they're searchable.
- **`fetchers/base.py`** holds the shared `FetchResult`/`MediaFile` types and the size-capped downloader.
- **Photo posts and carousels:** Apify image URLs (up to 5) are sent to Gemini as images.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Database tests need a throwaway Postgres (they wipe its `public` schema):

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/reelbrain_test pytest
```

Without `TEST_DATABASE_URL` those tests are skipped. The suite covers caption/shortcode regex, fetcher fallback order and timeouts, BM25 search, chat history trimming, the analysis fallback chain, the job queue (no double analysis of one reel, stale-job takeover, requeue on shutdown, retries), credits (charged once, only on success, never for cached reels), the app API (Google sign-in, 402 when out of credits, users can't see each other's jobs), the full worker pipeline with fakes (the video is deleted on success and on failure), and the webhook secret check.

## Free-tier notes

- Free-tier Gemini may use your inputs to improve Google's products. That's fine for public reels, but avoid sending sensitive videos.
- Apify's $5 monthly credit covers roughly 2,500 reels. After that, the bot falls back to yt-dlp, then Cobalt, then forwarding.
- Telegram bots can download files up to 20 MB.
