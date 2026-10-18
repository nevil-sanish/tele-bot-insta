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

from . import keys
from .spec import FIELDS, LIST_FIELDS, SPEC

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


_FIELDS = FIELDS
_LIST_FIELDS = LIST_FIELDS
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


INSTRUCTIONS = SPEC["instructions"]


def _prompt(caption: str | None) -> str:
    caption = (caption or "").strip()
    return f"{INSTRUCTIONS}\n\nCaption (context only):\n{caption[:SPEC['caption_max_chars']] if caption else '(none)'}"


# ---------------------------------------------------------------- Gemini

_gemini_clients: dict[str, object] = {}  # key id -> genai.Client


def _gemini(key: keys.Key):
    if key.id not in _gemini_clients:
        from google import genai

        _gemini_clients[key.id] = genai.Client(api_key=key.value)
    return _gemini_clients[key.id]


def _thinking_options():
    """Cheapest thinking first. Models reject levels they don't support (e.g.
    3.8 Flash has no MINIMAL), so we step down this list on a 400."""
    from google.genai import types

    return [
        types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        types.ThinkingConfig(thinking_budget=0),
        None,
    ]


# model -> index into _thinking_options() that worked, so later reels skip the failed tries.
_thinking_ok: dict[str, int] = {}


def _video_meta():
    from google.genai import types

    return types.VideoMetadata(fps=config.VIDEO_FPS, start_offset="0s", end_offset=f"{config.VIDEO_MAX_SECONDS}s")


async def _inline_parts(media: list[MediaFile]) -> list:
    """Media sent inside the request. Works with any key."""
    from google.genai import types

    parts = []
    for m in media:
        blob = types.Blob(data=await asyncio.to_thread(m.path.read_bytes), mime_type=m.mime_type)
        parts.append(types.Part(inline_data=blob, video_metadata=_video_meta() if m.is_video else None))
    return parts


async def _uploaded_parts(key: keys.Key, media: list[MediaFile], uploaded: list) -> list:
    """Files API upload for large media. Uploads belong to one key's project."""
    from google.genai import types

    client = _gemini(key)
    parts = []
    for m in media:
        f = await client.aio.files.upload(file=str(m.path), config={"mime_type": m.mime_type})
        uploaded.append((key, f))
        for _ in range(60):  # videos need a few seconds of server-side processing
            if f.state is None or f.state.name != "PROCESSING":
                break
            await asyncio.sleep(2)
            f = await client.aio.files.get(name=f.name)
        if f.state is not None and f.state.name == "FAILED":
            raise AnalysisError("Gemini Files API could not process the video")
        parts.append(types.Part(
            file_data=types.FileData(file_uri=f.uri, mime_type=m.mime_type),
            video_metadata=_video_meta() if m.is_video else None,
        ))
    return parts


def _is_bad_request(e: Exception) -> bool:
    return getattr(e, "code", None) == 400


async def _gemini_call(key: keys.Key, model: str, parts: list, caption: str | None) -> AnalysisResult:
    from google.genai import types

    contents = [types.Content(role="user", parts=[*parts, types.Part(text=_prompt(caption))])]
    last: Exception | None = None
    options = _thinking_options()
    start = _thinking_ok.get(model, 0)
    for i, thinking in enumerate(options[start:], start):
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
                _gemini(key).aio.models.generate_content(model=model, contents=contents, config=cfg),
                timeout=config.GEMINI_TIMEOUT,
            )
        except Exception as e:
            # 400 here usually means this model rejects this thinking setting.
            if _is_bad_request(e) and thinking is not None:
                last = e
                continue
            raise
        _thinking_ok[model] = i
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
    """Best model first; within a model, rotate keys past rate limits. Other errors
    (overload, timeout, bad output) move on to the next model."""
    pool = keys.gemini
    if not pool:
        raise AnalysisError("no Gemini API key configured")
    total = sum(m.path.stat().st_size for m in media)
    inline = await _inline_parts(media) if total <= config.INLINE_LIMIT_MB * 1024 * 1024 else None
    uploaded: list = []
    errors = []
    try:
        for model in config.GEMINI_MODELS:
            candidates = pool.candidates(model)
            if not candidates:
                errors.append(f"{model}: every key is rate-limited")
                continue
            for key in candidates:
                try:
                    parts = inline if inline is not None else await _uploaded_parts(key, media, uploaded)
                    async with pool.slot():
                        pool.used(key)
                        result = await _gemini_call(key, model, parts, caption)
                except Exception as e:
                    await keys.record("gemini", key, model, ok=False)
                    errors.append(f"{model}/{key.id}: {_short(e)}")
                    log.warning("gemini failed: %s", errors[-1])
                    rest = keys.rest_seconds(e)
                    if rest:
                        pool.rest(key, model, rest)
                        continue  # same model, next key
                    break  # not a quota problem: next model
                log.info("analysis ok: model=%s key=%s usage=%s", model, key.id, result.usage)
                await keys.record(
                    "gemini", key, model, ok=True,
                    input_tokens=result.usage.get("input") or 0, output_tokens=result.usage.get("output") or 0,
                )
                return result
        raise AnalysisError("; ".join(errors))
    finally:
        for key, f in uploaded:
            try:
                await _gemini(key).aio.files.delete(name=f.name)
            except Exception:
                pass


