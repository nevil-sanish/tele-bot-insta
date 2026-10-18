-- Applied at every startup; every statement is idempotent.
-- Layout: docs/ARCHITECTURE.md §6.

-- One person. Instagram and Google logins are identities that point here.
CREATE TABLE IF NOT EXISTS accounts (
    id         BIGSERIAL PRIMARY KEY,
    plan       TEXT NOT NULL DEFAULT 'free',          -- free | basic | pro | unlimited
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identities (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('ig', 'google')),
    external_id TEXT NOT NULL,                        -- IGSID or Google `sub`
    email       TEXT,
    name        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, external_id)
);
CREATE INDEX IF NOT EXISTS identities_account ON identities (account_id);

-- Short-lived codes the app shows; DMing "link <code>" to the bot joins the two identities.
CREATE TABLE IF NOT EXISTS link_codes (
    code       TEXT PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

-- Balance in units (1 reel = UNITS_PER_REEL). Cached here, every change is in `ledger`.
CREATE TABLE IF NOT EXISTS wallets (
    account_id   BIGINT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    units        INTEGER NOT NULL DEFAULT 0 CHECK (units >= 0),
    free_granted BOOLEAN NOT NULL DEFAULT false
);

-- Global cache: each reel is analyzed once and shared by everyone who sends it.
CREATE TABLE IF NOT EXISTS reels (
    shortcode      TEXT PRIMARY KEY,                  -- Instagram shortcode, or a stand-in (see db.reel_key)
    link           TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    key_ideas      JSONB NOT NULL DEFAULT '[]',
    on_screen_text TEXT NOT NULL DEFAULT '',
    transcript     TEXT NOT NULL DEFAULT '',
    tools          JSONB NOT NULL DEFAULT '[]',
    tags           JSONB NOT NULL DEFAULT '[]',
    category       TEXT NOT NULL DEFAULT '',
    caption        TEXT NOT NULL DEFAULT '',
    hashtags       JSONB NOT NULL DEFAULT '[]',
    urls           JSONB NOT NULL DEFAULT '[]',
    author         TEXT NOT NULL DEFAULT '',
    posted_at      TEXT,
    analysis       TEXT NOT NULL DEFAULT 'full',      -- full | audio-only | caption-only
    source         TEXT NOT NULL DEFAULT '',          -- where the media came from: meta | apify | upload | phone
    model          TEXT NOT NULL DEFAULT '',
    origin         TEXT NOT NULL DEFAULT 'server',    -- server | client (analyzed on a phone with the user's key)
    uploader_id    BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    reports        INTEGER NOT NULL DEFAULT 0,
    hidden         BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE reels ADD COLUMN IF NOT EXISTS search tsvector GENERATED ALWAYS AS (
    to_tsvector('english',
        title || ' ' || summary || ' ' || category || ' ' || author || ' ' ||
        key_ideas::text || ' ' || tools::text || ' ' || tags::text || ' ' || hashtags::text)
) STORED;
CREATE INDEX IF NOT EXISTS reels_search ON reels USING GIN (search);

-- Each person's library: pointers into `reels`.
CREATE TABLE IF NOT EXISTS library (
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    shortcode  TEXT NOT NULL REFERENCES reels(shortcode) ON DELETE CASCADE,
    via        TEXT NOT NULL DEFAULT 'ig',            -- ig | app
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, shortcode)
);
CREATE INDEX IF NOT EXISTS library_recent ON library (account_id, added_at DESC);

-- Persistent staged queue. A reel job moves fetch → analyze → deliver; a question is one `chat` stage.
CREATE TABLE IF NOT EXISTS jobs (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                      -- reel | chat
    stage         TEXT NOT NULL,                      -- fetch | analyze | deliver | chat
    status        TEXT NOT NULL DEFAULT 'queued',     -- queued | running | done | failed
    channel       TEXT NOT NULL,                      -- ig | app
    recipient     TEXT,                               -- IGSID to reply to (ig jobs)
    shortcode     TEXT,
    payload       JSONB NOT NULL DEFAULT '{}',        -- url, media_url, caption, question, …
    priority      INTEGER NOT NULL DEFAULT 0,         -- higher runs first
    reserved      INTEGER NOT NULL DEFAULT 0,         -- units held for this job, refunded on failure
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    locked_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs (priority DESC, id) WHERE status IN ('queued', 'running');
-- At most one analysis per reel at a time, even with many worker machines.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_analysis_per_reel ON jobs (shortcode)
    WHERE status = 'running' AND stage = 'analyze';
CREATE INDEX IF NOT EXISTS jobs_account ON jobs (account_id, id DESC);

-- Every balance change: grant | reserve | refund | purchase | expire | merge | adjust.
CREATE TABLE IF NOT EXISTS ledger (
    id         BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    delta      INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    job_id     BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    note       TEXT,
    idem_key   TEXT UNIQUE,                           -- makes payment webhooks safe to replay
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_account ON ledger (account_id, id DESC);

-- Per-person, per-channel conversation: the reel questions go to, recent Q&A, a reel waiting for its video.
CREATE TABLE IF NOT EXISTS conversations (
    account_id        BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel           TEXT NOT NULL,
    active_shortcode  TEXT REFERENCES reels(shortcode) ON DELETE SET NULL,
    active_at         TIMESTAMPTZ,
    history           JSONB NOT NULL DEFAULT '[]',
    pending_shortcode TEXT,                           -- reel we asked the user to send as a video
    last_inbound_at   TIMESTAMPTZ,                    -- Meta allows replies for 24 h after this
    PRIMARY KEY (account_id, channel)
);

-- Webhook deliveries already handled (Meta retries). The raw payload is kept a few days for debugging.
CREATE TABLE IF NOT EXISTS inbound_events (
    platform    TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    payload     JSONB,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, message_id)
);

-- Small server-side settings, e.g. the refreshed Instagram token.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Calls per API key per day (key_id is a short hash, never the key).
CREATE TABLE IF NOT EXISTS api_usage (
    day           DATE NOT NULL DEFAULT CURRENT_DATE,
    provider      TEXT NOT NULL,
    key_id        TEXT NOT NULL,
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    input_tokens  BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, key_id, model)
);
