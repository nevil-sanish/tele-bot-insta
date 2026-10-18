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
        /ask       → Postgres full-text search over the user's library → top 3 → Groq
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
app/        main.py (FastAPI /webhook, /health) · api.py (Android app API) · bot.py (handlers) · worker.py (queue + pipeline) · worker_main.py (ROLE=worker) · config.py
fetchers/   chain.py (ordered fallbacks + timeouts) · apify.py · ytdlp.py · cobalt.py · base.py (shared types, download)
services/   db.py + schema.sql (Postgres) · keys.py (API key pools) · models.py · caption.py · analyze.py · chat.py
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
- **Instant wake-up, anywhere.** Enqueuing sends a Postgres `NOTIFY`; every worker process `LISTEN`s, so a job queued by any web instance starts within milliseconds. If notifications can't be received (e.g. only a pooled connection works), workers poll every `QUEUE_FALLBACK_POLL` (5 s) instead.
- **Safe across machines.** Jobs are claimed with `FOR UPDATE SKIP LOCKED`, and a unique index allows only one running job per reel, so any number of workers can share the queue.
- **Priority.** Jobs carry the user's plan priority (premium 10, unlimited 5, free 0); higher runs first.
- **Neon-friendly.** Idle workers otherwise check only every `QUEUE_IDLE_POLL` (10 min), and `/health` never touches the database.

## Scaling to thousands of users

```
 Telegram webhook · Android app
            │
   ROLE=web  (1…N, stateless)     cache lookups, API, enqueue + NOTIFY
            │
   Postgres (Neon)                users · reels cache · libraries · jobs · chat state · search · usage
            │  NOTIFY
   ROLE=worker (1…N)              fetch → analyze → save, with key pools and concurrency caps
            │
   Apify · Gemini · Groq          several keys each, rotated
```

Everything that matters lives in Postgres, so web and worker processes can be added or restarted freely. The default `ROLE=all` runs both in one process (what the free Render service uses). To scale, run web services with `ROLE=web` and background workers with `ROLE=worker` (same Docker image; see `render.yaml`).

**Key pools.** Set several comma-separated keys per provider: `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `APIFY_TOKENS` (the singular names still work for one key). The least-used key goes first; a key that hits a rate limit rests until the time the API states (≈1 min for per-minute limits, until the daily reset for per-day limits) and the next key is tried. An overloaded model (503) skips straight to the next model instead of burning other keys. Calls per key and day are counted in `api_usage` (keys appear only as a short hash).

> Use several keys only when each is a legitimately separate paid project, or a user's own key. Creating many free accounts to get around free-tier limits breaks Google's and Groq's terms. At this scale, use paid tiers.

**Concurrency caps (per process).** `WORKER_CONCURRENCY` jobs at once; `GEMINI_CONCURRENCY`, `GROQ_CONCURRENCY`, `APIFY_CONCURRENCY` calls per provider; `ANALYZE_CONCURRENCY` videos held in memory at once (a ≤19 MB video takes ~50 MB while being sent).

**Capacity math.** One new reel takes ~20–45 s, almost all of it waiting on APIs, so a worker is limited by concurrency, not CPU:

| | Estimate |
|---|---|
| Worker with `WORKER_CONCURRENCY=8` | ≈ 8 reels / 30 s ≈ **900 reels/hour** |
| 2,000 active users × 5 reels/day | 10,000 requests/day |
| Cached share (popular reels sent by many users) | often 30–50 % → **5,000–7,000 analyses/day** (≈ 4–5 per minute on average; plan for 10× peaks) |
| Postgres storage | ~5–10 KB per reel → ~1 GB per 100,000 reels |

**Suggested setups** (Render prices are per service per month; API usage is billed separately by each provider):

| Users | Setup |
|---|---|
| Up to ~50 (now) | 1 free web service, `ROLE=all`, Neon free, one key each |
| ~500 | 1 Starter service ($7), `ROLE=all`, `WORKER_CONCURRENCY=6`; paid Gemini key; Apify paid plan |
| ~5,000 | 2 Starter web services (`ROLE=web`) + 2 Standard workers ($25, 2 GB; `WORKER_CONCURRENCY=12`, `ANALYZE_CONCURRENCY=8`); Neon paid plan; 2–3 Gemini paid project keys; Groq paid tier |

The biggest running cost is fetching (Apify ≈ $0.002 per new reel). The global cache, and later on-device fetching in the Android app, are what keep it down.

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
| `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `APIFY_TOKENS` | unset (falls back to the singular variable); comma-separated key pools |
| `ROLE` | `all` (`web` or `worker` when scaling out) |
| `WORKER_CONCURRENCY` | `2` jobs at once per process |
| `GEMINI_CONCURRENCY` / `GROQ_CONCURRENCY` / `APIFY_CONCURRENCY` | `8` / `8` / `5` calls at once per process |
| `ANALYZE_CONCURRENCY` | `4` videos in memory at once |
| `DATABASE_LISTEN_URL` | unset (derived from `DATABASE_URL`, dropping Neon's `-pooler`) |

When Google releases newer models, update `GEMINI_MODELS` in Render → Environment; no code change is needed. (`gemini-2.5-flash` is closed to new API keys.)

## Token savers in the code

- **Cache:** a reel that's already in the global `reels` table costs 0 tokens and 0 credits, for every user.
- **Captions, hashtags, URLs, author and date** come from code (fetcher metadata, instaloader, regex), not an LLM.
- **Gemini video settings:** `VideoMetadata(fps=0.5, start_offset="0s", end_offset="90s")` plus `media_resolution=LOW` (66 instead of 258 tokens per frame) and minimal thinking. Gemini downsamples the video, so Render never re-encodes.
- **Inline upload** below 19 MB; larger files go through the Files API and are deleted afterwards.
- **Compact output:** a JSON schema with all fields required, a summary of at most 4 sentences, at most 5 key ideas, and a verbatim transcript only when it's 150 words or fewer.
- **Chat:** Groq with `reasoning_effort="low"`. Each prompt holds the active reel plus the last 4 turns, trimmed to about 14K characters (around 3.5K tokens). `/ask` sends only the top 3 full-text search hits.

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

Without `TEST_DATABASE_URL` those tests are skipped. The suite covers caption/shortcode regex, fetcher fallback order and timeouts, key-pool rotation (rate limits rest a key, overloads switch model), full-text search scoped to each user, chat state in Postgres, NOTIFY wake-up across processes, the analysis fallback chain, the job queue (no double analysis of one reel, stale-job takeover, requeue on shutdown, retries), credits (charged once, only on success, never for cached reels), the app API (Google sign-in, 402 when out of credits, users can't see each other's jobs), the full worker pipeline with fakes (the video is deleted on success and on failure), and the webhook secret check.

## Free-tier notes

- Free-tier Gemini may use your inputs to improve Google's products. That's fine for public reels, but avoid sending sensitive videos.
- Apify's $5 monthly credit covers roughly 2,500 reels. After that, the bot falls back to yt-dlp, then Cobalt, then forwarding.
- Telegram bots can download files up to 20 MB.
