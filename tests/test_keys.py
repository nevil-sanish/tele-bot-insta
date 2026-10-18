import pytest

from app import config
from fetchers.base import MediaFile
from services import analyze, keys


class Quota(Exception):
    code = 429


class Overloaded(Exception):
    code = 503


def test_rest_seconds_classifies_limits():
    assert keys.rest_seconds(Overloaded("busy")) is None
    assert keys.rest_seconds(ValueError("bad json")) is None
    assert keys.rest_seconds(Quota("RESOURCE_EXHAUSTED retry in 12.5s")) == 12.5
    assert keys.rest_seconds(Quota("Too many requests")) == 60
    daily = keys.rest_seconds(Quota("quota GenerateRequestsPerDayPerProjectPerModel exceeded"))
    assert 0 < daily <= 86400 + 10
    assert keys.rest_seconds(Quota("Rate limit reached on requests per day (RPD)")) > 60


def test_pool_rotates_least_used_and_skips_resting():
    pool = keys.KeyPool("x", ["a", "b", "a", ""], 2)
    assert len(pool) == 2  # duplicates and blanks dropped
    first = pool.candidates("m")[0]
    pool.used(first)
    assert pool.candidates("m")[0] is not first  # least used goes first
    pool.rest(first, "m", 60)
    assert first not in pool.candidates("m")
    assert first in pool.candidates("other-model")  # resting is per model
    pool.rest(first, keys.ANY, 60)
    assert first not in pool.candidates("other-model")
    assert all(k.id != "a" for k in pool.keys)  # ids are hashes, never the key


@pytest.fixture
def two_gemini_keys(monkeypatch, tmp_path):
    pool = keys.KeyPool("gemini", ["key-1", "key-2"], 4)
    monkeypatch.setattr(keys, "gemini", pool)
    monkeypatch.setattr(config, "GEMINI_MODELS", ["best", "backup"])
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    return pool, [MediaFile(video, "video/mp4")]


async def test_gemini_rotates_keys_on_quota_then_succeeds(monkeypatch, two_gemini_keys):
    pool, media = two_gemini_keys
    calls = []

    async def fake_call(key, model, parts, caption):
        calls.append((key.value, model))
        if key.value == "key-1":
            raise Quota("RESOURCE_EXHAUSTED per minute")
        return analyze.AnalysisResult(analyze.ReelAnalysis(title="ok"), "full", model)

    monkeypatch.setattr(analyze, "_gemini_call", fake_call)
    result = await analyze._analyze_gemini(media, "cap")
    assert result.model == "best"
    assert ("key-1", "best") in calls and calls[-1] == ("key-2", "best")
    assert [k.value for k in pool.candidates("best")] == ["key-2"]  # key-1 resting


async def test_gemini_overload_moves_to_next_model(monkeypatch, two_gemini_keys):
    _, media = two_gemini_keys
    calls = []

    async def fake_call(key, model, parts, caption):
        calls.append(model)
        if model == "best":
            raise Overloaded("503 UNAVAILABLE")
        return analyze.AnalysisResult(analyze.ReelAnalysis(title="ok"), "full", model)

    monkeypatch.setattr(analyze, "_gemini_call", fake_call)
    result = await analyze._analyze_gemini(media, None)
    assert result.model == "backup" and calls == ["best", "backup"]  # no pointless 2nd key on overload


async def test_all_keys_limited_raises(monkeypatch, two_gemini_keys):
    pool, media = two_gemini_keys

    async def fake_call(key, model, parts, caption):
        raise Quota("quota exceeded per day")

    monkeypatch.setattr(analyze, "_gemini_call", fake_call)
    with pytest.raises(analyze.AnalysisError):
        await analyze._analyze_gemini(media, None)
    assert pool.candidates("best") == [] and pool.candidates("backup") == []


async def test_groq_call_rotates_on_rate_limit(monkeypatch):
    pool = keys.KeyPool("groq", ["g1", "g2"], 4)
    monkeypatch.setattr(keys, "groq", pool)
    used = []

    async def call(client):
        used.append(client.api_key)
        if client.api_key == "g1":
            raise Quota("Rate limit reached ... Please try again in 2m30s")
        return "ok"

    assert await analyze.groq_call("whisper", call) == "ok"
    assert used == ["g1", "g2"]
