"""Postgres (Neon) access: accounts and identities, wallets, the global reel
cache, libraries, conversations and the staged job queue. Plain SQL over
asyncpg; no ORM. Layout: docs/ARCHITECTURE.md §6."""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg

from app import config

from .models import Reel

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).with_name("schema.sql")
UNLIMITED = "unlimited"
PAID_PLANS = ("basic", "pro")
JOBS_CHANNEL = "reel_jobs"  # NOTIFY channel that wakes workers
PRIORITY = {"pro": 10, "basic": 5, UNLIMITED: 5, "free": 0}  # higher plans are served first
LINK_CODE_MINUTES = 10

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


# ---------------------------------------------------------------- accounts


@dataclass
class Account:
    id: int
    plan: str
    units: int
    free_granted: bool = False
    name: str | None = None
    email: str | None = None
    plan_until: datetime | None = None

    @property
    def unlimited(self) -> bool:
        return self.plan == UNLIMITED

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.plan, 0)

    def reels_left(self) -> int | None:
        return None if self.unlimited else self.units // config.UNITS_PER_REEL

    def to_dict(self) -> dict:
        return {
            "id": self.id, "plan": self.plan, "units": self.units, "reels_left": self.reels_left(),
            "name": self.name, "email": self.email,
            "plan_until": self.plan_until.isoformat() if self.plan_until else None,
        }


_ACCOUNT_SQL = """
    SELECT a.id, a.plan, a.plan_until, w.units, w.free_granted,
           (SELECT name FROM identities i WHERE i.account_id = a.id AND name IS NOT NULL ORDER BY i.id LIMIT 1) AS name,
           (SELECT email FROM identities i WHERE i.account_id = a.id AND email IS NOT NULL ORDER BY i.id LIMIT 1) AS email
    FROM accounts a JOIN wallets w ON w.account_id = a.id
    WHERE a.id = $1"""


async def get_account(account_id: int, con=None) -> Account | None:
    row = await (con or pool()).fetchrow(_ACCOUNT_SQL, account_id)
    return Account(**dict(row)) if row else None


async def _identity_account(
    kind: str, external_id: str, email: str | None = None, name: str | None = None, plan: str = "free"
) -> tuple[Account, bool]:
    """Find or create the account behind an identity. New accounts get FREE_UNITS once."""
    async with pool().acquire() as con, con.transaction():
        account_id = await con.fetchval(
            """UPDATE identities SET email = COALESCE($3, email), name = COALESCE($4, name)
               WHERE kind = $1 AND external_id = $2 RETURNING account_id""",
            kind, external_id, email, name,
        )
        created = account_id is None
        if created:
            account_id = await con.fetchval("INSERT INTO accounts (plan) VALUES ($1) RETURNING id", plan)
            # A racing request for the same identity loses here and retries as "found".
            await con.execute(
                "INSERT INTO identities (account_id, kind, external_id, email, name) VALUES ($1, $2, $3, $4, $5)",
                account_id, kind, external_id, email, name,
            )
            await con.execute(
                "INSERT INTO wallets (account_id, units, free_granted) VALUES ($1, $2, true)",
                account_id, config.FREE_UNITS,
            )
            if config.FREE_UNITS:
                await con.execute(
                    "INSERT INTO ledger (account_id, delta, kind, note) VALUES ($1, $2, 'grant', 'free trial')",
                    account_id, config.FREE_UNITS,
                )
        elif plan == UNLIMITED:
            await con.execute("UPDATE accounts SET plan = $2 WHERE id = $1", account_id, plan)
        return await get_account(account_id, con), created


async def ig_account(igsid: str, name: str | None = None) -> tuple[Account, bool]:
    plan = UNLIMITED if igsid in config.ADMIN_IG_IDS else "free"
    try:
        return await _identity_account("ig", igsid, name=name, plan=plan)
    except asyncpg.UniqueViolationError:
        return await _identity_account("ig", igsid, name=name, plan=plan)


