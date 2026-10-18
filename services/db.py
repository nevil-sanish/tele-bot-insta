"""Postgres (Neon) access: users, the global reel cache, libraries, the job
queue and credits. Plain SQL over asyncpg; no ORM."""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import asyncpg

from app import config

from .models import Reel

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).with_name("schema.sql")
UNLIMITED = "unlimited"

_pool: asyncpg.Pool | None = None


class DBError(Exception):
    pass


async def _init_connection(con: asyncpg.Connection) -> None:
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect(dsn: str | None = None) -> None:
    global _pool
    dsn = dsn or config.DATABASE_URL
    if not dsn:
        raise DBError("DATABASE_URL is not set")
    # statement_cache_size=0 keeps us compatible with Neon's pooled (pgbouncer) endpoint.
    _pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, statement_cache_size=0, init=_init_connection, command_timeout=30
    )
    async with _pool.acquire() as con:
        await con.execute(SCHEMA.read_text())


async def close() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise DBError("database not connected")
    return _pool


# ---------------------------------------------------------------- users


@dataclass
class User:
    id: int
    plan: str
    credits: int
    telegram_id: int | None = None
    google_sub: str | None = None
    email: str | None = None
    name: str | None = None

    @property
    def unlimited(self) -> bool:
        return self.plan == UNLIMITED

    def to_dict(self) -> dict:
        return {"id": self.id, "plan": self.plan, "credits": self.credits, "email": self.email, "name": self.name}


def _user(row) -> User | None:
    if not row:
        return None
    return User(
        id=row["id"], plan=row["plan"], credits=row["credits"], telegram_id=row["telegram_id"],
        google_sub=row["google_sub"], email=row["email"], name=row["name"],
    )


async def telegram_user(telegram_id: int, name: str | None = None) -> User:
    """Whitelisted Telegram users get the unlimited plan (no credits)."""
    row = await pool().fetchrow(
        """INSERT INTO users (telegram_id, name, plan) VALUES ($1, $2, $3)
           ON CONFLICT (telegram_id) DO UPDATE SET plan = $3, name = COALESCE($2, users.name)
           RETURNING *""",
        telegram_id, name, UNLIMITED,
    )
    return _user(row)


async def google_user(sub: str, email: str | None, name: str | None) -> tuple[User, bool]:
    """Find or create an app user from a verified Google login. Returns (user, created)."""
    async with pool().acquire() as con, con.transaction():
        row = await con.fetchrow("SELECT * FROM users WHERE google_sub = $1", sub)
        if row:
            row = await con.fetchrow(
                "UPDATE users SET email = $2, name = $3 WHERE id = $1 RETURNING *", row["id"], email, name
            )
            return _user(row), False
        row = await con.fetchrow(
            "INSERT INTO users (google_sub, email, name, credits) VALUES ($1, $2, $3, $4) RETURNING *",
            sub, email, name, config.FREE_CREDITS,
        )
        if config.FREE_CREDITS:
            await con.execute(
                "INSERT INTO credit_ledger (user_id, delta, reason) VALUES ($1, $2, 'signup bonus')",
                row["id"], config.FREE_CREDITS,
            )
        return _user(row), True


async def get_user(user_id: int) -> User | None:
    return _user(await pool().fetchrow("SELECT * FROM users WHERE id = $1", user_id))


async def add_credits(user_id: int, delta: int, reason: str) -> int:
    async with pool().acquire() as con, con.transaction():
        credits = await con.fetchval(
            "UPDATE users SET credits = GREATEST(credits + $2, 0) WHERE id = $1 RETURNING credits", user_id, delta
        )
        await con.execute(
            "INSERT INTO credit_ledger (user_id, delta, reason) VALUES ($1, $2, $3)", user_id, delta, reason
        )
        return credits


# ---------------------------------------------------------------- reels + libraries

_REEL_COLS = [
    "shortcode", "link", "title", "summary", "key_ideas", "on_screen_text", "transcript", "tools",
    "tags", "category", "caption", "hashtags", "urls", "author", "posted_at", "analysis", "source", "model",
]


