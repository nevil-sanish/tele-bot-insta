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