async def google_account(sub: str, email: str | None, name: str | None) -> tuple[Account, bool]:
    try:
        return await _identity_account("google", sub, email=email, name=name)
    except asyncpg.UniqueViolationError:
        return await _identity_account("google", sub, email=email, name=name)


async def identities(account_id: int) -> list[dict]:
    rows = await pool().fetch(
        "SELECT kind, external_id, email, name FROM identities WHERE account_id = $1 ORDER BY id", account_id
    )
    return [dict(r) for r in rows]


async def delete_account(account_id: int) -> None:
    """Erase a person: identities, wallet, ledger, library, jobs, conversations and the raw
    webhook payloads they sent. Reels stay in the global cache (public content), unlinked."""
    async with pool().acquire() as con, con.transaction():
        ig_ids = [r["external_id"] for r in await con.fetch(
            "SELECT external_id FROM identities WHERE account_id = $1 AND kind = 'ig'", account_id
        )]
        for igsid in ig_ids:
            await con.execute(
                """DELETE FROM inbound_events WHERE platform = 'ig'
                   AND payload @> jsonb_build_object('entry', jsonb_build_array(jsonb_build_object(
                       'messaging', jsonb_build_array(jsonb_build_object('sender', jsonb_build_object('id', $1::text))))))""",
                igsid,
            )
        await con.execute("DELETE FROM accounts WHERE id = $1", account_id)


async def account_for_identity(kind: str, external_id: str) -> int | None:
    return await pool().fetchval(
        "SELECT account_id FROM identities WHERE kind = $1 AND external_id = $2", kind, external_id
    )


async def set_plan(account_id: int, plan: str) -> None:
    await pool().execute("UPDATE accounts SET plan = $2 WHERE id = $1", account_id, plan)


# ---------------------------------------------------------------- linking


async def new_link_code(account_id: int) -> str:
    """6-digit code the app shows; the user DMs "link <code>" to the bot."""
    await pool().execute("DELETE FROM link_codes WHERE account_id = $1 OR expires_at < now()", account_id)
    for _ in range(5):
        code = f"{secrets.randbelow(10**6):06d}"
        try:
            await pool().execute(
                "INSERT INTO link_codes (code, account_id, expires_at) VALUES ($1, $2, now() + $3::interval)",
                code, account_id, timedelta(minutes=LINK_CODE_MINUTES),
            )
            return code
        except asyncpg.UniqueViolationError:
            continue
    raise DBError("could not allocate a link code")


async def link_with_code(code: str, account_id: int) -> Account | None:
    """Merge `account_id` (the Instagram side) into the account that made `code`.
    Returns the merged account, or None if the code is wrong or expired."""
    async with pool().acquire() as con, con.transaction():
        target = await con.fetchval(
            "DELETE FROM link_codes WHERE code = $1 AND expires_at > now() RETURNING account_id", code.strip()
        )
        if target is None:
            return None
        if target != account_id:
            await _merge(con, source=account_id, target=target)
        return await get_account(target, con)


