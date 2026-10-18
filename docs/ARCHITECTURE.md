# Reel Brain — Architecture Spec (v1 draft, for approval)

Status: **approved (Oct 2026).** All code phases are built; what's left is setup with Nevil's accounts
(Meta, Neon, Razorpay, Google Cloud/Play) — see SETUP_META.md and SETUP_PAYMENTS.md. Details in §10.1–10.3.
Scope is fixed to two platforms: an **Instagram DM bot** and an **Android app** (Flutter).
No Telegram bot, no web app.

---

## 0. Decisions so far

| # | Topic | Decision | Source |
|---|-------|----------|--------|
| 1 | Audience | Creators & marketers, general users, students & learners | Round 1 |
| 2 | Free tier | **2 free reels, once per person** (not monthly) | Round 1 |
| 3 | Cached reels | **Cost the same as a fresh analysis** | Round 1 |
| 4 | Chat / Q&A | Costs credits: **a fraction of a reel** | Rounds 1, 4 |
| 5 | Meta setup | None yet (no Business Manager, no app, no pro account) | Round 2 |
| 6 | DM reply | **Summary only** (full details in the app) | Round 2 |
| 7 | Unfetchable reel | **Ask the user to send the video file** | Round 2 |
| 8 | Bot account | A dedicated account that is **only a DM bot** (no posting) | Round 2 |
| 9 | App platform | **Android first** | Round 3 |
| 10 | App sign-in | **Google** | Round 3 |
| 11 | App fallback | If the phone can't fetch → **server fetch, costs credits** | Round 3 |
| 12 | BYOK results | **Shared globally** in our DB | Round 3 |
| 13 | Pricing | **Monthly plans only**, **INR / India first**, price per credit TBD | Round 4 |

### Assumed defaults (rounds 5–6 not asked yet — change any of these)

| # | Topic | Default I'll design for | Why |
|---|-------|-------------------------|-----|
| A1 | Hosting | Render: 1 paid web + 1 paid worker (no free tier) | Meta disables webhooks that time out; free tier sleeps |
| A2 | Database | Neon Postgres (free → Launch plan at ~1k users) | Already built on Postgres |
| A3 | AI / fetch | Gemini paid tier + Groq + Apify paid, multiple keys | Free tiers can't carry paying users |
| A4 | Team | You alone, part-time | Drives scope of v1 |
| A5 | Legal entity | Sole proprietor for now; Meta Business Verification + Razorpay KYC in your name | Both need *some* verified identity |
| A6 | Telegram bot | **Retired in P1**: the Render service is repurposed for the Instagram webhook | Final scope says no Telegram |
| A7 | Stashed WIP | Keep `spec/analysis.json` + Flutter pipeline pieces; drop the rest | Shared output schema is still useful |
| A8 | Credits expiry | Plan credits reset each billing month (no rollover) | Simplest to account for |
| A9 | Data retention | Videos deleted right after analysis; only text kept | Lower storage, lower legal risk |

---

## 1. System overview

```
                        ┌──────────────────────────────┐
 Instagram user ──DM──▶ │ Meta (Instagram Messaging API)│
                        └──────────────┬───────────────┘
                                       │ webhook (signed)
                                       ▼
┌──────────────┐  HTTPS/JWT   ┌──────────────────────┐    ┌────────────────┐
│ Android app  │ ───────────▶ │  API / webhook (web) │──▶ │ Postgres (Neon)│
│ (Flutter)    │ ◀─────────── │  FastAPI, stateless  │    │ accounts,      │
│              │              └──────────────────────┘    │ wallet, ledger,│
│ local:       │                                          │ reels (global),│
│  WebView fetch│                                          │ library, jobs  │
│  ML Kit OCR  │              ┌──────────────────────┐    └───────▲────────┘
│  Gemini/Groq │              │  Workers (N)         │ ───────────┘
│  (BYOK)      │              │  fetch → analyze →   │
└──────────────┘              │  deliver ; chat      │──▶ Apify, Gemini, Groq
                              └──────────────────────┘
 Payments: Razorpay (IG users, link in DM) · Google Play Billing (in app)
```

One backend, two front doors. Every result lands in the same global `reels` table; each person's "library" is a list of pointers into it.

---

## 2. Ban-proofing (the non-negotiables)

