# Reel Brain 🧠

Share an Instagram reel with the Reel Brain bot in DMs and get a short summary back. The reel is saved to your library, and you can then ask questions about it, or about anything you've saved. An Android app (coming in Phase 3) uses the same library and works for free with your own AI key.

The design and its reasons are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. The one-time Instagram/Meta setup is in **[docs/SETUP_META.md](docs/SETUP_META.md)**.

```
Instagram user ──DM──► Meta (official Instagram API) ──signed webhook──► FastAPI /webhooks/instagram
                                                                             │ 200 at once, dedupe, then:
                                                                             ▼
                                         app/inbound.py: command · reel · question → reserve credits → job
                                                                             │ Postgres queue + NOTIFY
                                                                             ▼
 FETCH    shared reel: Meta's media URL  ·  link: Apify, batched (≤10 reels per run)  ·  else: "send me the video"
 ANALYZE  Gemini, one call (fps 0.5 · LOW res · first 90 s): 3.5-flash → 3.8-flash → 3.5-flash-lite
          → Groq Whisper + LLM backup; video deleted, text saved to the global reel cache + the user's library
 DELIVER  summary DM (≤1000 bytes) + quick replies; refunds if anything before this failed
 CHAT     question → last reel (30 min) or full-text search over the library → Groq → DM
```

**Ban-proof by design.** The bot uses only Meta's official messaging API and only replies inside the 24-hour window the user opened. Our servers never fetch from Instagram with an account, and there are no fake accounts. See ARCHITECTURE §2.

## What users can send

| DM | What happens |
|---|---|
| A shared reel (➤ Share → the bot) | Analyzed from the media Meta sends; the summary comes back in ~20–60 s |
| A reel link | Looked up on Apify, then analyzed |
| A video file | Analyzed directly. If the bot just asked for this reel's video, the result is filed under that reel |
| Any other text | A question about the last reel (within 30 min), else about the whole library |
| `credits` · `plans` · `cancel` | Balance; plans with a Razorpay payment link; stop renewing |
| `new` · `link 123456` · `delete` · `help` | Start fresh · connect the Android app · erase your data · help |

## Credits and plans

Balances are stored as whole **units**: 1 reel = 10 units, 1 question = 1 unit. Each person gets 20 units free, once.

- **Plans:** monthly Basic (₹99, 30 reels) and Pro (₹299, 150 reels). Unused units don't roll over to the next month.
- **Paying:** Instagram users pay through a Razorpay link the bot DMs (`plans`, then `cancel` to stop renewing). App users pay through Google Play.
- **Pricing rules:** a reel costs the same whether it's new or already in the global cache. Units are held when a job is queued and refunded if it fails before delivery.
- **Records:** every change is a row in `ledger`, and payments are applied exactly once.
- **Own AI key:** app users with their own AI key use the app free. Only a server fetch costs 3 units.
- **Admins:** testers listed in `ADMIN_IG_IDS` are unlimited.

Setup for payments, Google sign-in, Play and Meta App Review is in [docs/SETUP_PAYMENTS.md](docs/SETUP_PAYMENTS.md). Public pages are served at `/privacy`, `/terms` and `/data-deletion`. Operational numbers are at `/admin/stats`.

## Repo layout

```
app/       main.py (webhooks, /health) · inbound.py (DM → action) · pipeline.py (stages) · worker.py (queue loop)
           worker_main.py (ROLE=worker) · api.py (Android app API, /v1) · pages.py (privacy, deletion, stats) · config.py
channels/  instagram.py (signature check, webhook parsing, Send API, token refresh)
fetchers/  apify.py (batched lookup) · base.py (size-capped download)
services/  db.py + schema.sql (Postgres) · billing.py (plans, Razorpay, Google Play) · analyze.py (Gemini/Groq)
           chat.py (Q&A) · keys.py (key pools)
           spec.py (reads spec/analysis.json, shared with the app) · caption.py · models.py
spec/      analysis.json: prompt, fields, video settings (the app bundles an identical copy)
mobile/    Flutter app (in progress, Phase 3)
scripts/   phase0_test.py (real reels end to end) · spike_apify.py (Apify batch cost) · create_razorpay_plans.py
docs/      ARCHITECTURE.md · SETUP_META.md · SETUP_PAYMENTS.md
```