async def _merge(con, source: int, target: int) -> None:
    src = await con.fetchrow(
        """SELECT a.plan, a.plan_until, w.units, w.free_granted FROM accounts a JOIN wallets w ON w.account_id = a.id
           WHERE a.id = $1 FOR UPDATE""",
        source,
    )
    dst = await con.fetchrow(
        """SELECT a.plan, a.plan_until, w.units, w.free_granted FROM accounts a JOIN wallets w ON w.account_id = a.id
           WHERE a.id = $1 FOR UPDATE""",
        target,
    )
    units = src["units"]
    if src["free_granted"] and dst["free_granted"]:
        units = max(units - config.FREE_UNITS, 0)  # one free trial per person, not per login
    keep = max((src, dst), key=lambda r: PRIORITY.get(r["plan"], 0))  # the better plan wins, with its end date
    await con.execute(
        "UPDATE accounts SET plan = $2, plan_until = $3 WHERE id = $1", target, keep["plan"], keep["plan_until"]
    )
    await con.execute("UPDATE wallets SET units = units + $2, free_granted = true WHERE account_id = $1", target, units)
    await con.execute(
        "INSERT INTO ledger (account_id, delta, kind, note) VALUES ($1, $2, 'merge', $3)",
        target, units, f"linked account {source}",
    )
    await con.execute("UPDATE identities SET account_id = $2 WHERE account_id = $1", source, target)
    await con.execute(
        """INSERT INTO library (account_id, shortcode, via, added_at)
           SELECT $2, shortcode, via, added_at FROM library WHERE account_id = $1
           ON CONFLICT (account_id, shortcode) DO NOTHING""",
        source, target,
    )
    await con.execute(
        """INSERT INTO conversations SELECT $2, channel, active_shortcode, active_at, history, pending_shortcode,
               last_inbound_at FROM conversations WHERE account_id = $1
           ON CONFLICT (account_id, channel) DO NOTHING""",
        source, target,
    )
    await con.execute("UPDATE jobs SET account_id = $2 WHERE account_id = $1", source, target)
    await con.execute("UPDATE subscriptions SET account_id = $2 WHERE account_id = $1", source, target)
    await con.execute("UPDATE ledger SET account_id = $2 WHERE account_id = $1", source, target)
    await con.execute("DELETE FROM accounts WHERE id = $1", source)


# ---------------------------------------------------------------- wallet


async def _reserve(con, account_id: int, units: int, note: str, job_id: int | None = None) -> int | None:
    """Hold `units` for a job. Returns units held (0 on unlimited), None if the balance is short."""
    plan = await con.fetchval("SELECT plan FROM accounts WHERE id = $1", account_id)
    if plan == UNLIMITED or units <= 0:
        return 0
    ok = await con.fetchval(
        "UPDATE wallets SET units = units - $2 WHERE account_id = $1 AND units >= $2 RETURNING 1", account_id, units
    )
    if not ok:
        return None
    await con.execute(
        "INSERT INTO ledger (account_id, delta, kind, job_id, note) VALUES ($1, $2, 'reserve', $3, $4)",
        account_id, -units, job_id, note,
    )
    return units


async def _refund(con, job_id: int, note: str = "job failed") -> None:
    row = await con.fetchrow("SELECT account_id, reserved FROM jobs WHERE id = $1 FOR UPDATE", job_id)
    if not row or row["reserved"] <= 0:
        return
    await con.execute("UPDATE jobs SET reserved = 0 WHERE id = $1", job_id)
    await con.execute("UPDATE wallets SET units = units + $2 WHERE account_id = $1", row["account_id"], row["reserved"])
    await con.execute(
        "INSERT INTO ledger (account_id, delta, kind, job_id, note) VALUES ($1, $2, 'refund', $3, $4)",
        row["account_id"], row["reserved"], job_id, note,
    )


async def grant(account_id: int, units: int, kind: str, note: str, idem_key: str | None = None) -> bool:
    """Add units (purchase, admin adjust). Replaying the same idem_key does nothing. Returns True if applied."""
    async with pool().acquire() as con, con.transaction():
        applied = await con.fetchval(
            """INSERT INTO ledger (account_id, delta, kind, note, idem_key) VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (idem_key) DO NOTHING RETURNING 1""",
            account_id, units, kind, note, idem_key,
        )
        if applied:
            await con.execute(
                "UPDATE wallets SET units = GREATEST(units + $2, 0) WHERE account_id = $1", account_id, units
            )
        return bool(applied)


# ---------------------------------------------------------------- subscriptions


async def upsert_subscription(
    account_id: int, provider: str, external_id: str, plan: str, status: str, current_end: datetime | None = None
) -> None:
    await pool().execute(
        """INSERT INTO subscriptions (account_id, provider, external_id, plan, status, current_end)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (provider, external_id) DO UPDATE SET
               plan = $4, status = $5, current_end = COALESCE($6, subscriptions.current_end), updated_at = now()""",
        account_id, provider, external_id, plan, status, current_end,
    )


