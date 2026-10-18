-- Applied at every startup; every statement is idempotent.

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE,
    google_sub  TEXT UNIQUE,
    email       TEXT,
    name        TEXT,
    plan        TEXT NOT NULL DEFAULT 'free',      -- free | premium | unlimited
    credits     INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Global cache: each reel is analyzed once and shared by everyone who sends it.
CREATE TABLE IF NOT EXISTS reels (
    shortcode      TEXT PRIMARY KEY,
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
    analysis       TEXT NOT NULL DEFAULT 'full',
    source         TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Each user's library: which reels they saved.
CREATE TABLE IF NOT EXISTS user_reels (
    user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shortcode TEXT NOT NULL REFERENCES reels(shortcode) ON DELETE CASCADE,
    saved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, shortcode)
);
CREATE INDEX IF NOT EXISTS user_reels_recent ON user_reels (user_id, saved_at DESC);

-- Persistent job queue: survives restarts and redeploys.
CREATE TABLE IF NOT EXISTS jobs (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shortcode         TEXT NOT NULL,
    url               TEXT,
    file_id           TEXT,                       -- Telegram file (forwarded video)
    mime_type         TEXT NOT NULL DEFAULT 'video/mp4',
    chat_id           BIGINT,                     -- Telegram chat to reply in (NULL for app jobs)
    status_message_id BIGINT,
    status            TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts          INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    charged           BOOLEAN NOT NULL DEFAULT false,
    locked_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs (status, id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_user ON jobs (user_id, id DESC);

-- Every credit change, for support and auditing.
CREATE TABLE IF NOT EXISTS credit_ledger (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    job_id     BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
