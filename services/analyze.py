"""One heavy AI call per reel.

Gemini gets the video + caption and downsamples it itself (fps 0.5, LOW
resolution, first 90 s), so Render never re-encodes anything. If every Gemini
model fails, ffmpeg pulls the audio track (stream copy, no re-encode), Groq
Whisper transcribes it and a Groq LLM fills in the same JSON.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from app import config
from fetchers.base import MediaFile, new_tmp_path

log = logging.getLogger(__name__)


class ReelAnalysis(BaseModel):
    title: str = ""
    summary: str = ""
    key_ideas: list[str] = []
    on_screen_text: str = ""
    transcript: str = ""
    tools: list[str] = []
    tags: list[str] = []
    category: str = ""


_FIELDS = list(ReelAnalysis.model_fields)
_LIST_FIELDS = {"key_ideas", "tools", "tags"}
# Every field required, no defaults: the model always fills the full shape.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        k: {"type": "ARRAY", "items": {"type": "STRING"}} if k in _LIST_FIELDS else {"type": "STRING"}
        for k in _FIELDS
    },
    "required": _FIELDS,
    "property_ordering": _FIELDS,
}


@dataclass
class AnalysisResult:
    data: ReelAnalysis
    mode: str  # full / audio-only / caption-only
    model: str
    usage: dict = field(default_factory=dict)


class AnalysisError(Exception):
    pass


INSTRUCTIONS = """You extract knowledge from an Instagram reel/post for a personal notes library.
Return JSON only, matching the schema. Rules:
- title: short and specific (max 10 words).
- summary: at most 4 sentences.
- key_ideas: at most 5 short bullet points.
- on_screen_text: important text shown on screen, deduplicated; "" if none.
- transcript: verbatim speech if it is 150 words or fewer, otherwise a condensed version; "" if no speech.
- tools: apps, products, libraries, websites or books mentioned.
- tags: 3-6 lowercase topic tags without '#'.
- category: one word or short phrase (e.g. tech, fitness, finance, cooking, productivity).
- Do NOT repeat the caption, its URLs or its hashtags; they are stored separately.
- Write in English."""


def _prompt(caption: str | None) -> str:
    caption = (caption or "").strip()
    return f"{INSTRUCTIONS}\n\nCaption (context only):\n{caption[:3000] if caption else '(none)'}"


# ---------------------------------------------------------------- Gemini

_gemini_client = None


def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def _thinking_options():
    """Minimal thinking first (Gemini 3), then budget 0 (2.5), then default."""
    from google.genai import types

    return [
        types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        types.ThinkingConfig(thinking_budget=0),
        None,
    ]


async def _media_parts(media: list[MediaFile], uploaded: list):
    """Build Gemini parts. Inline if the request stays under ~20 MB, else Files API."""
    from google.genai import types

    client = _gemini()
    total = sum(m.path.stat().st_size for m in media)
    inline = total <= config.INLINE_LIMIT_MB * 1024 * 1024
    video_meta = types.VideoMetadata(
        fps=config.VIDEO_FPS, start_offset="0s", end_offset=f"{config.VIDEO_MAX_SECONDS}s"
    )
    parts = []
    for m in media:
        meta = video_meta if m.is_video else None
        if inline:
            blob = types.Blob(data=await asyncio.to_thread(m.path.read_bytes), mime_type=m.mime_type)
            parts.append(types.Part(inline_data=blob, video_metadata=meta))
            continue
        f = await client.aio.files.upload(file=str(m.path), config={"mime_type": m.mime_type})
        uploaded.append(f)
        for _ in range(60):  # videos need a few seconds of server-side processing
            if f.state is None or f.state.name != "PROCESSING":
                break
            await asyncio.sleep(2)
            f = await client.aio.files.get(name=f.name)
        if f.state is not None and f.state.name == "FAILED":
            raise AnalysisError("Gemini Files API could not process the video")
        parts.append(
            types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=m.mime_type), video_metadata=meta)
        )
    return parts


def _is_bad_request(e: Exception) -> bool:
    return getattr(e, "code", None) == 400


async def _gemini_call(model: str, parts: list, caption: str | None) -> AnalysisResult:
    from google.genai import types

    contents = [types.Content(role="user", parts=[*parts, types.Part(text=_prompt(caption))])]
    last: Exception | None = None
    for thinking in _thinking_options():
        cfg = types.GenerateContentConfig(
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            max_output_tokens=2048,
            temperature=0.2,
            thinking_config=thinking,
        )
        try:
            resp = await asyncio.wait_for(
                _gemini().aio.models.generate_content(model=model, contents=contents, config=cfg),
                timeout=config.GEMINI_TIMEOUT,
            )
        except Exception as e:
            # 400 here usually means this model rejects this thinking setting.
            if _is_bad_request(e) and thinking is not None:
                last = e
                continue
            raise
        data = _parse_json(resp.text)
        u = resp.usage_metadata
        usage = {
            "input": getattr(u, "prompt_token_count", None),
            "output": getattr(u, "candidates_token_count", None),
            "thinking": getattr(u, "thoughts_token_count", None),
        } if u else {}
        return AnalysisResult(data=data, mode="full", model=model, usage=usage)
    raise last or AnalysisError("Gemini call failed")


async def _analyze_gemini(media: list[MediaFile], caption: str | None) -> AnalysisResult:
    if not config.GEMINI_API_KEY:
        raise AnalysisError("GEMINI_API_KEY not set")
    uploaded: list = []
    try:
        parts = await _media_parts(media, uploaded)
        errors = []
        for model in config.GEMINI_MODELS:
            try:
                result = await _gemini_call(model, parts, caption)
                log.info("analysis ok: model=%s usage=%s", model, result.usage)
                return result
            except Exception as e:  # 429 quota, 5xx, timeouts: try the next model
                errors.append(f"{model}: {_short(e)}")
                log.warning("gemini failed: %s", errors[-1])
        raise AnalysisError("; ".join(errors))
    finally:
        for f in uploaded:
            try:
                await _gemini().aio.files.delete(name=f.name)
            except Exception:
                pass


# ---------------------------------------------------------------- Groq backup

_groq_client = None


def groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import AsyncGroq

        _groq_client = AsyncGroq(api_key=config.GROQ_API_KEY)
    return _groq_client


async def extract_audio(video: MediaFile) -> MediaFile | None:
    """Copy the audio stream out of the video (no re-encode). None if no audio."""
    out = new_tmp_path(".m4a")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(video.path), "-vn", "-c:a", "copy",
        "-t", str(config.VIDEO_MAX_SECONDS), str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.info("no audio extracted: %s", err.decode(errors="ignore")[-200:])
        out.unlink(missing_ok=True)
        return None
    return MediaFile(out, "audio/mp4")


async def transcribe(audio: MediaFile) -> str:
    with audio.path.open("rb") as f:
        data = f.read()
    resp = await groq_client().audio.transcriptions.create(
        model=config.GROQ_WHISPER_MODEL,
        file=(audio.path.name, data),
        response_format="text",
    )
    return (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()


async def groq_json(system: str, user: str, max_tokens: int = 1500) -> dict:
    """Groq chat call that must return a JSON object; tries each chat model."""
    errors = []
    for model in config.GROQ_CHAT_MODELS:
        try:
            resp = await groq_client().chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                reasoning_effort="low",
                max_completion_tokens=max_tokens,
                temperature=0.2,
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            errors.append(f"{model}: {_short(e)}")
            log.warning("groq json failed: %s", errors[-1])
    raise AnalysisError("; ".join(errors))


async def _analyze_groq(media: list[MediaFile], caption: str | None) -> AnalysisResult:
    if not config.GROQ_API_KEY:
        raise AnalysisError("GROQ_API_KEY not set")
    transcript = ""
    video = next((m for m in media if m.is_video), None)
    if video:
        audio = await extract_audio(video)
        if audio:
            try:
                transcript = await transcribe(audio)
            finally:
                audio.path.unlink(missing_ok=True)
    if not transcript and not (caption or "").strip():
        raise AnalysisError("no speech and no caption to analyze")

    schema = json.dumps(ReelAnalysis().model_dump())
    user = (
        f"{_prompt(caption)}\n\nYou cannot see the video; on_screen_text must be \"\".\n"
        f"Schema: {schema}\n\nSpeech transcript:\n{transcript[:8000] or '(no speech)'}"
    )
    data = ReelAnalysis.model_validate(_clean(await groq_json("Return only a JSON object.", user)))
    if not data.transcript and transcript:
        data.transcript = transcript[:3000]
    mode = "audio-only" if transcript else "caption-only"
    return AnalysisResult(data=data, mode=mode, model="groq")


# ---------------------------------------------------------------- entry point


async def analyze(media: list[MediaFile], caption: str | None) -> AnalysisResult:
    errors = []
    try:
        return await _analyze_gemini(media, caption)
    except Exception as e:
        errors.append(f"gemini: {_short(e)}")
    try:
        return await _analyze_groq(media, caption)
    except Exception as e:
        errors.append(f"groq: {_short(e)}")
    raise AnalysisError(" | ".join(errors))


def _clean(raw: dict) -> dict:
    """Coerce whatever the LLM returned into the schema's types."""
    out = {}
    for name in _FIELDS:
        v = raw.get(name)
        if name in _LIST_FIELDS:
            if isinstance(v, str):
                v = [v] if v else []
            out[name] = [str(x).strip() for x in (v or []) if str(x).strip()]
        else:
            out[name] = "" if v is None else (v if isinstance(v, str) else json.dumps(v))
    return out


def _parse_json(text: str | None) -> ReelAnalysis:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return ReelAnalysis.model_validate(_clean(json.loads(text)))
    except ValueError as e:
        raise AnalysisError(f"model returned invalid JSON: {e}") from e


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}"