async def subscription(provider: str, external_id: str) -> dict | None:
    row = await pool().fetchrow(
        "SELECT * FROM subscriptions WHERE provider = $1 AND external_id = $2", provider, external_id
    )
    return dict(row) if row else None


async def live_subscription(account_id: int, provider: str | None = None) -> dict | None:
    """The newest subscription that is paid up or waiting for its first payment."""
    row = await pool().fetchrow(
        """SELECT * FROM subscriptions WHERE account_id = $1 AND ($2::text IS NULL OR provider = $2)
             AND status IN ('created', 'authenticated', 'active', 'pending')
           ORDER BY id DESC LIMIT 1""",
        account_id, provider,
    )
    return dict(row) if row else None


async def apply_renewal(
    account_id: int, plan: str, units: int, period_end: datetime | None, idem_key: str, note: str
) -> bool:
    """A paid billing cycle: switch to `plan` until `period_end` and top up to its units.
    On a renewal the unused units of the previous paid month expire (no rollover); on a
    first purchase leftover free units are kept. Replaying the same idem_key does nothing."""
    async with pool().acquire() as con, con.transaction():
        row = await con.fetchrow(
            """SELECT a.plan, a.plan_until, w.units FROM accounts a JOIN wallets w ON w.account_id = a.id
               WHERE a.id = $1 FOR UPDATE""",
            account_id,
        )
        if row is None:
            return False
        applied = await con.fetchval(
            """INSERT INTO ledger (account_id, delta, kind, note, idem_key) VALUES ($1, $2, 'purchase', $3, $4)
               ON CONFLICT (idem_key) DO NOTHING RETURNING id""",
            account_id, units, note, idem_key,
        )
        if not applied:
            return False
        left = row["units"]
        if row["plan"] in PAID_PLANS and row["plan_until"] is not None and left > 0:
            await con.execute(
                "INSERT INTO ledger (account_id, delta, kind, note) VALUES ($1, $2, 'expire', 'previous month')",
                account_id, -left,
            )
            left = 0
        await con.execute("UPDATE wallets SET units = $2 WHERE account_id = $1", account_id, left + units)
        if row["plan"] != UNLIMITED:
            await con.execute(
                "UPDATE accounts SET plan = $2, plan_until = $3 WHERE id = $1", account_id, plan, period_end
            )
        return True


async def downgrade_expired(grace: timedelta = timedelta(days=1)) -> list[int]:
    """Paid plans whose period ended (cancelled or failed renewal) go back to free; units stay."""
    rows = await pool().fetch(
        """UPDATE accounts SET plan = 'free', plan_until = NULL
           WHERE plan = ANY($1::text[]) AND plan_until < now() - $2::interval RETURNING id""",
        list(PAID_PLANS), grace,
    )
    return [r["id"] for r in rows]


async def first_payment_event(provider: str, event_id: str, payload: dict | None = None) -> bool:
    return bool(await pool().fetchval(
        """INSERT INTO payment_events (provider, event_id, payload) VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING RETURNING 1""",
        provider, event_id, payload,
    ))


async def forget_payment_event(provider: str, event_id: str) -> None:
    await pool().execute("DELETE FROM payment_events WHERE provider = $1 AND event_id = $2", provider, event_id)


async def ig_recipient(account_id: int) -> str | None:
    """The account's Instagram ID if we may message it now (inside Meta's 24-hour window)."""
    return await pool().fetchval(
        """SELECT i.external_id FROM identities i
           JOIN conversations c ON c.account_id = i.account_id AND c.channel = 'ig'
           WHERE i.account_id = $1 AND i.kind = 'ig' AND c.last_inbound_at > now() - interval '23 hours'
           ORDER BY i.id LIMIT 1""",
        account_id,
    )


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


