import asyncio

from app import config, pipeline, worker
from channels.instagram import SendError
from fetchers import apify
from fetchers.base import FetchError, MediaFile
from services import analyze
from services.models import Reel


class FakeMessenger:
    def __init__(self, fail: SendError | None = None):
        self.sent: list[tuple[str, str, list | None]] = []
        self.fail = fail

    async def send_text(self, recipient, text, options=None):
        if self.fail:
            raise self.fail
        self.sent.append((recipient, text, options))

    async def typing(self, recipient):
        pass

    async def refresh_token(self, force=False):
        return False


def fake_ai(monkeypatch, tmp_path, calls=None, fail=False):
    async def fake_download(url, mime="video/mp4", timeout=None):
        if "expired" in url:
            raise FetchError("HTTP 403")
        path = tmp_path / f"v{len(list(tmp_path.iterdir()))}.mp4"
        path.write_bytes(b"x")
        return MediaFile(path, mime)

    async def fake_analyze(media, caption):
        if calls is not None:
            calls.append(caption)
        if fail:
            raise analyze.AnalysisError("quota")
        data = analyze.ReelAnalysis(title="Py tips", summary="S", key_ideas=["k"], tags=["coding"])
        return analyze.AnalysisResult(data, "full", "gemini-3.5-flash")

    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(pipeline.analyze, "analyze", fake_analyze)


def fake_apify(monkeypatch, found: dict, calls: list | None = None):
    async def lookup(shortcodes):
        if calls is not None:
            calls.append(sorted(shortcodes))
        return {s: apify.ReelMeta(s, [("https://cdn/v.mp4", "video/mp4")], "Hi #python https://x.io", "bob")
                for s in shortcodes if s in found}

    monkeypatch.setattr(pipeline.apify, "lookup", lookup)


async def _drain(pg, out):
    """Run claimed jobs until the queue is empty (what the worker loop does)."""
    w = worker.Worker(out)
    while jobs := await w._claim():
        await pipeline.run(jobs, out)


async def _account(pg, igsid="u1", units=100):
    acc, _ = await pg.ig_account(igsid)
    await pg.grant(acc.id, units, "adjust", "test")
    return await pg.get_account(acc.id)


async def test_link_reel_goes_fetch_analyze_deliver(pg, monkeypatch, tmp_path):
    calls = []
    fake_ai(monkeypatch, tmp_path, calls)
    fake_apify(monkeypatch, {"SC1"})
    out = FakeMessenger()
    acc = await _account(pg)
    await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="SC1")
    await _drain(pg, out)

    reel = await pg.get_reel("SC1")
    assert reel.author == "bob" and reel.source == "apify" and reel.link.endswith("/reel/SC1/")
    assert "#python" in out.sent[-1][1] and "Py tips" in out.sent[-1][1]
    assert out.sent[-1][2] == pipeline.REEL_OPTIONS
    assert calls == ["Hi #python https://x.io"]
    assert list(tmp_path.iterdir()) == []  # videos are deleted, only text kept
    assert (await pg.get_conversation(acc.id, "ig")).active.shortcode == "SC1"


async def test_fetch_batches_many_reels_into_one_apify_run(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path)
    runs = []
    fake_apify(monkeypatch, {"B1", "B2", "B3"}, runs)
    acc = await _account(pg)
    for code in ("B1", "B2", "B3"):
        await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode=code)
    await _drain(pg, FakeMessenger())
    assert runs == [["B1", "B2", "B3"]]


async def test_shared_reel_with_media_url_skips_apify(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path)
    runs = []
    fake_apify(monkeypatch, set(), runs)
    acc = await _account(pg)
    await pg.create_job(acc.id, "reel", "analyze", "ig", units=10, recipient="u1", shortcode="igv_1",
                        payload={"media": [["https://cdn/x.mp4", "video/mp4"]], "caption": "c", "source": "meta"})
    out = FakeMessenger()
    await _drain(pg, out)
    assert runs == [] and (await pg.get_reel("igv_1")).link == ""  # stand-in keys get no fake link
    assert "Py tips" in out.sent[-1][1]


async def test_expired_webhook_url_falls_back_to_apify(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path)
    runs = []
    fake_apify(monkeypatch, {"EXP"}, runs)
    acc = await _account(pg)
    await pg.create_job(acc.id, "reel", "analyze", "ig", units=10, recipient="u1", shortcode="EXP",
                        payload={"media": [["https://cdn/expired.mp4", "video/mp4"]], "source": "meta"})
    await _drain(pg, FakeMessenger())
    assert runs == [["EXP"]] and (await pg.get_reel("EXP")).source == "apify"


async def test_unfetchable_reel_asks_for_video_and_refunds(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path)
    fake_apify(monkeypatch, set())
    out = FakeMessenger()
    acc = await _account(pg, units=10)
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="GONE")
    await _drain(pg, out)
    assert out.sent[-1][1] == pipeline.ASK_FOR_VIDEO
    assert await pg.pop_pending(acc.id, "ig") == "GONE"
    failed = await pg.get_job(job.id)
    assert failed.status == "failed" and "apify" in failed.error
    assert (await pg.get_account(acc.id)).units == acc.units


async def test_analysis_failure_refunds_and_tells_the_user(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path, fail=True)
    out = FakeMessenger()
    acc = await _account(pg)
    await pg.create_job(acc.id, "reel", "analyze", "ig", units=10, recipient="u1", shortcode="AF",
                        payload={"media": [["https://cdn/x.mp4", "video/mp4"]]})
    await _drain(pg, out)
    assert out.sent[-1][1] == pipeline.ANALYSIS_FAILED
    assert (await pg.get_account(acc.id)).units == acc.units
    assert list(tmp_path.iterdir()) == []