## Setup

1. Keys: [Gemini](https://aistudio.google.com/apikey), [Groq](https://console.groq.com/keys), [Apify](https://console.apify.com/settings/integrations), and a [Neon](https://neon.tech) `DATABASE_URL`.
2. Instagram: follow [docs/SETUP_META.md](docs/SETUP_META.md). It gives you `INSTAGRAM_APP_SECRET`, `INSTAGRAM_VERIFY_TOKEN` and `INSTAGRAM_ACCESS_TOKEN`.
3. Render: create a Web Service from this repo (Docker). Use a **Starter** plan so the webhook never sleeps. Add the variables from `.env.example`, then deploy.
4. In Meta's dashboard, register `https://<app>.onrender.com/webhooks/instagram` and subscribe to `messages`.

Run locally with `uvicorn app.main:app` (put the variables in the environment first). Meta needs a public HTTPS URL, e.g. `cloudflared tunnel --url http://localhost:8000`.

## Job queue

Every reel and question is a row in `jobs`, moving through **fetch → analyze → deliver** (a question is a single **chat** stage):

- **Restarts lose nothing.** On shutdown, running jobs go back to `queued`. A crashed job's heartbeat stops, and another worker takes it over after `JOB_LOCK_TIMEOUT` (180 s). After `JOB_MAX_ATTEMPTS` crashes the job fails and its units are refunded.
- **Stages are independent.** The fetch stage stores media *URLs* rather than files, so the stages of one job can run on different machines.
- **Batching.** A worker that claims a fetch job also takes up to `APIFY_BATCH - 1` more, so many reels share one Apify run.
- **One analysis per reel.** A unique index allows only one running analysis per reel. Anyone else who sends the same reel waits and then gets the cached result.
- **Instant wake-up.** Queuing a job sends a Postgres `NOTIFY`, and workers `LISTEN`. If notifications can't be received, workers poll every 5 s instead.
- **Priority.** Pro jobs run first (10), then basic and unlimited (5), then free (0).

## Scaling

Web and worker processes are stateless, because everything lives in Postgres. `ROLE=all` (the default) runs both in one process. To scale, run `ROLE=web` services plus `ROLE=worker` services from the same image (see `render.yaml`).

- **Key pools:** set comma-separated `GEMINI_API_KEYS`, `GROQ_API_KEYS` and `APIFY_TOKENS`. The least-used key goes first. A rate-limited key rests until the time the API states, and an overloaded model (503) moves to the next model. Usage per key and day goes to `api_usage`, which stores only key hashes.
- **Caps per process:** `WORKER_CONCURRENCY` sets jobs at once; `GEMINI_CONCURRENCY`, `GROQ_CONCURRENCY` and `APIFY_CONCURRENCY` set calls at once per provider; `ANALYZE_CONCURRENCY` sets videos held in memory at once.
- **Measured in Phase 0 (Oct 2026):**
  - Apify: $0.0023 per reel, batched (13 reels in one 13 s run); the returned video URLs download fine from a server.
  - Gemini: ~2.3k input and ~0.3k output tokens per reel; 6–20 s normally, up to ~60 s when it falls back across models under load.

> Use several keys only when each is a separate paid project, or a user's own key. Making many free accounts to get around limits breaks the providers' terms.

## Tests

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/reelbrain_test pytest   # wipes that DB's public schema
```

Without `TEST_DATABASE_URL`, the database tests are skipped. The suite covers:

- **Wallet:** holds, refunds that happen once, unlimited plans, payment-replay safety, and account linking that gives only one free trial.
- **Queue:** stages, batching, no double analysis, stale-job takeover, requeue on shutdown, and NOTIFY wake-up across processes.
- **Pipeline (with fakes):**
  - Media sources: Meta media URL, then Apify fallback, then asking for the video.
  - Paths: cached reels, a closed reply window, and refunds on failure.
  - Videos are deleted afterwards.
- **DM routing:** commands, reels, links, video files, questions, paywall and linking.
- **Instagram client:** webhook signature and dedupe, parsing, splitting to the byte limit, retries, the 24-hour window error and token refresh.
- **Other:** the app API, Q&A, and key-pool rotation.