async def get_reel(shortcode: str, con=None) -> Reel | None:
    return _reel(await (con or pool()).fetchrow(
        "SELECT * FROM reels WHERE shortcode = $1 AND NOT hidden", shortcode
    ))


async def save_reel(con, reel: Reel, origin: str = "server", uploader_id: int | None = None) -> None:
    """Insert or replace a reel. A server analysis always replaces a phone one, never the reverse."""
    cols = _REEL_COLS + ["origin", "uploader_id"]
    params = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
    await con.execute(
        f"""INSERT INTO reels ({", ".join(cols)}) VALUES ({params})
            ON CONFLICT (shortcode) DO UPDATE SET {updates}, hidden = false, reports = 0
            WHERE reels.origin = 'client' OR EXCLUDED.origin = 'server'""",
        *_reel_values(reel), origin, uploader_id,
    )


async def client_uploads_today(account_id: int) -> int:
    return await pool().fetchval(
        """SELECT count(*) FROM reels WHERE origin = 'client' AND uploader_id = $1
           AND created_at > now() - interval '24 hours'""",
        account_id,
    )


async def save_client_reel(account_id: int, reel: Reel) -> Reel:
    """A result made on someone's phone with their own key. Shared globally, but a
    server analysis always wins; returns whichever version is stored."""
    async with pool().acquire() as con, con.transaction():
        existing = await get_reel(reel.shortcode, con)
        if existing is None:
            await save_reel(con, reel, origin="client", uploader_id=account_id)
        await add_to_library(account_id, reel.shortcode, "app", con)
        return existing or await get_reel(reel.shortcode, con)


async def report_reel(account_id: int, shortcode: str, reason: str, hide_at: int) -> bool:
    """Record a report (once per person). Returns True when this report hid the reel."""
    async with pool().acquire() as con, con.transaction():
        added = await con.fetchval(
            """INSERT INTO reel_reports (account_id, shortcode, reason) VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING RETURNING 1""",
            account_id, shortcode, reason[:500],
        )
        if not added:
            return False
        hidden = await con.fetchval(
            """UPDATE reels SET reports = reports + 1, hidden = hidden OR reports + 1 >= $2
               WHERE shortcode = $1 RETURNING hidden AND reports = $2""",
            shortcode, hide_at,
        )
        return bool(hidden)


async def add_to_library(account_id: int, shortcode: str, via: str = "ig", con=None) -> None:
    await (con or pool()).execute(
        """INSERT INTO library (account_id, shortcode, via) VALUES ($1, $2, $3)
           ON CONFLICT (account_id, shortcode) DO UPDATE SET added_at = now()""",
        account_id, shortcode, via,
    )


async def library(account_id: int, limit: int = 20, offset: int = 0) -> list[Reel]:
    rows = await pool().fetch(
        """SELECT r.* FROM library l JOIN reels r USING (shortcode)
           WHERE l.account_id = $1 AND NOT r.hidden ORDER BY l.added_at DESC LIMIT $2 OFFSET $3""",
        account_id, limit, offset,
    )
    return [_reel(r) for r in rows]


async def in_library(account_id: int, shortcode: str) -> bool:
    return bool(await pool().fetchval(
        "SELECT 1 FROM library WHERE account_id = $1 AND shortcode = $2", account_id, shortcode
    ))


async def search_library(account_id: int, query: str, limit: int = 3) -> list[Reel]:
    """Full-text search over one person's library, best matches first (any word may match)."""
    terms = re.findall(r"\w+", (query or "").lower())[:12]
    if not terms:
        return []
    rows = await pool().fetch(
        """SELECT r.* FROM library l JOIN reels r USING (shortcode),
                  to_tsquery('english', $2) AS q
           WHERE l.account_id = $1 AND NOT r.hidden AND r.search @@ q
           ORDER BY ts_rank(r.search, q) DESC, l.added_at DESC LIMIT $3""",
        account_id, " | ".join(terms), limit,
    )
    return [_reel(r) for r in rows]


# ---------------------------------------------------------------- conversations