async def test_cached_reel_is_delivered_without_analysis(pg, monkeypatch, tmp_path):
    calls = []
    fake_ai(monkeypatch, tmp_path, calls)
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="OLD", title="Saved before"))
    out = FakeMessenger()
    acc = await _account(pg)
    await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="OLD")
    await _drain(pg, out)
    assert calls == [] and "Saved before" in out.sent[-1][1]
    assert await pg.in_library(acc.id, "OLD")
    assert (await pg.get_account(acc.id)).units == acc.units - 10  # same price (decision #3)


async def test_closed_window_keeps_reel_and_charge(pg, monkeypatch, tmp_path):
    async with pg.pool().acquire() as con:
        await pg.save_reel(con, Reel(shortcode="LATE", title="t"))
    acc = await _account(pg)
    job = await pg.create_job(acc.id, "reel", "deliver", "ig", units=10, recipient="u1", shortcode="LATE")
    await _drain(pg, FakeMessenger(fail=SendError("closed", window_closed=True)))
    assert (await pg.get_job(job.id)).status == "failed"
    assert await pg.in_library(acc.id, "LATE")
    assert (await pg.get_account(acc.id)).units == acc.units - 10


async def test_question_is_answered_and_charged(pg, monkeypatch):
    async def fake_answer(account_id, channel, question):
        return f"answer to {question}"

    monkeypatch.setattr(pipeline.chat, "answer", fake_answer)
    out = FakeMessenger()
    acc = await _account(pg)
    job = await pg.create_job(acc.id, "chat", "chat", "ig", units=1, recipient="u1", payload={"question": "why?"})
    await _drain(pg, out)
    assert out.sent[-1][1] == "answer to why?"
    done = await pg.get_job(job.id)
    assert done.status == "done" and done.payload["reply"] == "answer to why?"
    assert (await pg.get_account(acc.id)).units == acc.units - 1


def test_summary_fits_one_message():
    reel = Reel(shortcode="a", title="T" * 80, author="x", summary="s " * 300,
                key_ideas=["k" * 120] * 5, tools=["t"] * 10, tags=["tag"] * 10, urls=["https://a.io"] * 5)
    text = pipeline.format_summary(reel)
    assert text.startswith("🎬 ") and "Ask me anything" in text
    short = pipeline.format_summary(Reel(shortcode="b", title="T", summary="S", key_ideas=["one"], tags=["x y"]))
    assert len(short.encode()) <= config.DM_MAX_BYTES and "#x_y" in short and "• one" in short


async def test_worker_loop_processes_queued_jobs(pg, monkeypatch, tmp_path):
    fake_ai(monkeypatch, tmp_path)
    fake_apify(monkeypatch, {"W1"})
    out = FakeMessenger()
    w = worker.Worker(out)
    w.start(1, listen=False)
    acc = await _account(pg)
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="W1")
    w.wake()
    for _ in range(100):
        if (await pg.get_job(job.id)).status == "done":
            break
        await asyncio.sleep(0.05)
    await w.stop()
    assert (await pg.get_job(job.id)).status == "done" and out.sent


async def test_shutdown_mid_job_puts_it_back(pg, monkeypatch):
    started = asyncio.Event()

    async def slow_lookup(shortcodes):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(pipeline.apify, "lookup", slow_lookup)
    w = worker.Worker(FakeMessenger())
    w.start(1, listen=False)
    acc = await _account(pg)
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="SLOW")
    w.wake()
    await asyncio.wait_for(started.wait(), 5)
    await w.stop()  # e.g. Render redeploy
    resumed = await pg.get_job(job.id)
    assert resumed.status == "queued" and resumed.attempts == 0 and resumed.reserved == 10


async def test_worker_is_woken_by_postgres_notify(pg, monkeypatch, tmp_path):
    """A worker in another process learns about new jobs only through NOTIFY."""
    from tests.conftest import TEST_DATABASE_URL

    fake_ai(monkeypatch, tmp_path)
    fake_apify(monkeypatch, {"N1"})
    monkeypatch.setattr(config, "DATABASE_LISTEN_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(config, "QUEUE_IDLE_POLL", 3600)  # no polling to fall back on
    w = worker.Worker(FakeMessenger())
    w.start(1)
    await asyncio.sleep(0.5)  # listener connected, loop idle
    acc = await _account(pg)
    job = await pg.create_job(acc.id, "reel", "fetch", "ig", units=10, recipient="u1", shortcode="N1")
    for _ in range(60):
        if (await pg.get_job(job.id)).status == "done":
            break
        await asyncio.sleep(0.1)
    await w.stop()
    assert (await pg.get_job(job.id)).status == "done"


async def test_listener_failure_falls_back_to_polling(pg, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_LISTEN_URL", "postgresql://nobody@127.0.0.1:1/none")
    w = worker.Worker(FakeMessenger())
    w.start(1)
    for _ in range(50):
        if w.idle_poll == config.QUEUE_FALLBACK_POLL:
            break
        await asyncio.sleep(0.1)
    await w.stop()
    assert w.idle_poll == config.QUEUE_FALLBACK_POLL


def test_config_defaults_sane():
    assert config.JOB_LOCK_TIMEOUT > config.JOB_HEARTBEAT * 2