1. **Official API only.** The bot uses Meta's *Instagram API with Instagram Login* (messaging). No unofficial libraries (instagrapi etc.), no logging into the bot account from a server, no scraping with the bot account.
2. **We never fetch from Instagram with our account.** Media comes from (a) the media URL Meta includes in the webhook for a shared reel, (b) Apify, or (c) the user uploading the file.
3. **Only reply, never initiate.** We answer inside the 24-hour window the user opened. No cold DMs, no bulk messages, no follows/likes/comments.
4. **No fake accounts anywhere.** The app fetches logged-out public pages on the user's own phone; it never logs in to Instagram.
5. **Respect Meta's rate limits** with a per-account send queue (token bucket) and back-off on error codes.
6. **App Review done properly**: privacy policy, data-deletion callback, screencast. Advanced Access for `instagram_business_manage_messages`.

Worst case if Meta ever restricts the bot: the app keeps working on its own, and the DB/library is ours.

---

## 3. Instagram bot

### 3.1 Setup (one-time, needs you — step by step in [SETUP_META.md](SETUP_META.md))
1. New Instagram account → switch to **Professional (Creator or Business)**.
2. Meta developer app → add **Instagram** product (Instagram Login flavour) → connect the account.
3. Webhook subscription: `messages`, `messaging_postbacks`, `message_reactions` (optional).
4. Business Verification + App Review for Advanced Access (until then, only accounts added as app testers can use the bot — fine for beta).

### 3.2 Inbound flow
```
Meta webhook ─▶ verify X-Hub-Signature-256 ─▶ dedupe on message id ─▶ 200 OK (< 1 s)
           └─▶ enqueue: classify(message)
```
Classification:
| Message | Action |
|---|---|
| Shared reel (`ig_reel` attachment) | `fetch` job with the payload media URL + reel id |
| Text with an instagram.com/reel link | `fetch` job (Apify path) |
| Video attachment (after we asked for it) | `analyze` job using that file, matched to the pending reel |
| `link 123456` | link this Instagram identity to an app account |
| `credits` / `plan` / `upgrade` | balance + Razorpay payment link |
| `new` | clear the active reel |
| `help` | short help |
| Any other text | Q&A job (see 3.4) |