# ---------------------------------------------------------------- Groq backup

_groq_clients: dict[str, object] = {}  # key id -> AsyncGroq


def groq_client(key: keys.Key):
    if key.id not in _groq_clients:
        from groq import AsyncGroq

        _groq_clients[key.id] = AsyncGroq(api_key=key.value)
    return _groq_clients[key.id]


async def groq_call(model: str, call):
    """Run `call(client)` with the least-used Groq key that isn't resting for this
    model, rotating past rate limits. Other errors are raised."""
    pool = keys.groq
    if not pool:
        raise AnalysisError("no Groq API key configured")
    last = None
    for key in pool.candidates(model):
        try:
            async with pool.slot():
                pool.used(key)
                result = await call(groq_client(key))
        except Exception as e:
            await keys.record("groq", key, model, ok=False)
            rest = keys.rest_seconds(e)
            if not rest:
                raise
            pool.rest(key, model, rest)
            last = e
            continue
        usage = getattr(result, "usage", None)
        await keys.record(
            "groq", key, model, ok=True,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0, output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
        return result
    raise AnalysisError(f"every Groq key is rate-limited for {model}: {_short(last) if last else 'no keys'}")


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
    data = await asyncio.to_thread(audio.path.read_bytes)
    resp = await groq_call(config.GROQ_WHISPER_MODEL, lambda c: c.audio.transcriptions.create(
        model=config.GROQ_WHISPER_MODEL,
        file=(audio.path.name, data),
        response_format="text",
    ))
    return (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()


async def groq_json(system: str, user: str, max_tokens: int = 1500) -> dict:
    """Groq chat call that must return a JSON object; tries each chat model."""
    errors = []
    for model in config.GROQ_CHAT_MODELS:
        try:
            resp = await groq_call(model, lambda c, model=model: c.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                reasoning_effort="low",
                max_completion_tokens=max_tokens,
                temperature=0.2,
            ))
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            errors.append(f"{model}: {_short(e)}")
            log.warning("groq json failed: %s", errors[-1])
    raise AnalysisError("; ".join(errors))


async def _analyze_groq(media: list[MediaFile], caption: str | None) -> AnalysisResult:
    if not keys.groq:
        raise AnalysisError("no Groq API key configured")
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
        f"{_prompt(caption)}\n\n{SPEC['backup']['note']}\n"
        f"Schema: {schema}\n\nSpeech transcript:\n{transcript[:SPEC['backup']['transcript_max_chars']] or '(no speech)'}"
    )
    data = ReelAnalysis.model_validate(_clean(await groq_json("Return only a JSON object.", user)))
    if not data.transcript and transcript:
        data.transcript = transcript[:3000]
    mode = "audio-only" if transcript else "caption-only"
    return AnalysisResult(data=data, mode=mode, model="groq")


# ---------------------------------------------------------------- entry point


_analyze_slots: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _analyze_slots
    if _analyze_slots is None:
        _analyze_slots = asyncio.Semaphore(max(1, config.ANALYZE_CONCURRENCY))
    return _analyze_slots


async def analyze(media: list[MediaFile], caption: str | None) -> AnalysisResult:
    errors = []
    async with _slots():  # caps how many videos sit in memory at once
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
