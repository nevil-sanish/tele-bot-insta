from app import worker
from fetchers.base import FetchError, FetchResult, MediaFile
from services import analyze, chat
from services.notion_store import Reel


class FakeBot:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.edits.append(text)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def _job(url="https://www.instagram.com/reel/SC123/"):
    return worker.Job(chat_id=7, status_message_id=1, url=url)


async def test_full_pipeline_deletes_video_and_sets_active(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    async def fake_fetch(url):
        return FetchResult("apify", [MediaFile(video, "video/mp4")], caption="Hi #python https://x.io", author="bob")

    async def fake_analyze(media, caption):
        assert caption.startswith("Hi")
        data = analyze.ReelAnalysis(title="Py <tips>", summary="S", key_ideas=["k"], tags=["coding"])
        return analyze.AnalysisResult(data, "full", "m")

    monkeypatch.setattr(worker.chain, "fetch", fake_fetch)
    monkeypatch.setattr(worker.analyze, "analyze", fake_analyze)
    bot = FakeBot()
    await worker.process(bot, _job())

    assert not video.exists()  # only text is kept
    assert bot.edits[:2] == ["📥 Fetching…", "🧠 Analyzing…"]
    final = bot.edits[-1]
    assert "Py &lt;tips&gt;" in final and "#coding" in final and "#python" in final and "https://x.io" in final
    active = chat.state(7).active
    assert active.shortcode == "SC123" and active.author == "bob" and active.source == "apify"


async def test_fetch_failure_asks_for_forward(monkeypatch):
    async def broken(url):
        raise FetchError("apify: 403")

    monkeypatch.setattr(worker.chain, "fetch", broken)
    bot = FakeBot()
    await worker.process(bot, _job())
    assert "Forward me the video" in bot.edits[-1]
    assert chat.state(7).pending_link.endswith("/reel/SC123/")


async def test_analysis_failure_still_deletes_video(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    async def fake_fetch(url):
        return FetchResult("apify", [MediaFile(video, "video/mp4")], caption="c")

    async def boom(media, caption):
        raise analyze.AnalysisError("quota")

    monkeypatch.setattr(worker.chain, "fetch", fake_fetch)
    monkeypatch.setattr(worker.analyze, "analyze", boom)
    bot = FakeBot()
    await worker.process(bot, _job())
    assert not video.exists()
    assert "Analysis failed" in bot.edits[-1]


def test_worker_dedupes_in_flight():
    w = worker.Worker(bot=None)
    assert w.submit(_job()) is True
    assert w.submit(_job()) is False


def test_forwarded_job_shortcode():
    job = worker.Job(chat_id=1, status_message_id=1, file_id="f", file_unique_id="UQ")
    assert job.shortcode == "tg_UQ"


def test_format_result_marks_cache_and_mode():
    text = worker.format_result(Reel(shortcode="a", title="T", analysis="audio-only"), cached=True)
    assert "from your library" in text and "audio-only" in text
