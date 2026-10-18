# Reel Brain 🧠

A Telegram bot that turns Instagram reels into searchable notes. Send it a reel or post link (or forward a video). It pulls out the caption, speech, on-screen text and key ideas, shows the result in chat, saves it to Notion, and answers follow-up questions with Groq. The video is deleted right after analysis.

```
You ──► Telegram (link or forwarded video)
          │ webhook (200 returned immediately)
          ▼
FastAPI on Render ─ whitelist ─ Notion shortcode cache ─ background queue
          │
          ▼
FETCH   Apify (30 s) → yt-dlp (8 s) → Cobalt (8 s, if self-hosted) → "forward me the video"
CAPTION fetcher metadata → instaloader (5 s) → none          (code, no LLM)
ANALYZE Gemini, one call: fps 0.5 · LOW res · first 90 s
        gemini-3.5-flash → 3.8-flash → 3.5-flash-lite → Groq Whisper + LLM (audio-only)
          │
          ▼
delete video ─► save Notion page ─► show result in chat
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
| `/retry` | Reprocess reels saved with `Status = failed` |
| `/reset` | Clear the active reel and chat history |

If every fetcher fails, the bot remembers the link and asks you to forward the video. The next video you send is linked to that reel.

## Repo layout

```
app/        main.py (FastAPI /webhook, /health) · bot.py (handlers) · worker.py (queue + pipeline) · config.py
fetchers/   chain.py (ordered fallbacks + timeouts) · apify.py · ytdlp.py · cobalt.py · base.py (shared types, download)
services/   caption.py · analyze.py · chat.py · search.py · notion_store.py
scripts/    phase0_test.py (measure real reels) · setup_notion.py (create DB properties)
tests/      offline unit tests (no API keys needed)
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
| `NOTION_TOKEN` | [notion.so/profile/integrations](https://www.notion.so/profile/integrations), new internal integration |
| `NOTION_DATABASE_ID` | The 32-character ID in your database's URL |

Copy `.env.example` to `.env` and fill it in.

### 2. Notion database

1. Create an empty full-page database in Notion.
2. In the database's ⋯ menu, open **Connections** and add your integration.
3. Run the setup script, which adds every property (Shortcode, Link, Author, Summary, Tags, Category, Analysis, Source, Status, Date) and renames the title column to `Title`:

```bash
pip install -r requirements.txt
set -a; source .env; set +a
python -m scripts.setup_notion
```

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

- **Cache:** a shortcode that's already in Notion costs 0 tokens.
- **Captions, hashtags, URLs, author and date** come from code (fetcher metadata, instaloader, regex), not an LLM.
- **Gemini video settings:** `VideoMetadata(fps=0.5, start_offset="0s", end_offset="90s")` plus `media_resolution=LOW` (66 instead of 258 tokens per frame) and minimal thinking. Gemini downsamples the video, so Render never re-encodes.
- **Inline upload** below 19 MB; larger files go through the Files API and are deleted afterwards.
- **Compact output:** a JSON schema with all fields required, a summary of at most 4 sentences, at most 5 key ideas, and a verbatim transcript only when it's 150 words or fewer.
- **Chat:** Groq with `reasoning_effort="low"`. Each prompt holds the active reel plus the last 4 turns, trimmed to about 14K characters (around 3.5K tokens). `/ask` sends only the BM25 top 3.

## Where this differs from the original plan

- **Gemini clip settings:** these use the SDK's real `VideoMetadata(fps, start_offset, end_offset)`. The `"processing": {"type": "static"}` form in the plan isn't an actual API field.
- **Apify:** called through the `run-sync-get-dataset-items` REST endpoint with httpx. That's one HTTP call and one fewer dependency than `apify-client`.
- **Notion:** uses API version `2025-09-03` (notion-client 3.x), where pages live in the database's *data source*. This is handled automatically.
- **`Analysis` has a third value, `caption-only`:** used for photo posts or silent videos when Gemini is down.
- **The author's hashtags are merged into `Tags`** after Gemini's topic tags (10 at most), so they're searchable.
- **`fetchers/base.py`** holds the shared `FetchResult`/`MediaFile` types and the size-capped downloader.
- **Photo posts and carousels:** Apify image URLs (up to 5) are sent to Gemini as images.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline. They cover caption/shortcode regex, fetcher fallback order and timeouts, the Notion property/body round trip, BM25 search, chat history trimming, the analysis fallback chain, the full worker pipeline with fakes (the video is deleted on success and on failure), and the webhook secret check.

## Free-tier notes

- Free-tier Gemini may use your inputs to improve Google's products. That's fine for public reels, but avoid sending sensitive videos.
- Apify's $5 monthly credit covers roughly 2,500 reels. After that, the bot falls back to yt-dlp, then Cobalt, then forwarding.
- Telegram bots can download files up to 20 MB.