def _reel(row) -> Reel | None:
    if not row:
        return None
    data = {c: row[c] for c in _REEL_COLS}
    data["date"] = data.pop("posted_at")
    return Reel(**data)


def _reel_values(r: Reel) -> list:
    return [
        r.shortcode, r.link, r.title, r.summary, r.key_ideas, r.on_screen_text, r.transcript, r.tools,
        r.tags, r.category, r.caption, r.hashtags, r.urls, r.author, r.date, r.analysis, r.source, r.model,
    ]


async def get_reel(shortcode: str) -> Reel | None:
    return _reel(await pool().fetchrow("SELECT * FROM reels WHERE shortcode = $1", shortcode))


async def save_reel(con, reel: Reel) -> None:
    cols = ", ".join(_REEL_COLS)
    params = ", ".join(f"${i}" for i in range(1, len(_REEL_COLS) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _REEL_COLS[1:])
    await con.execute(
        f"INSERT INTO reels ({cols}) VALUES ({params}) ON CONFLICT (shortcode) DO UPDATE SET {updates}",
        *_reel_values(reel),
    )


async def add_to_library(user_id: int, shortcode: str, con=None) -> None:
    await (con or pool()).execute(
        """INSERT INTO user_reels (user_id, shortcode) VALUES ($1, $2)
           ON CONFLICT (user_id, shortcode) DO UPDATE SET saved_at = now()""",
        user_id, shortcode,
    )


async def library(user_id: int, limit: int = 20, offset: int = 0) -> list[Reel]:
    rows = await pool().fetch(
        """SELECT r.* FROM user_reels u JOIN reels r USING (shortcode)
           WHERE u.user_id = $1 ORDER BY u.saved_at DESC LIMIT $2 OFFSET $3""",
        user_id, limit, offset,
    )
    return [_reel(r) for r in rows]


async def library_shortcodes(user_id: int) -> set[str]:
    rows = await pool().fetch("SELECT shortcode FROM user_reels WHERE user_id = $1", user_id)
    return {r["shortcode"] for r in rows}


async def in_library(user_id: int, shortcode: str) -> bool:
    return bool(await pool().fetchval(
        "SELECT 1 FROM user_reels WHERE user_id = $1 AND shortcode = $2", user_id, shortcode
    ))


async def all_reels() -> list[Reel]:
    return [_reel(r) for r in await pool().fetch("SELECT * FROM reels")]


# ---------------------------------------------------------------- job queue


@dataclass
class Job:
    id: int
    user_id: int
    shortcode: str
    url: str | None = None
    file_id: str | None = None
    mime_type: str = "video/mp4"
    chat_id: int | None = None
    status_message_id: int | None = None
    status: str = "queued"
    attempts: int = 0
    error: str | None = None

    @property
    def link(self) -> str:
        return f"https://www.instagram.com/reel/{self.shortcode}/" if self.url else ""


def _job(row) -> Job | None:
    if not row:
        return None
    return Job(**{k: row[k] for k in Job.__dataclass_fields__})


async def enqueue(
    user_id: int, shortcode: str, url: str | None = None, file_id: str | None = None,
    mime_type: str = "video/mp4", chat_id: int | None = None, status_message_id: int | None = None,
) -> Job:
    row = await pool().fetchrow(
        """INSERT INTO jobs (user_id, shortcode, url, file_id, mime_type, chat_id, status_message_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        user_id, shortcode, url, file_id, mime_type, chat_id, status_message_id,
    )
    return _job(row)


async def claim_job() -> Job | None:
    """Take the oldest runnable job. A running job whose heartbeat stopped (crash,
    redeploy) is taken over. Jobs for a reel that is already being analyzed wait,
    so the same reel is never analyzed twice at once."""
    stale = timedelta(seconds=config.JOB_LOCK_TIMEOUT)
    async with pool().acquire() as con, con.transaction():
        await con.execute(
            """UPDATE jobs SET status = 'failed', error = COALESCE(error, 'gave up after repeated crashes'),
                   updated_at = now()
               WHERE status = 'running' AND locked_at < now() - $1::interval AND attempts >= $2""",
            stale, config.JOB_MAX_ATTEMPTS,
        )
        row = await con.fetchrow(
            """UPDATE jobs SET status = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
               WHERE id = (
                   SELECT j.id FROM jobs j
                   WHERE (j.status = 'queued' OR (j.status = 'running' AND j.locked_at < now() - $1::interval))
                     AND NOT EXISTS (
                         SELECT 1 FROM jobs r
                         WHERE r.shortcode = j.shortcode AND r.id <> j.id AND r.status = 'running'
                           AND r.locked_at >= now() - $1::interval)
                   ORDER BY j.id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1)
               RETURNING *""",
            stale,
        )
        return _job(row)


async def heartbeat(job_id: int) -> None:
    await pool().execute("UPDATE jobs SET locked_at = now() WHERE id = $1 AND status = 'running'", job_id)


async def requeue(job_id: int) -> None:
    """Put a job back (e.g. on shutdown) without counting the interrupted attempt."""
    await pool().execute(
        """UPDATE jobs SET status = 'queued', locked_at = NULL, attempts = GREATEST(attempts - 1, 0),
               updated_at = now() WHERE id = $1 AND status = 'running'""",
        job_id,
    )


async def finish_job(job: Job, reel: Reel, analyzed: bool) -> bool:
    """Save the reel (if newly analyzed), add it to the user's library and charge
    one credit for a fresh analysis on a credit plan. Returns True if charged."""
    async with pool().acquire() as con, con.transaction():
        if analyzed:
            await save_reel(con, reel)
        await add_to_library(job.user_id, reel.shortcode, con)
        charged = False
        if analyzed:
            charged = bool(await con.fetchval(
                """UPDATE users SET credits = credits - 1
                   WHERE id = $1 AND plan <> $2 AND credits > 0 RETURNING 1""",
                job.user_id, UNLIMITED,
            ))
            if charged:
                await con.execute(
                    "INSERT INTO credit_ledger (user_id, delta, reason, job_id) VALUES ($1, -1, 'analysis', $2)",
                    job.user_id, job.id,
                )
        await con.execute(
            """UPDATE jobs SET status = 'done', error = NULL, charged = $2, locked_at = NULL, updated_at = now()
               WHERE id = $1""",
            job.id, charged,
        )
        return charged


async def fail_job(job_id: int, error: str) -> None:
    await pool().execute(
        "UPDATE jobs SET status = 'failed', error = $2, locked_at = NULL, updated_at = now() WHERE id = $1",
        job_id, error[:1000],
    )


async def get_job(job_id: int) -> Job | None:
    return _job(await pool().fetchrow("SELECT * FROM jobs WHERE id = $1", job_id))


async def pending_jobs(user_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*) FROM jobs WHERE user_id = $1 AND status IN ('queued', 'running')", user_id
    )


async def failed_jobs(user_id: int, limit: int = 5) -> list[Job]:
    """Latest failed job per reel that hasn't since succeeded."""
    rows = await pool().fetch(
        """SELECT DISTINCT ON (j.shortcode) j.* FROM jobs j
           WHERE j.user_id = $1 AND j.status = 'failed'
             AND NOT EXISTS (SELECT 1 FROM user_reels u WHERE u.user_id = j.user_id AND u.shortcode = j.shortcode)
             AND NOT EXISTS (SELECT 1 FROM jobs q WHERE q.shortcode = j.shortcode AND q.user_id = j.user_id
                             AND q.status IN ('queued', 'running'))
           ORDER BY j.shortcode, j.id DESC LIMIT $2""",
        user_id, limit,
    )
    return [_job(r) for r in rows]


async def retry_job(job_id: int, chat_id: int | None = None, status_message_id: int | None = None) -> None:
    await pool().execute(
        """UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, locked_at = NULL, updated_at = now(),
               chat_id = COALESCE($2, chat_id), status_message_id = COALESCE($3, status_message_id)
           WHERE id = $1""",
        job_id, chat_id, status_message_id,
    )


async def queue_size() -> int:
    return await pool().fetchval("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")
