import pytest

from fetchers.base import MediaFile
from services import analyze


def test_parse_json_coerces_types():
    data = analyze._parse_json('```json\n{"title":"x","tags":"ai","key_ideas":null,"transcript":{"a":1}}\n```')
    assert data.title == "x" and data.tags == ["ai"] and data.key_ideas == [] and data.transcript == '{"a": 1}'


def test_parse_json_rejects_garbage():
    with pytest.raises(analyze.AnalysisError):
        analyze._parse_json("not json")


async def test_falls_back_to_groq_when_gemini_fails(monkeypatch, tmp_path):
    async def gemini_down(media, caption):
        raise analyze.AnalysisError("429 everywhere")

    async def groq_ok(media, caption):
        return analyze.AnalysisResult(analyze.ReelAnalysis(title="t"), "audio-only", "groq")

    monkeypatch.setattr(analyze, "_analyze_gemini", gemini_down)
    monkeypatch.setattr(analyze, "_analyze_groq", groq_ok)
    result = await analyze.analyze([MediaFile(tmp_path / "v.mp4", "video/mp4")], "cap")
    assert result.mode == "audio-only"


async def test_both_fail_raises(monkeypatch):
    async def down(media, caption):
        raise RuntimeError("down")

    monkeypatch.setattr(analyze, "_analyze_gemini", down)
    monkeypatch.setattr(analyze, "_analyze_groq", down)
    with pytest.raises(analyze.AnalysisError) as e:
        await analyze.analyze([], "")
    assert "gemini" in str(e.value) and "groq" in str(e.value)


def test_schema_requires_every_field():
    assert set(analyze.RESPONSE_SCHEMA["required"]) == set(analyze.ReelAnalysis.model_fields)


async def test_thinking_fallback_is_remembered_per_model(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class BadRequest(Exception):
        code = 400

    async def fake_generate(model, contents, config):
        calls.append(config.thinking_config)
        if config.thinking_config.thinking_level is not None and config.thinking_config.thinking_level.name == "MINIMAL":
            raise BadRequest("Thinking level MINIMAL is not supported")
        return SimpleNamespace(text='{"title": "t"}', usage_metadata=None)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
    monkeypatch.setattr(analyze, "_gemini", lambda key: client)
    analyze._thinking_ok.clear()
    key = analyze.keys.Key("k", "id")
    await analyze._gemini_call(key, "m", [], "cap")
    assert len(calls) == 2  # MINIMAL rejected, LOW worked
    await analyze._gemini_call(key, "m", [], "cap")
    assert len(calls) == 3  # second reel goes straight to LOW