@dataclass
class Conversation:
    active: Reel | None = None
    active_at: datetime | None = None
    history: list[dict] = field(default_factory=list)
    pending_shortcode: str | None = None
    last_inbound_at: datetime | None = None


async def get_conversation(account_id: int, channel: str) -> Conversation:
    row = await pool().fetchrow(
        "SELECT * FROM conversations WHERE account_id = $1 AND channel = $2", account_id, channel
    )
    if not row:
        return Conversation()
    active = await get_reel(row["active_shortcode"]) if row["active_shortcode"] else None
    return Conversation(active, row["active_at"], row["history"], row["pending_shortcode"], row["last_inbound_at"])


async def _upsert_conversation(account_id: int, channel: str, values: dict[str, str], *args) -> None:
    """values: column → SQL expression (args are $3, $4, …)."""
    cols, exprs = ", ".join(values), ", ".join(values.values())
    sets = ", ".join(f"{c} = {e}" for c, e in values.items())
    await pool().execute(
        f"""INSERT INTO conversations (account_id, channel, {cols}) VALUES ($1, $2, {exprs})
            ON CONFLICT (account_id, channel) DO UPDATE SET {sets}""",
        account_id, channel, *args,
    )


async def touch_inbound(account_id: int, channel: str) -> None:
    await _upsert_conversation(account_id, channel, {"last_inbound_at": "now()"})


async def set_active_reel(account_id: int, channel: str, shortcode: str) -> None:
    """Questions now go to this reel; a new reel starts a fresh Q&A history."""
    await _upsert_conversation(
        account_id, channel, {"active_shortcode": "$3", "active_at": "now()", "history": "'[]'::jsonb"}, shortcode
    )


async def save_history(account_id: int, channel: str, history: list[dict]) -> None:
    await _upsert_conversation(account_id, channel, {"history": "$3::jsonb", "active_at": "now()"}, history)


async def set_pending(account_id: int, channel: str, shortcode: str | None) -> None:
    await _upsert_conversation(account_id, channel, {"pending_shortcode": "$3"}, shortcode)


async def pop_pending(account_id: int, channel: str) -> str | None:
    """Take the reel waiting for a video (if any) and clear it, atomically."""
    return await pool().fetchval(
        """WITH old AS (SELECT pending_shortcode FROM conversations
                        WHERE account_id = $1 AND channel = $2 FOR UPDATE)
           UPDATE conversations c SET pending_shortcode = NULL FROM old
           WHERE c.account_id = $1 AND c.channel = $2 AND old.pending_shortcode IS NOT NULL
           RETURNING old.pending_shortcode""",
        account_id, channel,
    )


async def reset_conversation(account_id: int, channel: str) -> None:
    await pool().execute(
        """UPDATE conversations SET active_shortcode = NULL, active_at = NULL, history = '[]', pending_shortcode = NULL
           WHERE account_id = $1 AND channel = $2""",
        account_id, channel,
    )


# ---------------------------------------------------------------- job queue


@dataclass
class Job:
    id: int
    account_id: int
    kind: str
    stage: str
    channel: str
    status: str = "queued"
    recipient: str | None = None
    shortcode: str | None = None
    payload: dict = field(default_factory=dict)
    priority: int = 0
    reserved: int = 0
    attempts: int = 0
    error: str | None = None


def _job(row) -> Job | None:
    if not row:
        return None
    return Job(**{k: row[k] for k in Job.__dataclass_fields__})


class _ShortBalance(Exception):
    pass