### 3.3 Reel pipeline (stage queue)
```
fetch ──▶ analyze ──▶ deliver
  │          │
  │          └─ fallbacks: Gemini video → (Whisper + OCR frames → Groq LLM)
  └─ fallbacks: webhook media URL → Apify (batched) → ask user to send the video
```
- **Cache first:** if `reels[shortcode]` exists → skip to deliver (still charged, decision #3).
- **One analysis per reel at a time** (existing unique-running index).
- **Reserve → charge → refund:** credits are reserved when the job is created, charged on delivery, refunded on failure. No credits → reply with plan options instead of queueing.
- **Deliver:** "typing" indicator while working → summary DM (≤ 1000 chars) + quick replies: *Ask a question* · *Open in app* · *New reel*.
- Message order per user is preserved by a per-recipient send queue.

### 3.4 Q&A in DM
- If the user has an **active reel** (last delivered, < 30 min ago, not reset) → answer about that reel.
- Otherwise → full-text search over **their library**, top 3 reels → answer with `[1] [2]` citations.
- Costs a fraction of a credit (see §5). History: last 4 turns, stored per conversation.

### 3.5 What the bot can't do (Meta limits)
- Can't reply after 24 h of user silence → long jobs must finish well within that (target < 2 min).
- Text messages are capped (~1000 chars) → summary only, details in the app.
- Can't see reels the user only *watched* — the user must share them.

---

## 4. Android app (Flutter)

### 4.1 Modes
| Mode | Who pays for AI | Fetch | Cost to user |
|---|---|---|---|
| **BYOK** (own Gemini/Groq key) | User | On phone (WebView, logged out) | **Free** |
| **BYOK + phone fetch fails** | User | Server (Apify) | Credits (fetch only) |
| **Subscription** | Us (AI gateway) | Server | Credits |

### 4.2 Local pipeline (BYOK)
```
Share sheet / paste link
  → WebView loads public reel page logged-out → extract video URL → download to cache
  → Gemini (video, same prompt + JSON schema as server: spec/analysis.json)
     fallback: Groq Whisper (audio) + ML Kit OCR (frames) → Groq LLM
  → save locally (SQLite + FTS5) → upload result to server (shared globally)
  → delete video
```
- Keys stored in Android Keystore (`flutter_secure_storage`), **never sent to our server**.
- Same prompt/schema file is bundled in the app and used by the server, so results look identical.

### 4.3 Sync & library
- Library lives on the server (account-wide), cached locally for offline search.
- Linked Instagram identity → reels shared to the bot show up in the app automatically.

### 4.4 Protecting the global DB from BYOK uploads (decision #12)
Results from phones are shared globally, so:
- Server validates JSON schema, field lengths, URL schemes; rejects anything else.
- Stored with `source = 'client'`, uploader id, model name.
- A server-made analysis (`source = 'server'`) always wins over a client one.
- Rate limit uploads per account (e.g. 100/day) and a *Report* button; 3 reports → hidden + re-analyzed by the server.

---

## 5. Accounts, credits, payments

### 5.1 Identity
- One **account** can have many **identities**: `google:<sub>`, `instagram:<IGSID>`.
- **Linking:** app shows a 6-digit code (valid 10 min) → user DMs `link 123456` to the bot → identities merge. This proves ownership of both sides.
- Free grant is per *person*: merging two accounts never gives two free grants.

### 5.2 Units
Credits are stored as integer **units** so fractions work:
| Action | Units (proposal) |
|---|---|
| Reel analysis (new or cached) | 10 |
| Server fetch only (BYOK fallback) | 3 |
| One Q&A message | 1 |
| Free grant (once) | 20 (= 2 reels) |

### 5.3 Plans (monthly, INR — prices TBD)
| Plan | Units / month | Notes |
|---|---|---|
| Free | 20 once | |
| Basic | e.g. 300 (≈ 30 reels) | |
| Pro | e.g. 1500 (≈ 150 reels) | higher queue priority |

### 5.4 Ledger
Every balance change is a row: `grant | reserve | charge | refund | expire | purchase`, with an idempotency key. Balance = cached column, checked against the ledger by a nightly job.

### 5.5 Payment rails
- **Instagram users:** Razorpay Subscriptions; bot DMs a payment link; Razorpay webhook → grant units.
- **App users:** Google Play Billing (required for in-app digital subscriptions); Play RTDN (Pub/Sub push) → grant units; purchase token verified server-side.
- A linked person can pay on either side; units go to the one account.

---

## 6. Data model (Postgres)

```
accounts(id, plan, priority, created_at, deleted_at)
identities(id, account_id, kind ig|google, external_id UNIQUE(kind, external_id), display_name)
link_codes(code, account_id, expires_at)
wallets(account_id PK, units >= 0, free_granted bool)
ledger(id, account_id, delta, kind, job_id, idem_key UNIQUE, created_at)
subscriptions(id, account_id, provider razorpay|play, external_id, plan, status, period_end)
payment_events(provider, event_id, PRIMARY KEY(provider, event_id), payload)  -- webhook dedupe
reels(shortcode PK, analysis jsonb, search tsvector, source server|client, model, uploader_id, reports, created_at)
library(account_id, shortcode, via ig|app, added_at, PK(account_id, shortcode))
jobs(id, stage fetch|analyze|deliver|chat, account_id, shortcode, payload jsonb, status, priority,
     attempts, locked_at, error, reserve_ledger_id, created_at)
conversations(account_id, channel ig|app, active_shortcode, history jsonb, last_inbound_at)
inbound_events(platform, message_id, PRIMARY KEY(platform, message_id), received_at)
api_usage(day, provider, key_id, model, requests, errors, tokens_in, tokens_out)   -- existing
```
Reused from the current code: job queue (SKIP LOCKED, heartbeat, NOTIFY), key pools, Gemini/Groq analysis, FTS search, usage tracking.

---

## 7. Backend API (for the app)

```
POST /v1/auth/google              → JWT
GET  /v1/me                       → account, units, plan, linked identities
POST /v1/link/code                → 6-digit code for the bot
GET  /v1/library?q=&cursor=       → reels (FTS search)
GET  /v1/reels/{shortcode}
POST /v1/reels/{shortcode}/result → BYOK upload (validated)
POST /v1/reels/{shortcode}/report
POST /v1/jobs {url, mode}         → server fetch/analyze (reserves units, 402 if none)
GET  /v1/jobs/{id}
POST /v1/chat {shortcode?, question}
POST /v1/billing/play/verify
POST /v1/webhooks/meta | /razorpay | /play
DELETE /v1/me                     → account + data deletion (also Meta's deletion callback)
```

---

## 8. Reliability & scale
- Web returns 200 to Meta in < 1 s; all work happens in workers.
- Workers scale horizontally; per-provider concurrency caps + key rotation (existing).
- Apify calls are **batched** (many reel URLs per actor run) to cut cost.
- Back-pressure: if queue > limit, free users get "busy, try in a few minutes"; paid users still queue.
- Observability: job durations per stage, failure reasons, cost per reel (from `api_usage`), daily summary to you.

Rough capacity target for v1: 1,000 users, ~5,000 reels/day, p50 < 60 s end to end.

---

## 9. Migration from today's repo
- **Remove:** `app/bot.py`, Telegram config and webhook, Notion leftovers, `cobalt`/`ytdlp` fetchers (they hit Instagram from our server).
- **Keep & adapt:** `services/db.py` queue, `services/keys.py`, `services/analyze.py`, `services/chat.py`, `fetchers/apify.py`, `app/api.py` (→ `/v1`).
- **New:** `channels/instagram.py` (webhook + send API), `services/wallet.py`, `services/identity.py`, `payments/razorpay.py`, `payments/play.py`, OCR fallback, `mobile/` app.
- Bring back from stash: `spec/analysis.json`, `services/spec.py`, Flutter pipeline files.

---

## 10. Build plan
| Phase | What | Exit check |
|---|---|---|
| **P0 – Spikes (≈1 wk)** | Meta app + test pro account; confirm `ig_reel` webhook gives a downloadable media URL; Apify batch cost per reel; WebView logged-out fetch success rate on 50 reels | Numbers written down; plan adjusted |
| **P1 – Server core** | Accounts/identities, wallet + ledger, stage queue, IG webhook + send, Q&A in DM, free grant | Tester accounts use the bot end-to-end |
| **P2 – Payments (IG)** | Razorpay subscriptions + webhook | Real ₹ payment grants units |
| **P3 – Android app** | Google sign-in, library, BYOK local pipeline, server mode, linking, Play Billing | Internal testing track |
| **P4 – Launch** | App Review, privacy policy, data deletion, monitoring | Public bot + Play Store listing |

---

### 10.1 Phase 0 results

| Spike | Result | Status |
|---|---|---|
| Apify batch cost | 13/13 reels in **one 13 s run**, **$0.0023 per reel** (≈ ₹0.19); video URLs download from a server (HTTP 206, video/mp4) | ✅ done |
| End-to-end on live reels | 5/5 analyzed; avg **2.3k input / 0.3k output tokens** per reel; 6–20 s normally, up to ~60 s when Gemini returns 503 and we fall back across models | ✅ done |
| Meta `ig_reel` webhook shape | Meta's docs list `title`, `url` and a video id but don't pin the key names. The code accepts `reel_video_id`/`video_id`/`id` and logs the keys of every attachment | ⏳ needs Meta setup (SETUP_META.md step 5) |
| WebView logged-out fetch rate | Belongs with the app (P3); measured on a real phone, since datacenter IPs aren't representative | ⏳ P3 |
| Standard vs Advanced Access | Docs say Standard works for accounts we own or added as testers; the public needs Advanced Access (App Review) | ⏳ confirm during setup |

### 10.2 What P1 built (server core)
- Schema v2 (§6), minus `subscriptions` / `payment_events`, which come with payments (P2).
- Identities and linking via a 6-digit code; wallet with reserve/refund and an idempotent ledger.
- Staged queue: fetch (batched Apify) → analyze → deliver, and chat.
- Instagram webhook (signature, dedupe, handshake), Send API with retries and the 24-hour window error, and weekly token refresh.
- DM commands, Q&A, and the app API under `/v1`.
- Removed: the Telegram bot, yt-dlp, Cobalt and instaloader (they all fetched from Instagram using our servers).

### 10.3 What P2–P4 built
- **P2 payments:** Razorpay subscriptions. The DM `plans` command shows a payment link; a signed webhook applies each paid cycle exactly once. Unused units expire at renewal (A8), `cancel` stops renewal at the end of the cycle, and ended plans fall back to free daily.
- **P3 server:**
  - Own-key result upload: validated, rate-limited, and a server analysis always wins.
  - "Wrong summary" reports: 3 reports hide the reel and queue a server re-analysis.
  - Fetch-only jobs for own-key users (3 units; free if the reel is already analyzed).
  - Plans list, Google Play purchase verification with acknowledgement, and Play renewal notifications.
- **P3 app:** see mobile/README.md.
- **P4:**
  - `/privacy`, `/terms` and `/data-deletion` pages.
  - Meta's signed data-deletion callback, the DM `delete` command and app `DELETE /v1/me`, all of which also cancel a renewing plan.
  - `/admin/stats`.

## 11. Open questions (round 5–6, answer when ready)
1. Hosting budget per month (₹/$)? Render vs Google Cloud Run?
2. Do you have / can you get a PAN-linked business identity for Meta Business Verification and Razorpay?
3. Target launch date — is this for a hackathon/demo or a real public launch?
4. Anyone else on the team (Flutter dev, designer)?
5. Unit prices and plan prices (needs P0 cost numbers first).
6. Should free users get Q&A at all, or Q&A only on paid plans?
7. iOS ever? (affects whether we lean on Android-only APIs)
8. Languages: English only, or also Malayalam/Hindi reels and replies?
