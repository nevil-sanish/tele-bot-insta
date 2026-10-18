import asyncio

from app import config, worker
from fetchers.base import FetchError, FetchResult, MediaFile
from services import analyze, chat
from services.models import Reel


class FakeBot:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.edits.append(text)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


URL = "https://www.instagram.com/reel/SC123/"


async def _job(pg, telegram_id=7, chat_id=7):
    user = await pg.telegram_user(telegram_id)
    await pg.enqueue(user.id, "SC123", url=URL, chat_id=chat_id, status_message_id=1)
    return await pg.claim_job()


def _fake_pipeline(monkeypatch, tmp_path, calls=None):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    async def fake_fetch(url):
        return FetchResult("apify", [MediaFile(video, "video/mp4")], caption="Hi #python https://x.io", author="bob")

    async def fake_analyze(media, caption):
        if calls is not None:
            calls.append(caption)
        data = analyze.ReelAnalysis(title="Py <tips>", summary="S", key_ideas=["k"], tags=["coding"])
        return analyze.AnalysisResult(data, "full", "gemini-3.5-flash")

    monkeypatch.setattr(worker.chain, "fetch", fake_fetch)
    monkeypatch.setattr(worker.analyze, "analyze", fake_analyze)
    return video


async def test_full_pipeline_saves_reel_and_deletes_video(pg, monkeypatch, tmp_path):
    video = _fake_pipeline(monkeypatch, tmp_path)
    bot = FakeBot()
    job = await _job(pg)
    await worker.process(bot, job)

    assert not video.exists()  # only text is kept
    assert bot.edits[:3] == ["📥 Fetching…", "🧠 Analyzing…", "💾 Saving…"]
    final = bot.edits[-1]
    assert "Py &lt;tips&gt;" in final and "#coding" in final and "#python" in final and "https://x.io" in final
    saved = await pg.get_reel("SC123")
    assert saved.author == "bob" and saved.model == "gemini-3.5-flash" and saved.link == URL
    assert (await pg.get_job(job.id)).status == "done"
    assert chat.state(7).active.shortcode == "SC123"


async def test_second_request_uses_the_cache(pg, monkeypatch, tmp_path):
    calls = []
    _fake_pipeline(monkeypatch, tmp_path, calls)
    await worker.process(FakeBot(), await _job(pg))
    bot = FakeBot()
    other = await pg.telegram_user(8)
    await pg.enqueue(other.id, "SC123", url=URL, chat_id=8, status_message_id=2)
    await worker.process(bot, await pg.claim_job())
    assert len(calls) == 1  # analyzed once, shared by both users
    assert "from the library" in bot.edits[-1]
    assert [r.shortcode for r in await pg.library(other.id)] == ["SC123"]


async def test_fetch_failure_fails_job_and_asks_for_forward(pg, monkeypatch):
    async def broken(url):
        raise FetchError("apify: 403")

    monkeypatch.setattr(worker.chain, "fetch", broken)
    bot = FakeBot()
    job = await _job(pg)
    await worker.process(bot, job)
    assert "Forward me the video" in bot.edits[-1]
    assert chat.state(7).pending_link == URL
    failed = await pg.get_job(job.id)
    assert failed.status == "failed" and "apify: 403" in failed.error


async def test_analysis_failure_still_deletes_video(pg, monkeypatch, tmp_path):
    video = _fake_pipeline(monkeypatch, tmp_path)

    async def boom(media, caption):
        raise analyze.AnalysisError("quota")

    monkeypatch.setattr(worker.analyze, "analyze", boom)
    bot = FakeBot()
    job = await _job(pg)
    await worker.process(bot, job)
    assert not video.exists()
    assert "Analysis failed" in bot.edits[-1]
    assert (await pg.get_job(job.id)).status == "failed"


async def test_worker_loop_processes_queued_jobs(pg, monkeypatch, tmp_path):
    _fake_pipeline(monkeypatch, tmp_path)
    w = worker.Worker(FakeBot())
    w.start(1)
    user = await pg.telegram_user(9)
    reel, job = await w.submit(user.id, "SC123", url=URL, chat_id=9, status_message_id=3)
    assert reel is None
    for _ in range(50):
        if (await pg.get_job(job.id)).status == "done":
            break
        await asyncio.sleep(0.05)
    await w.stop()
    assert (await pg.get_job(job.id)).status == "done"
    # Saved now: the next submit is answered from the cache without a job.
    reel, job = await w.submit(user.id, "SC123", url=URL)
    assert reel.title == "Py <tips>" and job is None


async def test_shutdown_mid_job_puts_it_back(pg, monkeypatch):
    started = asyncio.Event()

    async def slow_fetch(url):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(worker.chain, "fetch", slow_fetch)
    w = worker.Worker(FakeBot())
    w.start(1)
    user = await pg.telegram_user(10)
    _, job = await w.submit(user.id, "SC123", url=URL, chat_id=10, status_message_id=4)
    await asyncio.wait_for(started.wait(), 5)
    await w.stop()  # e.g. Render redeploy
    resumed = await pg.get_job(job.id)
    assert resumed.status == "queued" and resumed.attempts == 0


def test_format_result_marks_cache_and_mode():
    text = worker.format_result(Reel(shortcode="a", title="T", analysis="audio-only"), cached=True)
    assert "from the library" in text and "audio-only" in text


def test_shortcode_for_links_and_forwards():
    assert worker.shortcode_for(URL) == "SC123"
    assert worker.shortcode_for(None, "UQ") == "tg_UQ"


def test_config_defaults_sane():
    assert config.JOB_LOCK_TIMEOUT > config.JOB_HEARTBEAT * 2
