# Setting up the Instagram bot (one-time, ~45 min)

These steps need **you** (they involve your logins). The code is ready; it waits for four values in Render → Environment.
Meta renames dashboard menus often — if a label differs, look for the closest match or check
[Meta's Instagram Platform docs](https://developers.facebook.com/docs/instagram-platform).

## 0. Database (Neon) — needed before anything deploys
1. [neon.tech](https://neon.tech) → sign up → **Create project** (region: Singapore / AWS ap-southeast-1, closest to India).
2. Dashboard → **Connect** → copy the connection string (`postgresql://…?sslmode=require`).
3. Render → reel-brain → **Environment** → add `DATABASE_URL` = that string.

## 1. The bot's Instagram account
1. Create a new Instagram account for the bot (e.g. `@reelbrain.ai`). Don't use your personal one.
2. Instagram → Settings → **Account type and tools → Switch to professional account** → **Creator** (or Business).
3. Instagram → Settings → **Messages and story replies → Message controls → Connected tools →
   Allow access to messages: ON**. Without this, the API can't read or send DMs.

## 2. The Meta app
1. [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create app**.
2. Use case: **Manage messaging & content on Instagram**. App type: Business. Name: Reel Brain.
3. In the app → **Instagram → API setup with Instagram login**:
   - **Generate access tokens → Add account** → log in as the bot account → copy the token.
     → Render: `INSTAGRAM_ACCESS_TOKEN` = token. (Lasts 60 days; the server refreshes it weekly by itself.)
   - Copy **Instagram app secret** (on the same page — *not* the Facebook app secret under App settings).
     → Render: `INSTAGRAM_APP_SECRET` = secret.
4. Make up a random string (e.g. from a password generator) → Render: `INSTAGRAM_VERIFY_TOKEN` = that string.
5. Click **Save, rebuild and deploy** in Render and wait until the deploy is live.

## 3. Webhook
1. Same page → **Configure webhooks**:
   - Callback URL: `https://<your-render-url>/webhooks/instagram`
   - Verify token: the `INSTAGRAM_VERIFY_TOKEN` string → **Verify and save**.
2. Subscribe the bot account to the fields **messages** and **messaging_postbacks**.

## 4. Testers (until App Review)
Until Meta approves the app (Phase 4), only people with a role on the app can use the bot.
1. App → **App roles → Roles → Add people → Instagram tester** → enter the tester's Instagram username.
2. The tester accepts in Instagram → Settings → **Website permissions / Apps and websites → Tester invites**.
3. Add yourself first.

## 5. Try it, and send me two log lines (this finishes Phase 0)
1. From your tester account, DM the bot "hi" → it should welcome you with 2 free reels.
2. Share any reel to the bot (➤ Share → the bot).
3. Render → **Logs**, find and send me:
   - `new Instagram user <ID> → account …` — that ID is your IGSID. Put it in Render as `ADMIN_IG_IDS` to get the unlimited plan.
   - `ig attachments from …: [('ig_reel', [...keys...])]` — this confirms what Meta sends for a shared reel
     (the field names Meta's docs don't pin down). If it's different, I adjust `app/inbound.py`.

## Later (Phase 4, before public launch)
- **Business Verification** (Meta Business Settings → Security Centre) — needs a document in your name or the business's.
- **App Review → Advanced Access** for `instagram_business_manage_messages` and `instagram_business_basic`
  (screencast of the bot working, privacy policy URL, data-deletion URL — I'll build both pages).
