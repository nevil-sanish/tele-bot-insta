"""Public pages Meta's App Review and Google Play require (privacy policy, terms,
data deletion), Meta's data-deletion callback, and a small admin stats endpoint."""

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
from datetime import date

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import config
from services import billing, db

log = logging.getLogger(__name__)
router = APIRouter(tags=["pages"])
UPDATED = date(2026, 10, 18)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · Reel Brain</title>
<style>
 :root {{ color-scheme: light dark; --fg:#1c1b1f; --bg:#fffbfe; --muted:#5f5b66; --accent:#6750a4; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e6e1e5; --bg:#1c1b1f; --muted:#b9b4be; --accent:#d0bcff; }} }}
 body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 system-ui, sans-serif; }}
 main {{ max-width: 720px; margin: 0 auto; padding: 32px 16px 64px; }}
 h1 {{ font-size: 1.8rem; margin-bottom: 0; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
 .muted {{ color: var(--muted); }} a {{ color: var(--accent); }} li {{ margin: .3rem 0; }}
</style></head><body><main>{body}</main></body></html>""")


def _contact() -> str:
    parts = []
    if config.CONTACT_EMAIL:
        e = html.escape(config.CONTACT_EMAIL)
        parts.append(f'email <a href="mailto:{e}">{e}</a>')
    if config.BOT_USERNAME:
        u = html.escape(config.BOT_USERNAME.lstrip("@"))
        parts.append(f'DM <a href="https://ig.me/m/{u}">@{u}</a> on Instagram')
    return " or ".join(parts) or "DM the Reel Brain account on Instagram"


@router.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return _page("Reel Brain", f"""
<h1>Reel Brain 🧠</h1>
<p>Share an Instagram reel with Reel Brain in your DMs and get a short summary back. Every reel is saved to your
own searchable library, and you can ask questions about anything you've saved.</p>
<p>Contact: {_contact()}.</p>
<p class="muted"><a href="/privacy">Privacy policy</a> · <a href="/terms">Terms</a> · <a href="/data-deletion">Delete your data</a></p>""")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    return _page("Privacy policy", f"""
<h1>Privacy policy</h1><p class="muted">Last updated {UPDATED:%d %B %Y}</p>
<p>Reel Brain ("we") turns Instagram reels you share with us into notes you can search and ask questions about.
This policy explains what we collect, why, and how to delete it.</p>

<h2>What we collect</h2>
<ul>
<li><b>Instagram messages you send to our account</b>: your Instagram-scoped user ID (a number Meta gives our app,
not your username or password), the text you send, and links or media you share with us.</li>
<li><b>App sign-in</b> (Android app): your Google account ID, email address and name.</li>
<li><b>Your library</b>: which reels you saved, the questions you asked and our answers (the last few turns).</li>
<li><b>Credits and payments</b>: your balance, plan and payment status. Card and UPI details are handled by
Razorpay or Google Play; we never see or store them.</li>
<li><b>AI keys</b> you add in the app stay on your phone (Android Keystore) and are never sent to us.</li>
</ul>

<h2>What we do with it</h2>
<ul>
<li>Fetch the reel you shared, analyze it with AI, and send you the summary.</li>
<li>Keep the summary in a shared reel library so the same public reel isn't analyzed twice. Summaries describe
public reels and are not linked to you for anyone else; your personal library is visible only to you.</li>
<li>Answer your questions, keep track of credits, and process subscriptions.</li>
</ul>
<p>Videos are deleted right after analysis; we keep only text. We don't sell your data, show ads, or use it for
anything else.</p>

<h2>Who processes data for us</h2>
<ul>
<li>Meta (Instagram messaging), Google (Gemini AI, Google sign-in, Google Play billing), Groq (AI backup and
answers), Apify (fetching public reels), Razorpay (payments in India).</li>
<li>Render (hosting) and Neon (database).</li>
</ul>
<p>Each of them only gets what it needs for its part.</p>

<h2>How long we keep it</h2>
<p>Your library and account data stay until you delete them. Raw message deliveries are kept for 7 days for
debugging. Payment records are kept as long as the law requires.</p>

<h2>Deleting your data</h2>
<p>Send <b>delete</b> to our Instagram account, use <i>Delete account</i> in the app, or see
<a href="/data-deletion">data deletion</a>. Deletion is immediate and permanent.</p>

<h2>Children</h2><p>Reel Brain is not meant for children under 13.</p>
<h2>Contact</h2><p>{_contact()}.</p>""")


@router.get("/terms", response_class=HTMLResponse)
async def terms() -> HTMLResponse:
    return _page("Terms", f"""
<h1>Terms of use</h1><p class="muted">Last updated {UPDATED:%d %B %Y}</p>
<ul>
<li>Reel Brain summarizes content you choose to share with it, for your personal use. Summaries are made by AI and
can be wrong — check anything important against the original.</li>
<li>Only share content you're allowed to share. Reels belong to their creators; our summaries link back to them.</li>
<li>Free credits are given once per person. Plans renew monthly until you cancel ("cancel" in the DM, or Google Play
for app purchases); unused credits don't carry over to the next month. Credits for a reel that fails are refunded
automatically.</li>
<li>Don't abuse the service (spam, automated use, attempts to break it). We may suspend accounts that do.</li>
<li>The service is provided as is. Contact: {_contact()}.</li>
</ul>""")


@router.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion(code: str = "") -> HTMLResponse:
    status = ""
    if code:
        done = await db.kv_get(f"deletion:{code}")
        status = (f"<p><b>Request {html.escape(code)}:</b> "
                  f"{'completed — your data has been deleted.' if done else 'not found.'}</p>")
    return _page("Delete your data", f"""
<h1>Delete your data</h1>{status}
<p>You can erase everything Reel Brain stores about you at any time:</p>
<ul>
<li><b>Instagram</b>: send <b>delete</b> to our account, then confirm.</li>
<li><b>Android app</b>: Profile → Delete account.</li>
<li><b>Or</b> {_contact()} and we'll do it for you.</li>
</ul>
<p>Deletion is immediate: your library, questions, credits, linked logins and stored messages are removed.
A renewing plan is cancelled. Reel summaries in the shared library describe public reels and are not linked to you.</p>""")


def parse_signed_request(signed_request: str, secret: str) -> dict | None:
    """Meta's signed_request: base64url(signature).base64url(json), HMAC-SHA256 with the app secret."""
    try:
        sig_b64, payload_b64 = signed_request.split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not secret or not hmac.compare_digest(sig, expected):
            return None
        return json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
    except (ValueError, TypeError):
        return None


@router.post("/meta/data-deletion")
async def meta_data_deletion(request: Request, signed_request: str = Form(...)) -> dict:
    """Meta's Data Deletion Request callback (set it in the Meta app's settings)."""
    data = parse_signed_request(signed_request, config.INSTAGRAM_APP_SECRET)
    if not data or not data.get("user_id"):
        raise HTTPException(400, "invalid signed_request")
    account_id = await db.account_for_identity("ig", str(data["user_id"]))
    if account_id:
        await billing.stop_renewals(account_id)
        await db.delete_account(account_id)
    code = secrets.token_hex(8)
    await db.kv_set(f"deletion:{code}", "done")
    base = config.PUBLIC_URL.rstrip("/") or str(request.base_url).rstrip("/")
    return {"url": f"{base}/data-deletion?code={code}", "confirmation_code": code}


@router.get("/admin/stats")
async def stats(authorization: str = Header(default="")) -> dict:
    """Operational numbers for the last 24 h. Authorization: Bearer ADMIN_TOKEN."""
    if not config.ADMIN_TOKEN or not hmac.compare_digest(authorization, f"Bearer {config.ADMIN_TOKEN}"):
        raise HTTPException(401)
    p = db.pool()
    jobs = await p.fetch(
        """SELECT kind, status, count(*) AS n, round(avg(extract(epoch FROM updated_at - created_at))) AS avg_s
           FROM jobs WHERE created_at > now() - interval '24 hours' GROUP BY kind, status ORDER BY kind, status"""
    )
    usage = await p.fetch(
        """SELECT provider, sum(requests) AS requests, sum(errors) AS errors,
                  sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens
           FROM api_usage WHERE day = CURRENT_DATE GROUP BY provider ORDER BY provider"""
    )
    plans = await p.fetch("SELECT plan, count(*) AS n FROM accounts GROUP BY plan ORDER BY plan")
    errors = await p.fetch(
        """SELECT split_part(error, ':', 1) AS stage, count(*) AS n FROM jobs
           WHERE status = 'failed' AND created_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC"""
    )
    return {
        "accounts": await p.fetchval("SELECT count(*) FROM accounts"),
        "new_accounts_24h": await p.fetchval("SELECT count(*) FROM accounts WHERE created_at > now() - interval '24 hours'"),
        "accounts_by_plan": {r["plan"]: r["n"] for r in plans},
        "reels_cached": await p.fetchval("SELECT count(*) FROM reels"),
        "queue": await db.queue_size(),
        "jobs_24h": [dict(r) for r in jobs],
        "failures_24h": {r["stage"]: r["n"] for r in errors},
        "api_usage_today": [dict(r) for r in usage],
        "purchases_24h": await p.fetchval(
            "SELECT count(*) FROM ledger WHERE kind = 'purchase' AND created_at > now() - interval '24 hours'"
        ),
    }