async def create_job(
    account_id: int, kind: str, stage: str, channel: str, *, units: int, recipient: str | None = None,
    shortcode: str | None = None, payload: dict | None = None, priority: int = 0, note: str = "",
) -> Job | None:
    """Reserve `units` and queue a job in one transaction. None if the balance is short."""
    try:
        async with pool().acquire() as con, con.transaction():
            row = await con.fetchrow(
                """INSERT INTO jobs (account_id, kind, stage, channel, recipient, shortcode, payload, priority)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *""",
                account_id, kind, stage, channel, recipient, shortcode, payload or {}, priority,
            )
            held = await _reserve(con, account_id, units, note or f"{kind} {shortcode or ''}".strip(), row["id"])
            if held is None:
                raise _ShortBalance
            if held:
                row = await con.fetchrow("UPDATE jobs SET reserved = $2 WHERE id = $1 RETURNING *", row["id"], held)
            await con.execute(f"NOTIFY {JOBS_CHANNEL}")  # delivered to workers on commit
    except _ShortBalance:
        return None
    return _job(row)


_CLAIMABLE = """
    SELECT j.id FROM jobs j
    WHERE (j.status = 'queued' OR (j.status = 'running' AND j.locked_at < now() - $1::interval))
      AND ($2::text IS NULL OR j.stage = $2)
      AND NOT (j.stage = 'analyze' AND EXISTS (
          SELECT 1 FROM jobs r WHERE r.shortcode = j.shortcode AND r.id <> j.id
            AND r.status = 'running' AND r.stage = 'analyze'))
    ORDER BY j.priority DESC, j.id
    FOR UPDATE SKIP LOCKED
    LIMIT $3"""


async def claim_jobs(stage: str | None = None, limit: int = 1) -> list[Job]:
    """Take the next runnable jobs: highest priority, then oldest. A running job whose
    heartbeat stopped (crash, redeploy) is taken over. An analysis waits while the same
    reel is being analyzed elsewhere; the unique index enforces that across machines
    (a lost race just returns nothing)."""
    stale = timedelta(seconds=config.JOB_LOCK_TIMEOUT)
    try:
        async with pool().acquire() as con, con.transaction():
            dead = await con.fetch(
                """SELECT id FROM jobs WHERE status = 'running' AND locked_at < now() - $1::interval
                   AND attempts >= $2 FOR UPDATE SKIP LOCKED""",
                stale, config.JOB_MAX_ATTEMPTS,
            )
            for r in dead:
                await _fail(con, r["id"], "gave up after repeated crashes")
            rows = await con.fetch(
                f"""UPDATE jobs SET status = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
                    WHERE id IN ({_CLAIMABLE}) RETURNING *""",
                stale, stage, limit,
            )
            return sorted((_job(r) for r in rows), key=lambda j: (-j.priority, j.id))
    except asyncpg.UniqueViolationError:
        return []  # another machine claimed this reel's analysis a moment ago


async def claim_job(stage: str | None = None) -> Job | None:
    jobs = await claim_jobs(stage, 1)
    return jobs[0] if jobs else None


async def listen_for_jobs(on_job, dsn: str | None = None) -> asyncpg.Connection:
    """Dedicated connection that calls on_job() whenever a job is queued anywhere.
    Needs a direct (non-pooled) connection; Neon's direct host is the pooled one
    without '-pooler'."""
    dsn = dsn or config.DATABASE_LISTEN_URL or config.DATABASE_URL.replace("-pooler.", ".")
    con = await asyncpg.connect(dsn)
    await con.add_listener(JOBS_CHANNEL, lambda *_: on_job())
    return con


async def heartbeat(job_id: int) -> None:
    await pool().execute("UPDATE jobs SET locked_at = now() WHERE id = $1 AND status = 'running'", job_id)


async def requeue(job_id: int) -> None:
    """Put a job back (e.g. on shutdown) without counting the interrupted attempt."""
    await pool().execute(
        """UPDATE jobs SET status = 'queued', locked_at = NULL, attempts = GREATEST(attempts - 1, 0),
               updated_at = now() WHERE id = $1 AND status = 'running'""",
        job_id,
    )


