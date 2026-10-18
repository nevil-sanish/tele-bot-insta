# Payments, app login and launch setup (needs you; do these whenever you're ready)

Everything here is built and tested in code; each step only gives the server a key.
Put each value in Render → reel-brain → Environment, then redeploy.

## 1. Razorpay (Instagram users pay through a link in the DM)
1. [dashboard.razorpay.com](https://dashboard.razorpay.com) → sign up (KYC needs PAN + bank account; test mode works before KYC).
2. **Account & Settings → API keys → Generate** (Test mode first) → `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
3. **Subscriptions** must be enabled on the account (Dashboard → Subscriptions; ask Razorpay support if it's not listed).
4. Create the two plans — from your laptop with the keys set:
   `python -m scripts.create_razorpay_plans` → copy the printed `RAZORPAY_PLAN_BASIC=…` and `RAZORPAY_PLAN_PRO=…`.
   (Prices: `PLAN_BASIC_PRICE_INR` / `PLAN_PRO_PRICE_INR`, default ₹99 / ₹299; credits: `PLAN_BASIC_UNITS` / `PLAN_PRO_UNITS`.)
5. **Webhooks → Add new webhook**: URL `https://<your-render-url>/webhooks/razorpay`, a secret you make up
   (→ `RAZORPAY_WEBHOOK_SECRET`), events: all `subscription.*`.
6. Test: DM the bot `plans` → tap a plan → pay with a [test card/UPI](https://razorpay.com/docs/payments/payments/test-card-upi-details/)
   → the bot confirms and `credits` shows the plan.
7. Going live: repeat steps 2, 4, 5 with Live-mode keys.

## 2. Google sign-in for the app
1. [console.cloud.google.com](https://console.cloud.google.com) → new project "Reel Brain" → **APIs & Services → OAuth consent screen** (External, app name, your email).
2. **Credentials → Create credentials → OAuth client ID**:
   - **Web application** (name "server") → copy its client ID. The app uses this as `serverClientId`.
   - **Android**: package `app.reelbrain`, SHA-1 of your signing key (`cd mobile/android && ./gradlew signingReport`).
3. Render: `GOOGLE_CLIENT_IDS` = the web client ID (add the Android one too, comma-separated), and
   `APP_SECRET` = any long random string.
4. Build the app with `--dart-define=GOOGLE_SERVER_CLIENT_ID=<web client ID> --dart-define=API_URL=https://<your-render-url>`.

## 3. Google Play (publishing + in-app subscriptions)
1. [play.google.com/console](https://play.google.com/console) → developer account ($25 once) → create app "Reel Brain".
2. Upload the release bundle (`flutter build appbundle`, see mobile/README.md) to **Internal testing** first.
3. **Monetize → Subscriptions**: create `reelbrain_basic` and `reelbrain_pro`, each with a monthly base plan (₹99 / ₹299).
4. Server verification: Google Cloud → **IAM → Service accounts → Create** → Keys → JSON. In Play Console
   **Users and permissions → Invite** that service account's email with "View financial data" and "Manage orders".
   Render: `PLAY_SERVICE_ACCOUNT_JSON` = the JSON file contents (one line).
5. Renewal notices: Google Cloud → **Pub/Sub → Topic** `play-rtdn` → grant `google-play-developer-notifications@system.gserviceaccount.com`
   the Publisher role → **Subscription** (Push) to `https://<your-render-url>/v1/webhooks/play?token=<random>`
   (→ `PLAY_RTDN_TOKEN`). Play Console → **Monetization setup → Real-time notifications** → the topic name.

## 4. Launch pages and Meta App Review
- Render: `BOT_USERNAME` = the bot's Instagram username, `CONTACT_EMAIL` = the address shown on the privacy page.
- Public pages are live at `/privacy`, `/terms`, `/data-deletion`.
- In the Meta app: **App settings → Basic** → Privacy Policy URL `https://<your-render-url>/privacy`,
  Terms URL `/terms`, **Data deletion → Data deletion callback URL** `https://<your-render-url>/meta/data-deletion`.
- **App Review**: request Advanced Access for `instagram_business_basic` and `instagram_business_manage_messages`;
  record a screencast of: DM "hi" → share a reel → summary → ask a question → "delete".
- Ops: set `ADMIN_TOKEN`, then `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<your-render-url>/admin/stats`.
