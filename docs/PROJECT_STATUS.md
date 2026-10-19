# Reel Brain: complete project status

**Written:** 18 Oct 2026
**Repository:** `nevil-sanish/tele-bot-insta`
**Read with:** [ARCHITECTURE.md](ARCHITECTURE.md) (the approved design), [SETUP_META.md](SETUP_META.md) (Instagram setup), [SETUP_PAYMENTS.md](SETUP_PAYMENTS.md) (payments, Google, Play, launch).

This document explains everything end to end:

- what the product is and the decisions behind it;
- every part that is built, and how it works;
- how a request flows through the system;
- what is unfinished;
- exactly what you (Nevil) need to do, in order.

---

## Contents

1. [What Reel Brain is](#1-what-reel-brain-is)
2. [Status at a glance](#2-status-at-a-glance)
3. [Final product decisions](#3-final-product-decisions)
4. [Architecture in one picture](#4-architecture-in-one-picture)
5. [Why Instagram can't ban us (ban-proofing)](#5-why-instagram-cant-ban-us-ban-proofing)
6. [Work done, phase by phase](#6-work-done-phase-by-phase)
7. [End-to-end flows, step by step](#7-end-to-end-flows-step-by-step)
8. [Money: credits, plans and payments](#8-money-credits-plans-and-payments)
9. [The database, table by table](#9-the-database-table-by-table)
10. [Code map: every file and what it does](#10-code-map-every-file-and-what-it-does)
11. [Configuration: every environment variable](#11-configuration-every-environment-variable)
12. [Testing](#12-testing)
13. [Deployment and scaling](#13-deployment-and-scaling)
14. [Measured numbers and running costs](#14-measured-numbers-and-running-costs)
15. [Work still to be done: by me (code)](#15-work-still-to-be-done-by-me-code)
16. [Work still to be done: by you (accounts and setup)](#16-work-still-to-be-done-by-you-accounts-and-setup)
17. [Decisions still open](#17-decisions-still-open)
18. [Known limitations and risks](#18-known-limitations-and-risks)
19. [Glossary](#19-glossary)

---

## 1. What Reel Brain is

People save Instagram reels ("5 Python tips", "a 10-minute workout", "how to file taxes") and never find them again. Reel Brain turns each reel into a **searchable note** and lets you **ask questions** about everything you've saved.

There are exactly **two ways to use it**:

| Front door | How it works | Who pays for the AI |
|---|---|---|
| **Instagram bot** (DMs) | Share a reel to the bot account; it replies with a summary and saves it to your library. Ask questions in the same chat. | We do, so it costs the user credits (2 reels free, then a monthly plan). |
| **Android app** | Share a reel to the app, paste a link, or pick a video. Browse and search your library, chat with it. | **Free** if you add your own Gemini/Groq key (the work happens on your phone). Otherwise it uses credits, like the bot. |

Both use the **same account and library** once linked. A reel analyzed once by anyone is stored in a **global cache**, so the same reel is never analyzed twice.

There is **no Telegram bot and no web app**. The old Telegram bot was retired in Phase 1. It is still running on Render from an old deploy, only because the new deploys fail until you add the database.

---

## 2. Status at a glance

| Phase | What | Code status | Where it is | Blocked on you? |
|---|---|---|---|---|
| **P0: spikes** | Test the risky parts before building | ✅ Done, except one check that needs Meta | `main` | One test DM after Meta setup |
| **P1: server core** | Instagram bot, accounts, credits, queue, Q&A | ✅ Done and tested | `main` | Neon + Meta setup to go live |
| **P2: payments** | Razorpay plans in DMs; Google Play verification | ✅ Done and tested | `main` | Razorpay account; Play Console |
| **P3: app server side** | Own-key uploads, reports, server fetch, plans, Play billing API | ✅ Done and tested | `main` (+1 small change on the WIP branch) | Nothing |
| **P3: Android app** | Flutter app: sign-in, library, own-key mode, credits mode, chat, plans, linking | 🟡 **About 80% written; not yet compiled or tested** | Branch `claude/elegant-darwin-b2glb7` | Google Cloud sign-in (only to log in) |
| **P4: launch** | Privacy/terms/deletion pages, Meta deletion callback, admin stats | ✅ Done and tested | `main` | Meta App Review + Business Verification |

**Tests:** 109 server tests pass (108 on `main`, plus 1 on the WIP branch). Flutter tests are not written yet.

**Git branches:**
- `main`: everything finished and tested (Phases 0, 1, 2, 4 and the P3 server endpoints). Render deploys from here.
- `claude/elegant-darwin-b2glb7`: the unfinished Android app, plus one small server change (questions about a specific reel). It gets merged into `main` when the app is finished.

---

## 3. Final product decisions

These came from your answers to the question rounds and were approved with the architecture.

| # | Topic | Decision |
|---|---|---|
| 1 | Who it's for | Creators and marketers, general users, students and learners |
| 2 | Free tier | **2 free reels, once per person** (not every month) |
| 3 | Cached reels | Cost **the same** as a fresh analysis (even though it's free for us) |
| 4 | Questions | Cost **a fraction of a reel** (1 unit vs 10 for a reel) |
| 5 | Meta setup | You had none yet; built from scratch |
| 6 | Bot reply | **Summary only** in the DM; full details in the app |
| 7 | Unfetchable reel | The bot **asks the user to send the video file** |
| 8 | Bot account | A dedicated account that only does DMs (no posting) |
| 9 | App platform | **Android first** (Flutter, so iOS is possible later) |
| 10 | App sign-in | **Google** |
| 11 | App fallback | If the phone can't fetch a reel, the **server fetches it (costs credits)** |
| 12 | Own-key results | **Shared globally** in our database, with safeguards (see §6.6) |
| 13 | Pricing | **Monthly plans only**, **India/INR first**; exact prices not final (defaults ₹99 and ₹299) |

**Assumed defaults** (rounds 5–6 were never asked; change any of them):

| # | Topic | Default |
|---|---|---|
| A1 | Hosting | Render, paid Starter (never sleeps) |
| A2 | Database | Neon Postgres |
| A3 | AI and fetching | Paid Gemini, Groq and Apify, several keys each when scaling |
| A4 | Team | You alone, part-time |
| A5 | Legal entity | Sole proprietor; Meta and Razorpay verification in your name |
| A6 | Telegram bot | Retired (done) |
| A7 | Old Flutter work | Kept and extended (done) |
| A8 | Credit expiry | Plan credits reset each month; no rollover |
| A9 | Retention | Videos deleted right after analysis; only text kept |

---

## 4. Architecture in one picture

```
                         ┌───────────────────────────────┐
 Instagram user ──DM──►  │ Meta: Instagram Messaging API  │  (official API only)
                         └───────────────┬───────────────┘
                                         │ signed webhook
                                         ▼
┌───────────────┐  HTTPS + JWT  ┌─────────────────────────────┐      ┌──────────────────────┐
│ Android app   │ ────────────► │ Web process (FastAPI)        │ ───► │ Postgres (Neon)       │
│ (Flutter)     │ ◄──────────── │ • /webhooks/instagram        │      │ accounts, identities, │
│               │               │ • /webhooks/razorpay, /play  │      │ wallets, ledger,      │
│ own-key mode: │               │ • /v1/* app API              │      │ reels (global cache), │
│ fetch on phone│               │ • /privacy /terms /deletion  │      │ library, jobs, convs, │
│ Gemini/Groq   │               └─────────────┬───────────────┘      │ subscriptions …       │
│ with own key  │                             │ job rows + NOTIFY    └──────────▲───────────┘
└───────────────┘                             ▼                                 │
                              ┌─────────────────────────────┐                   │
                              │ Worker process(es)           │ ──────────────────┘
                              │ fetch → analyze → deliver    │
                              │ chat                         │ ──► Apify · Gemini · Groq
                              └─────────────────────────────┘
 Payments: Razorpay (link in the DM) · Google Play Billing (in the app)
```

**Key ideas:**
- **One backend, two front doors.** Every result goes into the same global `reels` table. Each person's library is a list of pointers into it.
- **The web process never does slow work.** It answers Meta in under 1 second and writes a job to the database. Workers do the slow parts.
- **Everything lives in Postgres**, so any number of web and worker processes can run and restart freely without losing anything.

---

## 5. Why Instagram can't ban us (ban-proofing)

This was your top requirement. The design follows these rules:

1. **Official API only.** The bot uses Meta's *Instagram API with Instagram Login*. No unofficial libraries (instagrapi, instaloader), no logging into Instagram from a server, no browser automation.
2. **Our servers never fetch from Instagram with an account.** Media comes from one of three places:
   - the video link **Meta itself puts in the webhook** when someone shares a reel;
   - **Apify** (a third-party service that uses its own infrastructure);
   - **the user sending the video file.**

   In Phase 1 I deleted the old fetchers that hit Instagram from our server (yt-dlp, Cobalt, instaloader).
3. **Reply only, never initiate.** The bot only answers inside the **24-hour window** the user opened by messaging first. No cold DMs, no bulk messages, no follows, likes or comments.
4. **No fake accounts anywhere.** The app fetches the **public, logged-out** reel page on the user's own phone, like a normal browser visit. It never signs into Instagram.
5. **Rate limits respected.** The sender caps concurrent sends and backs off on Meta's rate-limit error codes.
6. **App Review done properly** (Phase 4 setup): privacy policy, data-deletion callback, screencast.

Worst case, if Meta ever restricted the bot: the app keeps working, and the library is ours.

---

## 6. Work done, phase by phase

### 6.0 Before the final plan (history, for context)

The project started as a **Telegram bot plus Notion**. It was then moved to **Postgres** (accounts, credits, persistent queue) and made scalable (API key pools, separate web and worker roles). When you fixed the final direction ("only Instagram bot + mobile app"), I paused, asked 4 rounds of questions, wrote `ARCHITECTURE.md`, and you approved it. Everything below follows that approved design.

### 6.1 Phase 0: spikes (testing the risky parts first)

| Test | How | Result |
|---|---|---|
| **Apify cost per reel** | `scripts/spike_apify.py`: listed recent reels of a public profile, then fetched 13 of them in **one** batched run | **13 of 13 succeeded in 13 seconds; $0.0023 per reel (about ₹0.19).** The video links Apify returns **download fine from a server** (HTTP 206, video/mp4). |
| **End to end on real reels** | `scripts/phase0_test.py`: 5 live reels through Apify → download → Gemini | **5 of 5 analyzed.** Average **2,300 input tokens and 300 output tokens** per reel. **6–20 s** normally; one took ~64 s because Gemini returned "503 high demand" and the code fell back across models, which proved the fallback works. |
| **What Meta sends for a shared reel** | Needs the Meta app, so not done yet | Meta's docs say a shared reel arrives as an attachment of type `ig_reel` with a title, URL and video ID, but don't pin down the exact field names. The code accepts all the likely names and **logs the field names of every attachment**, so your first test settles it. |
| **Phone fetch success rate** | Needs a real phone | Planned for when the app runs on your phone. |

### 6.2 Phase 1: the server core

#### 6.2.1 Receiving Instagram messages (`app/main.py`, `channels/instagram.py`)

- **`GET /webhooks/instagram`**: Meta's one-time check when you register the webhook. It returns `hub.challenge` only if `hub.verify_token` matches `INSTAGRAM_VERIFY_TOKEN`.
- **`POST /webhooks/instagram`**: every DM arrives here.
  1. **Signature check.** Meta signs each request with your app secret (`X-Hub-Signature-256`). Anything unsigned or wrongly signed gets 403, so nobody can fake messages.
  2. **Parsing** (`instagram.parse`). It reads `entry[].messaging[]` and keeps real messages: text, attachments (`ig_reel`, `share`, `video`, …) and quick-reply taps. It **drops** the bot's own echoes, deleted messages, read receipts and reactions, and sorts by timestamp.
  3. **Dedupe.** Meta retries deliveries, so each message ID is stored in `inbound_events`. A repeat is ignored. The raw payload is kept 7 days for debugging, then pruned.
  4. **Answer Meta at once** (200 OK). The messages are then handled right after, in the background.

#### 6.2.2 Deciding what a DM means (`app/inbound.py`)

Every message first finds or creates the person's **account** from their Instagram ID (a new account gets 20 free units, and its ID is logged for `ADMIN_IG_IDS`). The time of their last message is recorded, because Meta only allows replies for 24 hours after it. Then:

| The DM is… | What happens |
|---|---|
| `help` / `hi` / `start` | Help text (a welcome with free credits for brand-new users) |
| `credits` | Balance in reels and credits. Shows the plan and renewal date if they have one; otherwise the plans. |
| `plans` / `upgrade` | Plan list with **quick-reply buttons** ("Basic ₹99/mo", "Pro ₹299/mo") |
| tapping a plan button | Creates a Razorpay subscription and DMs the **payment link** |
| `cancel` | Stops renewal at the end of the cycle (Razorpay). For Play purchases, it tells them to cancel in Play. |
| `new` / `reset` | Clears the active reel and Q&A history |
| `link 123456` | Joins this Instagram identity to the app account that made the code |
| `delete` | Asks for confirmation (buttons). `delete confirm` erases everything (see §7.9). |
| a **shared reel** (`ig_reel`/`share`) | Queues a reel job using Meta's video link (analyze stage) |
| a **reel link** in text | Queues a reel job that goes through Apify (fetch stage) |
| a **video file** | Analyzes it directly. If the bot had just asked for this reel's video, the result is filed under that reel. |
| any **other text** | It's a **question** (chat job) |

Commands only count when the **whole message** is the command. So "hi, what tools did it use?" is a question, not "hi".

Before queuing anything it checks two things:
- **Busy limit:** at most `MAX_PENDING_JOBS` (5) jobs in progress per person.
- **Credits:** units are **reserved** when the job is created. If there aren't enough, the user gets the **paywall** reply (plans plus the free own-key app option).

#### 6.2.3 The job queue (`services/db.py`, `app/worker.py`)

Each reel and each question is a row in `jobs`. A reel moves through **stages**:

```
fetch ──► analyze ──► deliver          (reel)
chat                                    (question)
fetch (stops here; returns media)       (app user with own key, see §6.6)
```

- **Claiming** uses `FOR UPDATE SKIP LOCKED`, so many workers on many machines never take the same job.
- **Priority:** pro jobs run first (10), then basic and unlimited (5), then free (0).
- **One analysis per reel at a time:** a unique index allows only one *running analyze* per reel. Ten people sharing the same viral reel cause **one** AI call; the rest get the cached result.
- **Heartbeats:** a running job updates `locked_at` every 30 s. If a worker dies, another one takes the job over after 180 s. After 3 crashes the job fails and the **credits are refunded**.
- **Shutdown:** on a redeploy, running jobs go back to `queued` without counting as an attempt.
- **Instant wake-up:** queuing a job sends a Postgres `NOTIFY` and workers `LISTEN`. If that connection isn't possible, workers poll every 5 s.
- **Housekeeping (daily):**
  - refresh the Instagram token (about weekly);
  - prune webhook payloads older than 7 days;
  - move ended paid plans back to free.

#### 6.2.4 What each stage does (`app/pipeline.py`)

**Fetch (batched).** When a worker claims a fetch job, it grabs up to 9 more (`APIFY_BATCH = 10`) and resolves them all in **one Apify run**.
1. A reel someone already analyzed skips straight to **deliver**.
2. A reel Apify found gets its video link, caption, author and date stored in the job. It moves to **analyze**.
3. A reel Apify couldn't find: the job fails, **credits are refunded**, the reel is remembered as "pending", and the bot DMs: *"I couldn't get that reel. Please send me the video itself…"*

**Analyze:**
1. Download the media (size-capped at 100 MB).
2. If Meta's webhook link has expired, it goes back to fetch via Apify (once).
3. Show "typing…" in the DM.
4. Run the AI (`services/analyze.py`). Gemini watches the video in **one call**: 0.5 frames per second, low resolution, first 90 s, minimal thinking. It tries `gemini-3.5-flash`, then `3.8-flash`, then `3.5-flash-lite`, rotating API keys on rate limits.
5. If every Gemini model fails, the **backup**: Groq Whisper transcribes the audio and a Groq text model fills the same JSON (marked `audio-only`).
6. **The video file is deleted**; only text is kept.
7. The reel is saved to the global cache and the person's library, and the job moves to **deliver**.
8. If the AI fails completely: the job fails, credits are refunded, and the bot apologizes.

**Deliver:**
1. Adds the reel to the library and makes it the **active reel** (questions in the next 30 minutes go to it).
2. Sends a **plain-text summary under 1,000 bytes** (Meta's limit): title, author, summary, up to 4 key ideas, tools, links, tags, then "Ask me anything about it 👇".
3. Adds quick replies: *Ask a question*, *My credits*, *Help*.

If Meta refuses because the 24-hour window closed, the reel stays in the library and the charge stands. (They got the reel; they just didn't get the DM.)

**Chat:** answers via `services/chat.py`.
- **Active reel (last 30 min):** it sends that reel's data plus the last 4 Q&A turns to Groq.
- **Otherwise:** it runs a full-text search over **their** library, sends the top 3 reels to Groq, and answers with `[1] [2]` citations listing the sources.

On failure, the credit is refunded.

#### 6.2.5 Sending messages (`channels/instagram.Messenger`)

- Posts to `graph.instagram.com/v25.0/me/messages` with the bot's token.
- Splits long text into parts of at most **1,000 bytes**, breaking at line ends.
- Retries rate limits and temporary errors (Meta codes 1, 2, 4, 17, 32, 341, 613, and 5xx) with back-off. Recognizes the **"outside the 24-hour window"** error.
- A semaphore caps concurrent sends (`SEND_CONCURRENCY`).
- **Token refresh:** the long-lived token lasts 60 days. The worker swaps it for a fresh one about weekly and stores it in the database (`kv` table), so every instance uses it.

#### 6.2.6 Accounts, identities and linking

- An **account** is one person. **Identities** point to it: `ig:<Instagram ID>` and/or `google:<Google ID>`.
- **Linking:**
  1. The app asks for a 6-digit code (valid 10 minutes, single use).
  2. The user DMs `link 123456` to the bot. Sending it from their own Instagram proves they own that Instagram account.
  3. The two accounts **merge**: identities, library, conversations, jobs, ledger and subscriptions all move over. **The better plan wins.**
  4. Balances are added, **minus one free trial** if both sides had one (one free trial per person, not per login).

#### 6.2.7 App API (`app/api.py`, all under `/v1`)

- `POST /v1/auth/google`: verifies a Google ID token (signature, audience, verified email) and returns **our own session token** (a signed JWT, 30 days).
- `GET /v1/me`, `POST /v1/link/code`, `GET /v1/library?q=` (search), `GET /v1/reels/{shortcode}`.
- `POST /v1/jobs` (analyze a link with credits), `POST /v1/chat` (a question), `GET /v1/jobs/{id}` (poll for the result).

### 6.3 Phase 2: payments

**Instagram users pay with Razorpay** (`services/billing.py`):
- **Plans**: Basic ₹99/month for 300 units (30 reels); Pro ₹299/month for 1,500 units (150 reels). Every number is an environment variable.
- **Checkout**: tapping a plan creates a Razorpay **subscription** with `notes = {account_id, plan}` and DMs its hosted **`short_url`** link. The user pays with UPI or card on Razorpay's page.
- **Webhook** `POST /webhooks/razorpay`:
  - **Signature check** (`X-Razorpay-Signature`, HMAC-SHA256 with your webhook secret).
  - **Dedupe** on `x-razorpay-event-id`. If applying an event crashes, its "seen" mark is removed, so Razorpay's retry gets applied.
  - **`subscription.charged`**: one paid month. It is applied exactly once per payment ID (ledger key `razorpay:<payment id>`). The plan is set until the period end, then the bot DMs: *"✅ Payment received — you're on Pro until 18 Nov 2026: 150 reels this month."*
  - **`subscription.halted`** (renewal failed): the bot warns the user.
  - **`subscription.cancelled`**: the bot tells the user when the plan ends.
  - The DM only goes out if they messaged in the last ~23 hours (Meta's rule).
- **Renewal rule (A8):** on a renewal, last month's unused units **expire** and the new month's units are added. On the **first** purchase, leftover free units are kept.
- **Plan end:** a daily job moves plans whose period ended (cancelled or failed renewal) back to **free**. Units already held stay.
- **Setup script:** `scripts/create_razorpay_plans.py` creates both plans in Razorpay and prints their IDs.

**App users pay with Google Play** (Play requires this for in-app digital subscriptions):
- `POST /v1/billing/play {purchase_token}`. The server asks Google's **Play Developer API** about the purchase and checks four things:
  - it's for this account (the app sends the account ID as Play's "obfuscated account ID");
  - the token isn't already claimed by another account;
  - which plan it is;
  - its state and expiry.

  It applies the paid month exactly once per **order ID**, and **acknowledges** the purchase (Google refunds unacknowledged ones after 3 days).
- `POST /v1/webhooks/play?token=…`: Google's **real-time notifications** (renewals, cancellations), pushed via Pub/Sub. Each one re-checks the purchase with Google.

### 6.4 Phase 3, server side: what the app needs

| Endpoint | Purpose |
|---|---|
| `POST /v1/reels/{shortcode}/result` | **Own-key mode:** the phone analyzed the reel with the user's key and shares the result. **Free.** See the safeguards below. |
| `POST /v1/reels/{shortcode}/report` | "This summary is wrong". Counted once per person; **3 reports hide the reel** and queue a fresh **server** analysis. |
| `POST /v1/fetch` | **Own-key mode, phone can't open the reel:** the server finds the media via Apify (**3 units**) and returns the video link for the phone to analyze. **Free if the reel is already analyzed** (it's added to the library and the units are refunded). |
| `GET /v1/plans` | Plans, prices, units and Play product IDs, for the app's Plans screen |
| `POST /v1/billing/play` | Play purchase verification (above) |
| `DELETE /v1/me` | Delete the account (§7.9) |
| `POST /v1/chat {question, shortcode?}` | *(On the WIP branch.)* Ask about a **specific** reel from the app |

**Safeguards on shared own-key results** (decision #12):
- Every field is **validated** (lengths, list sizes, author format, shortcode format), with **100 uploads per account per day**.
- Results are stored with `origin = client` and the uploader's ID.
- **A server analysis always wins**: a phone result never overwrites a server one. If the server already has the reel, its version is returned and added to the library.
- Reports can hide a bad result, as above.
- Videos picked from the phone (not Instagram) get a private key `app_<16 hex>`, so they never collide with real reels.

### 6.5 Phase 4: launch pieces (`app/pages.py`)

- **`/`**: a simple home page with links.
- **`/privacy`**: a full privacy policy. It covers:
  - what we collect, and why;
  - the processors (Meta, Google, Groq, Apify, Razorpay, Render, Neon);
  - retention (videos deleted at once; webhook payloads 7 days);
  - how to delete your data;
  - that it isn't meant for children under 13.
- **`/terms`**: terms of use, covering AI accuracy, content ownership, credits and renewals, and abuse.
- **`/data-deletion`**: how to delete your data, plus a **status lookup** by confirmation code.
- **`POST /meta/data-deletion`**: Meta's **Data Deletion Request callback**. It verifies Meta's `signed_request` (HMAC with the app secret), deletes the account, and returns `{url, confirmation_code}` as Meta requires.
- **Deletion paths:** the DM `delete` command, the app's `DELETE /v1/me` and the Meta callback.
  - All three cancel a renewing Razorpay plan first.
  - They delete the account and everything linked to it, including the raw webhook payloads that person sent.
  - Public reel summaries stay in the shared cache, unlinked from the person.
- **`GET /admin/stats`** (needs `Authorization: Bearer ADMIN_TOKEN`):
  - accounts (total, new in 24 h, by plan) and cached reels;
  - queue size, jobs in the last 24 h (by kind and status, with average time) and failures by stage;
  - today's API usage per provider, and purchases.

### 6.6 Phase 3: the Android app (🟡 in progress, on the WIP branch)

**Already written** (`mobile/`):

| Area | Files | What it does |
|---|---|---|
| Shared spec | `assets/analysis_spec.json`, `lib/core/spec.dart` | **Exactly the same** prompt, fields and video settings as the server (a test checks the two copies match) |
| On-phone fetching | `lib/pipeline/fetcher.dart` | A hidden WebView opens the **public, logged-out** reel page (embed page first). A script reads the video URL, caption and author; the file is downloaded to the cache. |
| On-phone AI | `gemini.dart`, `groq.dart`, `analyzer.dart`, `ocr.dart` | Gemini over REST with the user's key (same settings as the server, inline or Files API upload). Backup: Groq Whisper plus **on-device OCR** (Google ML Kit, from frames every 3 s), then a text model. |
| Local library | `lib/data/library_db.dart` | SQLite with **FTS5 full-text search** (works offline), a local job queue that survives the app being killed, and chat history |
| Job runner | `lib/pipeline/runner.dart` | Three modes, chosen automatically. **Own key:** fetch on the phone, analyze on the phone, delete the video, save, share with the server. **Own key, phone can't open the reel:** server fetch (3 credits, can be turned off), then analyze on the phone. **No key:** the server analyzes for credits, and the phone polls. Also syncs the server library down and retries shares made while offline. |
| Server connection | `lib/data/api_client.dart` | Every `/v1` endpoint, with friendly error messages ("You're out of credits…") and job polling |
| Sign-in | `lib/data/session.dart` | Google sign-in (Credential Manager) to our session token, stored encrypted. Works offline with the cached account; signs out automatically if the server rejects the session. |
| Billing | `lib/data/billing.dart` | Google Play subscriptions: buy, restore, send the purchase to the server, complete the purchase |
| Secrets | `lib/data/key_store.dart` | Android Keystore for the user's AI keys (**never sent to us**), the session token and settings |
| Screens | `lib/ui/*.dart` | **Sign-in**; **Home** (search bar, credits or own-key banner, jobs in progress with Retry / Pick the video / Dismiss, library list, pull to refresh, "Add reel" sheet with paste-link or pick-video); **Reel** details (summary, ideas, tools, links, tags, on-screen text, transcript, caption, open in Instagram, copy, report); **Chat** (one reel or the whole library); **Account**; **Your AI keys**; **Plans** (Play prices, subscribe, restore); **Link Instagram** (code, countdown, copy and open Instagram, auto-check on return) |
| App root | `lib/main.dart` | Opens to Sign-in or Home. Receives reels **shared from Instagram** via the Android share menu; anything shared before signing in is queued until after. |

**Not done yet:** see §15.1.

---

## 7. End-to-end flows, step by step

### 7.1 Someone shares a reel with the bot (the main flow)

1. In Instagram, the user taps **Share → (the bot)** on a reel.
2. Meta POSTs a webhook to `/webhooks/instagram` with an `ig_reel` attachment (video link, title/caption, video ID).
3. The server checks the signature, dedupes the message ID and **answers 200 immediately**.
4. `inbound.handle`:
   1. Finds or creates the account (new users get 20 units).
   2. Records "last message now".
   3. Sees a reel. The reel's **cache key** is its shortcode if known, otherwise `igv_<video id>`.
   4. If the reel is already cached, it queues a **deliver** job and still **reserves 10 units** (decision #3).
   5. Otherwise it reserves **10 units** and queues an **analyze** job with Meta's video link. Not enough units → plans reply.
   6. Replies *"📥 Got it — analyzing…"*.
5. A worker claims the analyze job:
   1. Shows "typing…".
   2. Downloads the video.
   3. Makes one Gemini call (with fallbacks).
   4. Deletes the video.
   5. Saves the reel to the global cache and the user's library.
6. A worker claims the deliver job, makes this the **active reel** and DMs the summary with quick replies. **Done; the 10 units are spent.**
7. Total time: typically **15–60 s**.

### 7.2 Someone pastes a reel link in a DM

Same as 7.1, except the job starts at **fetch**: one Apify run resolves up to 10 queued reels together, then analyze, then deliver.

### 7.3 The reel can't be fetched

1. Apify can't resolve the reel (private, deleted, or blocked). The job fails and **the 10 units are refunded**.
2. The bot DMs: *"I couldn't get that reel. Please send me the video itself…"*. The reel is remembered as **pending**.
3. The user downloads the reel and sends the video file to the bot.
4. The bot sees a video, **takes the pending reel's identity**, reserves 10 units and analyzes it. The result is filed under the original reel, so it joins the global cache properly.

### 7.4 Asking a question in a DM

1. The user types any text that isn't a command.
2. 1 unit is reserved and a **chat** job queued.
3. The worker decides what the question is about:
   - **Active reel** (shown or asked about in the last 30 min): the reel data plus the last 4 turns go to Groq.
   - **Otherwise:** a search of *their* library; the top 3 reels go to Groq, and the answer comes back with `[1] [2]` sources.
4. The reply is DMed (split if it's longer than 1,000 bytes). On failure, the unit is refunded.

### 7.5 Running out of credits and buying a plan (Instagram)

1. A reel or question can't reserve units, so the bot replies: *"You're out of credits."* It adds the plans, quick-reply buttons and the free app option.
2. The user taps **"Pro ₹299/mo"**. The server creates a Razorpay subscription with `notes={account_id, plan}` and DMs the payment link.
3. The user pays on Razorpay's page (UPI autopay or card).
4. Razorpay sends `subscription.charged` to `/webhooks/razorpay`. The server checks the signature, dedupes and applies the month (plan = pro until the period end, +1,500 units).
5. The bot DMs: *"✅ Payment received…"*.
6. **Every month after:** a new `subscription.charged` arrives. The previous month's leftover units expire and 1,500 new units are added.

### 7.6 Cancelling

1. The user DMs `cancel`, and the server calls Razorpay to cancel **at cycle end**.
2. The plan stays until its period ends.
3. The daily housekeeping job then moves the account to **free** (units stay).

Play subscriptions are cancelled in the Play Store; Google's notification updates our records.

### 7.7 Using the app with your own key (free)

1. Sign in with Google.
2. Account → **Your AI keys** → paste a Gemini key (and optionally Groq). The keys are stored encrypted on the phone.
3. In Instagram, tap **Share → Reel Brain** (or paste a link in the app).
4. The app queues the reel locally. It opens the public reel page in a hidden WebView (logged out), finds the video, downloads it, and analyzes it **on the phone** with **your** key, using exactly the same prompt as the server. The video is then deleted.
5. The reel is saved in the local library and **shared with the server** (free). If the server already had a better (server-made) version, that one is kept and shown.
6. If the phone **can't** open the reel: the server fetches the media (3 units, can be turned off in settings) and the phone analyzes it. If that also fails, the app shows **"Pick the video"** so you can choose the downloaded file.
7. Questions are answered **on the phone** with your key.

### 7.8 Using the app without a key (credits)

1. Share or paste a reel. The app calls `POST /v1/jobs`, which reserves 10 units.
2. The server runs fetch → analyze → deliver, exactly like the bot. The app polls `GET /v1/jobs/{id}` and saves the result locally.
3. Questions go through `POST /v1/chat` (1 unit each) and are polled the same way.

### 7.9 Deleting all data

| From | How |
|---|---|
| Instagram | DM `delete` → tap **Yes, delete it** (or send `delete confirm`) |
| App | Account → **Delete account** → confirm |
| Meta | The user removes the app in Instagram settings; Meta calls `/meta/data-deletion` |

Each of these does the following:
1. Cancels a renewing Razorpay plan.
2. Deletes the account, identities, wallet, ledger, library, conversations, jobs, subscriptions, reports, and that person's raw webhook payloads.
3. Leaves public reel summaries in the shared cache, unlinked.

The app also wipes its local database.

### 7.10 Linking the app and the Instagram bot

1. App → Account → **Link Instagram** shows `link 482913` (valid 10 minutes).
2. **Copy & open Instagram** opens a DM with the bot. The user sends the message.
3. The bot merges the two accounts (§6.2.6) and replies *"✅ Linked!"*.
4. Back in the app, the link screen checks, sees Instagram linked, and syncs the library. Reels shared in DMs now show up in the app.

### 7.11 Buying a plan in the app (Google Play)

1. App → **Plans**. Prices come from Play; the user taps **Subscribe** and pays in Google's sheet.
2. The app sends the purchase token to `POST /v1/billing/play`.
3. The server verifies the purchase with Google, applies the month, and acknowledges it.
4. Renewals arrive as Play notifications and are applied the same way.

---

## 8. Money: credits, plans and payments

| Item | Units | Notes |
|---|---|---|
| A reel (new or cached) | **10** | Decision #3: same price either way |
| A question | **1** | A tenth of a reel |
| Server fetch for own-key app users | **3** | Free if the reel is already analyzed |
| Free trial | **20** (2 reels) | Once per **person** (linking doesn't add a second one) |
| Basic plan | **300/month** (30 reels) | ₹99/month (default, not final) |
| Pro plan | **1,500/month** (150 reels) | ₹299/month (default, not final) |
| Unlimited | ∞ | Only for people listed in `ADMIN_IG_IDS` |

**Rules:**
- **Reserve, then refund on failure.** Units are held when the job is created. They are refunded if the job fails **before** delivery; never twice.
- **Every change is a ledger row:**

  | Kind | Meaning |
  |---|---|
  | `grant` | Free trial |
  | `reserve` | Units held for a job |
  | `refund` | Units returned after a failure |
  | `purchase` | A paid month |
  | `expire` | Last month's unused units |
  | `merge` | Accounts linked |
  | `adjust` | Manual change |

  Payments carry an **idempotency key**, so replays can't double-credit.
- **No rollover:** at each renewal, last month's unused units expire.
- **App with your own key is free.**

---

## 9. The database, table by table

All defined in `services/schema.sql`. It is applied automatically on every startup and is safe to run repeatedly.

| Table | One row is… | Important columns |
|---|---|---|
| `accounts` | a person | `plan` (free/basic/pro/unlimited), `plan_until` |
| `identities` | a login pointing to an account | `kind` (ig/google), `external_id` (unique per kind), email, name |
| `link_codes` | a 6-digit linking code | `code`, `account_id`, `expires_at` |
| `wallets` | a balance | `units` (never below 0), `free_granted` |
| `ledger` | one balance change | `delta`, `kind`, `job_id`, `idem_key` (unique) |
| `subscriptions` | a Razorpay or Play subscription | `provider`, `external_id` (sub_… or purchase token), `plan`, `status`, `current_end` |
| `payment_events` | a payment webhook already handled | `(provider, event_id)` |
| `reels` | **one analyzed reel (global cache)** | title, summary, key_ideas, on_screen_text, transcript, tools, tags, category, caption, hashtags, urls, author, `analysis` (full/audio-only/caption-only), `source`, `model`, `origin` (server/client), `uploader_id`, `reports`, `hidden`, `search` (full-text index) |
| `reel_reports` | one person's "wrong summary" report | `(account_id, shortcode)` |
| `library` | "this person saved this reel" | `(account_id, shortcode)`, `via` (ig/app), `added_at` |
| `jobs` | a unit of work | `kind` (reel/chat/fetch), `stage`, `status`, `channel` (ig/app), `recipient`, `shortcode`, `payload`, `priority`, `reserved`, `attempts`, `locked_at` |
| `conversations` | chat state per person per channel | `active_shortcode`, `active_at`, `history`, `pending_shortcode`, `last_inbound_at` |
| `inbound_events` | an Instagram message already handled | `(platform, message_id)`, raw `payload` (7 days) |
| `kv` | a server setting | e.g. the refreshed Instagram token |
| `api_usage` | calls per API key per day | provider, key hash (never the key), model, requests, errors, tokens |

---

## 10. Code map: every file and what it does

```
app/
  main.py          Web entry: Instagram webhook (+ verification), Razorpay and Play webhooks,
                   includes the app API and pages, /health, starts the worker when ROLE=all
  inbound.py       One DM → command / reel / video / question; plans, checkout, cancel, link, delete
  pipeline.py      What each job stage does: fetch (batched Apify), analyze, deliver, chat
  worker.py        Queue loop: claim, heartbeat, requeue on shutdown, LISTEN/NOTIFY, daily housekeeping
  worker_main.py   Standalone worker process (ROLE=worker)
  api.py           Android app API under /v1
  pages.py         /privacy /terms /data-deletion, Meta deletion callback, /admin/stats
  config.py        Every setting, read from environment variables
channels/
  instagram.py     Signature check, webhook parsing, Send API client, text splitting, token refresh
fetchers/
  apify.py         Batched reel lookup via Apify (media URLs + caption metadata)
  base.py          Size-capped downloader and shared types
services/
  db.py            All database access (accounts, wallet, reels, library, conversations, queue, billing)
  schema.sql       The database layout
  billing.py       Plans, Razorpay (checkout, cancel, webhook), Google Play (verify, acknowledge, notifications)
  analyze.py       Gemini video analysis with model/key fallbacks; Groq Whisper backup
  chat.py          Q&A: active reel or library search → Groq
  keys.py          API key pools: least-used first, rest on rate limits, per-model cooldowns
  spec.py          Loads spec/analysis.json (shared with the app)
  caption.py       Shortcodes, hashtags, URLs, mentions (plain code, no AI)
  models.py        The Reel data class
spec/analysis.json The single source of truth for the prompt and fields (the app has an identical copy)
scripts/
  spike_apify.py           P0: Apify batch cost
  phase0_test.py           P0: real reels end to end
  create_razorpay_plans.py Creates the two Razorpay plans
tests/             109 tests (see §12)
docs/              ARCHITECTURE.md · SETUP_META.md · SETUP_PAYMENTS.md · PROJECT_STATUS.md (this file)
mobile/            The Flutter Android app (see §6.6)
Dockerfile         Python 3.12 + ffmpeg; ROLE=worker runs the worker, otherwise the web server
render.yaml        Render service definition and the list of environment variables
.env.example       Every environment variable, with where to get it
```

---

## 11. Configuration: every environment variable

Set these in **Render → reel-brain → Environment**. Never paste keys into chat.

### Required for the bot

| Variable | What | Where from |
|---|---|---|
| `DATABASE_URL` | Neon connection string | Neon → Connect |
| `INSTAGRAM_APP_SECRET` | Verifies Meta's webhook signatures | Meta app → Instagram → API setup with Instagram login |
| `INSTAGRAM_VERIFY_TOKEN` | Any random text; typed again in Meta's webhook form | You make it up |
| `INSTAGRAM_ACCESS_TOKEN` | Bot account's token (refreshed automatically) | Same Meta page → Generate access tokens |
| `GEMINI_API_KEY` (or `GEMINI_API_KEYS`) | Analysis | aistudio.google.com/apikey |
| `GROQ_API_KEY` (or `GROQ_API_KEYS`) | Backup and Q&A | console.groq.com/keys |
| `APIFY_TOKEN` (or `APIFY_TOKENS`) | Fetching reels | console.apify.com → Integrations |

### Optional / launch

| Variable | What |
|---|---|
| `ADMIN_IG_IDS` | Instagram IDs with the unlimited plan (yours) |
| `BOT_USERNAME` | The bot's Instagram username (for links on the public pages) |
| `CONTACT_EMAIL` | Shown on `/privacy` |
| `ADMIN_TOKEN` | Unlocks `/admin/stats` |
| `PUBLIC_URL` | Set automatically by Render |

### Payments

| Variable | What |
|---|---|
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Razorpay API keys |
| `RAZORPAY_WEBHOOK_SECRET` | The secret you type when adding the webhook |
| `RAZORPAY_PLAN_BASIC`, `RAZORPAY_PLAN_PRO` | Printed by `scripts/create_razorpay_plans.py` |
| `PLAY_SERVICE_ACCOUNT_JSON` | Google service account JSON (one line) with Play Console access |
| `PLAY_RTDN_TOKEN` | Random text in the Pub/Sub push URL |
| `PLAY_PACKAGE_NAME`, `PLAY_PRODUCT_BASIC`, `PLAY_PRODUCT_PRO` | Defaults `app.reelbrain`, `reelbrain_basic`, `reelbrain_pro` |
| `PLAN_BASIC_PRICE_INR`, `PLAN_BASIC_UNITS`, `PLAN_PRO_PRICE_INR`, `PLAN_PRO_UNITS` | Defaults 99 / 300 / 299 / 1500 |

### App login

| Variable | What |
|---|---|
| `APP_SECRET` | Any long random text; signs app sessions |
| `GOOGLE_CLIENT_IDS` | Google OAuth client IDs (web + Android) |

### Tuning (defaults are fine)

| Variable | Default | Meaning |
|---|---|---|
| `UNITS_PER_REEL` / `UNITS_PER_QUESTION` / `UNITS_PER_FETCH` / `FREE_UNITS` | 10 / 1 / 3 / 20 | Prices in units |
| `MAX_PENDING_JOBS` | 5 | Jobs in progress per person |
| `CLIENT_UPLOADS_PER_DAY` | 100 | Own-key uploads per person per day |
| `REPORTS_TO_HIDE` | 3 | Reports before a reel is hidden and re-analyzed |
| `ACTIVE_REEL_MINUTES` | 30 | How long questions go to the last reel |
| `GEMINI_MODELS` | 3.5-flash, 3.8-flash, 3.5-flash-lite | Tried in order |
| `GROQ_CHAT_MODELS` | gpt-oss-120b, gpt-oss-20b | Q&A and backup |
| `APIFY_BATCH` | 10 | Reels per Apify run |
| `ROLE` | all | `web` / `worker` when scaling |
| `WORKER_CONCURRENCY` | 4 | Jobs at once per worker |
| `GEMINI_CONCURRENCY` / `GROQ_CONCURRENCY` / `APIFY_CONCURRENCY` / `ANALYZE_CONCURRENCY` / `SEND_CONCURRENCY` | 8 / 8 / 5 / 4 / 5 | Caps per process |
| `JOB_MAX_ATTEMPTS` / `JOB_LOCK_TIMEOUT` / `JOB_HEARTBEAT` | 3 / 180 s / 30 s | Crash handling |
| `GRAPH_API_URL` | https://graph.instagram.com/v25.0 | Meta API version |

---

## 12. Testing

**Run:**
```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/reelbrain_test pytest   # wipes that DB's public schema
```

**What the 109 tests cover:**
- **Wallet:** free trial once; reserve; refund exactly once; unlimited holds nothing; idempotent grants; linking merges with one free trial.
- **Queue:** stages advance; batch claiming; no double analysis; stale-job takeover then give-up with refund; requeue on shutdown; priority; NOTIFY wake-up across processes; fallback to polling.
- **Pipeline (with fakes):**
  - each media source: link → Apify → analyze → deliver; Meta media URL; expired URL → Apify;
  - unfetchable → ask for the video, plus refund;
  - AI failure → refund; cached reel delivered without AI; closed 24-h window keeps the reel and the charge;
  - question answered and charged; fetch-only jobs (paid, and free when cached);
  - videos deleted afterwards.
- **DM routing:**
  - welcome, every command, questions that start with "hi";
  - shared reel, links, cached reel, video after a failed fetch;
  - paywall, busy limit, linking, delete with confirmation.
- **Instagram client:** signature check, parsing, echo/read dropping, byte-limit splitting, quick replies, retry then give up, window-closed error, token refresh.
- **Payments:**
  - Razorpay: signature; DM plans and checkout; charge applied once; renewal expires leftovers; notes fallback; cancel; halt warning; expired plans downgrade; unlimited keeps its plan.
  - Play: purchase granted and acknowledged; restore doesn't double-grant; another account's token rejected; renewal via notification; mismatched account rejected.
- **App API:** sign-in, auth required, job lifecycle, cached reels cost the same, out of credits (402), questions, link code, own-key upload (and server wins), upload limits, reports hide and re-queue, server fetch, plans, delete account, question about a specific reel.
- **Pages:** all public pages render; Meta deletion callback (bad signature rejected, account deleted, status page); admin stats need the token.
- **Other:** Q&A history cap, library search scoped per person, active-reel expiry, key-pool rotation, spec identical in app and server.

**Not tested yet:**
- The Flutter app has no tests (§15.1).
- Nothing has run against real Meta, Razorpay or Play yet; that needs your accounts (§16).

---

## 13. Deployment and scaling

- **Today:** one Render web service from the `Dockerfile` with `ROLE=all` (web plus worker in one process).
- **Render deploys `main` automatically.** New deploys **fail until `DATABASE_URL` is set**; that's why the old Telegram version is still live.
- **To scale:**
  - Run `ROLE=web` services (stateless; add more for traffic).
  - Run `ROLE=worker` services from the same image (add more for throughput).
  - Use several API keys per provider (comma-separated) and paid tiers.
  - The `render.yaml` comments show the worker block.
- **Capacity:** one worker with `WORKER_CONCURRENCY=8` handles about **900 reels/hour**. Most time is spent waiting on APIs, not CPU.
- **Database size:** about 5–10 KB per reel, so about 1 GB per 100,000 reels.

---

## 14. Measured numbers and running costs

| Item | Number |
|---|---|
| Apify per reel (batched) | **$0.0023** (≈ ₹0.19) |
| Gemini tokens per reel | ~2,300 in / ~300 out |
| Time per reel | 6–20 s normally; up to ~60 s when Gemini is overloaded |
| Render (bot server) | Starter ≈ $7/month per service |
| Neon | Free to start; paid when it grows |
| Google Play developer account | $25 once |
| Razorpay | Per-transaction fee (see Razorpay pricing) |

**Rough margin check:** a Basic user paying ₹99 for 30 reels costs about 30 × (₹0.19 Apify + a few paise of Gemini) ≈ **₹6–10** in API costs. That's healthy, before payment fees and hosting.

---

## 15. Work still to be done: by me (code)

### 15.1 Finish the Android app (the main remaining job)

| # | Task | Details |
|---|---|---|
| 1 | **Make the code analyzer-clean** | 1 small style fix left in `gemini.dart`, then re-run `flutter analyze` |
| 2 | **Android settings** (`AndroidManifest.xml`, Gradle) | Add the **share-menu intent filters**, so "Reel Brain" appears in Instagram's Share sheet (text/links and video/*); single-task launch mode for share handling; internet permission; queries for opening Instagram and links; `minSdk` 24; app name "Reel Brain" and icon |
| 3 | **Flutter tests** | Text helpers; spec loading; local database (search, jobs, sync flags); API client against a fake server; runner in all three modes (own key, phone fails → server fetch, credits mode) with fakes; chat context trimming |
| 4 | **Build an APK** | `flutter build apk`. Fix any plugin build problems (e.g. the older `video_thumbnail` plugin may need a replacement for frame grabbing). Produce a debug APK you can install. |
| 5 | **`mobile/README.md`** | How to build: `--dart-define=API_URL=…`, `GOOGLE_SERVER_CLIENT_ID=…`, `BOT_USERNAME=…`; release signing; uploading to Play internal testing |
| 6 | **Merge into `main`** | After 1–5, merge the WIP branch so `main` has everything, including the `/v1/chat` shortcode change |

### 15.2 Server follow-ups

| # | Task | Why |
|---|---|---|
| 7 | **Adjust to Meta's real reel format** | After your first test (§16, step 6), confirm the `ig_reel` field names and update `app/inbound.py` if needed |
| 8 | **OCR in the server's backup path** | The spec says the backup is *Whisper + OCR of frames → Groq*. The server backup currently uses **only Whisper** (the app does have OCR). Add frame grabbing with ffmpeg plus Tesseract OCR, so silent text-only reels still get good summaries when Gemini is down. |
| 9 | **Phone fetch success rate** | Measure on your real phone with ~50 reels (P0 spike) and decide whether to tune the WebView script |
| 10 | **Small polish** | Keep per-person DM order strict when several jobs finish together; optional daily summary for you (from `/admin/stats`) |

---

## 16. Work still to be done: by you (accounts and setup)

In order. Steps 1–7 make the **Instagram bot work** (≈ 1 hour). Detailed clicks are in `SETUP_META.md` and `SETUP_PAYMENTS.md`.

### Now: make the bot live

1. **Neon database.** Sign up at neon.tech → Create project (region: Singapore). Copy the connection string → Render → Environment → `DATABASE_URL`. *Every deploy fails until this is set.*
2. **Render plan.** Switch the service to **Starter**. Free services sleep, and Meta disables webhooks that time out.
3. **Bot Instagram account:**
   - Create a new account (e.g. @reelbrain.ai).
   - Settings → Account type → **Professional → Creator**.
   - Settings → Messages → Message controls → Connected tools → **Allow access to messages: ON**.
4. **Meta app:**
   - developers.facebook.com → Create app → use case **"Manage messaging & content on Instagram"**.
   - Instagram → **API setup with Instagram login**.
   - Generate an access token for the bot account → `INSTAGRAM_ACCESS_TOKEN`.
   - Copy the **Instagram app secret** → `INSTAGRAM_APP_SECRET`.
   - Make up a random `INSTAGRAM_VERIFY_TOKEN`.
   - Add all three in Render and **redeploy**.
5. **Webhook:**
   - Callback URL: `https://<your-render-url>/webhooks/instagram`, with the same verify token → Verify and save.
   - Subscribe to **messages** and **messaging_postbacks**.
   - App roles → add yourself as an **Instagram tester**, then accept the invite in Instagram.
6. **Test**, then send me two log lines:
   - DM the bot "hi", then share a reel with it.
   - In Render → Logs, find `new Instagram user <ID> → account …` and put that ID in `ADMIN_IG_IDS`.
   - Also find `ig attachments from …: [('ig_reel', [...])]` and send me that line.
7. **Small settings:** `BOT_USERNAME`, `CONTACT_EMAIL`, `ADMIN_TOKEN` (any random text).

### Now: security

8. **Rotate your Gemini, Groq and Apify keys.** They were visible in this chat earlier. Create new ones, put them in Render and delete the old ones.

### When you want payments (Instagram)

9. **Razorpay:**
   1. Sign up and complete KYC (PAN + bank). Test mode works before KYC.
   2. Generate API keys → `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`. Make sure **Subscriptions** is enabled.
   3. On your laptop with the keys set: `python -m scripts.create_razorpay_plans` → copy the printed `RAZORPAY_PLAN_BASIC` / `RAZORPAY_PLAN_PRO` lines.
   4. Webhooks → add `https://<your-render-url>/webhooks/razorpay` with a secret (→ `RAZORPAY_WEBHOOK_SECRET`) and all `subscription.*` events.
   5. Test with a test card or UPI: DM `plans`, tap a plan, pay; the bot confirms.
   6. Repeat with **live** keys when ready.

### When you want the app

10. **Google sign-in:**
    1. console.cloud.google.com → new project → OAuth consent screen.
    2. Create a **Web** OAuth client (its ID is the app's `GOOGLE_SERVER_CLIENT_ID`).
    3. Create an **Android** client: package `app.reelbrain` plus your signing key's SHA-1.
    4. Render: `GOOGLE_CLIENT_IDS` = both IDs; `APP_SECRET` = long random text.
11. **Google Play Console** ($25 once):
    1. Create the app and upload the build to **internal testing**.
    2. Create subscriptions `reelbrain_basic` and `reelbrain_pro` (monthly).
    3. Create a **service account** in Google Cloud, invite its email in Play Console (view financial data, manage orders) → `PLAY_SERVICE_ACCOUNT_JSON`.
    4. Create a **Pub/Sub** topic and push subscription to `https://<your-render-url>/v1/webhooks/play?token=<random>` (→ `PLAY_RTDN_TOKEN`), and set the topic in Play → Monetization setup.

### Before public launch

12. **Meta app settings:**
    - Privacy Policy URL `https://<your-render-url>/privacy`.
    - Terms URL `/terms`.
    - Data deletion callback `https://<your-render-url>/meta/data-deletion`.
13. **Meta Business Verification** (needs a document in your or your business's name).
14. **Meta App Review:** request **Advanced Access** for `instagram_business_basic` and `instagram_business_manage_messages`. Include a screencast: DM "hi" → share a reel → summary → ask a question → "delete". *Until approved, only testers can use the bot.*
15. **Play Store listing:** screenshots, description, data-safety form (matching `/privacy`), then production release.

---

## 17. Decisions still open

| # | Question | Default used now |
|---|---|---|
| 1 | Final prices for Basic and Pro | ₹99 / ₹299 (env vars, easy to change) |
| 2 | Monthly hosting budget; Render vs Google Cloud Run | Render Starter |
| 3 | Business identity for Meta and Razorpay verification | Your name (sole proprietor) |
| 4 | Launch date: hackathon demo or public launch? | Not set |
| 5 | Team (Flutter dev, designer)? | You alone |
| 6 | Should free users be able to ask questions? | Yes (1 unit each) |
| 7 | iOS app later? | Android only for now |
| 8 | Languages: English only, or Malayalam/Hindi too? | English output |

---

## 18. Known limitations and risks

| Risk | What it means | Mitigation |
|---|---|---|
| **Meta App Review** | The public can't use the bot until approved | Built to Meta's rules; pages and deletion callback ready |
| **Meta's `ig_reel` format** | Field names not confirmed until your first test | The code accepts likely names and logs real ones; a quick fix after the test |
| **24-hour window** | The bot can't message users who haven't written in 24 h | Jobs finish in about a minute; reels stay in the library anyway |
| **Apify can fail on some reels** | Private, deleted or region-blocked reels | The bot asks for the video file; credits are refunded |
| **Gemini overload (503)** | Slower answers at busy times | Falls back across 3 models, then Groq; seen working in P0 |
| **Phone WebView fetch** | Instagram may show a login wall for some reels | Server-fetch fallback (3 units) or "Pick the video" |
| **Shared own-key results** | Someone could upload a bad summary | Validation, rate limits, reports (3 hide it), server results always win |
| **Free-trial abuse** | People making new Instagram accounts for free reels | Accepted for now (2 reels is small); can add limits later |
| **Old deploy still live** | The old Telegram bot runs until the new deploy works | Fixed by adding `DATABASE_URL` |
| **Keys seen in chat** | Gemini/Groq/Apify keys were exposed earlier | Rotate them (§16, step 8) |
| **Server backup has no OCR yet** | Silent, text-only reels get weaker summaries when Gemini is down | §15.2, task 8 |

---

## 19. Glossary

| Term | Meaning |
|---|---|
| **Reel / shortcode** | An Instagram video post; the shortcode is the ID in its link (`instagram.com/reel/<shortcode>/`) |
| **IGSID** | Instagram-scoped user ID: the number Meta gives our app for each person who DMs the bot |
| **Webhook** | Meta, Razorpay or Google calling *our* server when something happens |
| **24-hour window** | Meta lets a business reply only within 24 h of the user's last message |
| **Unit / credit** | Our balance currency: a reel is 10, a question is 1 |
| **Ledger** | The list of every balance change, for auditing |
| **Own key (BYOK)** | The user's own Gemini/Groq API key, used on their phone, so the app is free for them |
| **Global cache** | The shared `reels` table: a reel analyzed once serves everyone |
| **Stage** | A step of a job: fetch → analyze → deliver, or chat |
| **Worker** | The background process that runs jobs |
| **Apify** | A paid service that fetches public Instagram data using its own infrastructure |
| **Gemini / Groq** | Google's AI that watches the video; Groq runs speech-to-text and fast chat models |
| **RTDN** | Google Play's real-time developer notifications (subscription events) |
| **App Review** | Meta's approval process before the public can use the bot |