async def advance(job: Job, stage: str, con=None, shortcode: str | None = None, **payload) -> None:
    """Hand the job to its next stage (fresh attempts), merging `payload`."""
    job.payload.update(payload)
    job.stage = stage
    if shortcode:
        job.shortcode = shortcode
    con = con or pool()
    await con.execute(
        """UPDATE jobs SET stage = $2, status = 'queued', payload = $3, shortcode = COALESCE($4, shortcode),
               attempts = 0, locked_at = NULL, error = NULL, updated_at = now() WHERE id = $1""",
        job.id, stage, job.payload, shortcode,
    )
    await con.execute(f"NOTIFY {JOBS_CHANNEL}")


async def save_analysis(job: Job, reel: Reel, origin: str = "server") -> None:
    """Store the reel globally, add it to the requester's library and move on to delivery."""
    async with pool().acquire() as con, con.transaction():
        await save_reel(con, reel, origin)
        await add_to_library(job.account_id, reel.shortcode, job.channel, con)
        await advance(job, "deliver", con, shortcode=reel.shortcode)


async def finish_job(job_id: int, **payload) -> None:
    await pool().execute(
        """UPDATE jobs SET status = 'done', error = NULL, locked_at = NULL, updated_at = now(),
               payload = payload || $2::jsonb WHERE id = $1""",
        job_id, payload,
    )


async def _fail(con, job_id: int, error: str, refund: bool = True) -> None:
    await con.execute(
        "UPDATE jobs SET status = 'failed', error = $2, locked_at = NULL, updated_at = now() WHERE id = $1",
        job_id, error[:1000],
    )
    if refund:
        await _refund(con, job_id)


async def refund_job(job_id: int) -> None:
    async with pool().acquire() as con, con.transaction():
        await _refund(con, job_id, "not needed")


async def fail_job(job_id: int, error: str, refund: bool = True) -> None:
    """Mark failed and give the held units back (unless the user already got what they paid for)."""
    async with pool().acquire() as con, con.transaction():
        await _fail(con, job_id, error, refund)


async def get_job(job_id: int) -> Job | None:
    return _job(await pool().fetchrow("SELECT * FROM jobs WHERE id = $1", job_id))


async def pending_jobs(account_id: int) -> int:
    return await pool().fetchval(
        "SELECT count(*) FROM jobs WHERE account_id = $1 AND status IN ('queued', 'running')", account_id
    )


async def queue_size() -> int:
    return await pool().fetchval("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")


# ---------------------------------------------------------------- webhooks + settings


async def first_delivery(platform: str, message_id: str, payload: dict | None = None) -> bool:
    """True the first time we see this message (Meta retries deliveries)."""
    return bool(await pool().fetchval(
        """INSERT INTO inbound_events (platform, message_id, payload) VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING RETURNING 1""",
        platform, message_id, payload,
    ))


async def prune_inbound(days: int = 7) -> None:
    await pool().execute("DELETE FROM inbound_events WHERE received_at < now() - $1::interval", timedelta(days=days))


async def kv_get(key: str) -> tuple[str, datetime] | None:
    row = await pool().fetchrow("SELECT value, updated_at FROM kv WHERE key = $1", key)
    return (row["value"], row["updated_at"]) if row else None


async def kv_set(key: str, value: str) -> None:
    await pool().execute(
        """INSERT INTO kv (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()""",
        key, value,
    )


# ---------------------------------------------------------------- API usage


async def record_usage(provider: str, key_id: str, model: str, ok: bool, input_tokens: int, output_tokens: int) -> None:
    await pool().execute(
        """INSERT INTO api_usage (provider, key_id, model, requests, errors, input_tokens, output_tokens)
           VALUES ($1, $2, $3, 1, $4, $5, $6)
           ON CONFLICT (day, provider, key_id, model) DO UPDATE SET
               requests = api_usage.requests + 1, errors = api_usage.errors + EXCLUDED.errors,
               input_tokens = api_usage.input_tokens + EXCLUDED.input_tokens,
               output_tokens = api_usage.output_tokens + EXCLUDED.output_tokens""",
        provider, key_id, model, 0 if ok else 1, input_tokens, output_tokens,
    )
