import hashlib
import hmac
import json

import httpx
import pytest

from app import config
from channels import instagram


def sign(body: bytes, secret: str = "app-s3cret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check():
    body = b'{"object":"instagram"}'
    assert instagram.verify_signature(body, sign(body))
    assert not instagram.verify_signature(body, sign(body, "other"))
    assert not instagram.verify_signature(body, None)
    assert not instagram.verify_signature(body + b" ", sign(body))


def webhook(*events):
    return {"object": "instagram", "entry": [{"id": "BOT", "time": 1, "messaging": list(events)}]}


def msg(sender="u1", mid="m1", ts=1, **message):
    return {"sender": {"id": sender}, "recipient": {"id": "BOT"}, "timestamp": ts, "message": {"mid": mid, **message}}


def test_parse_keeps_messages_and_drops_echoes_and_reads():
    body = webhook(
        msg(mid="m2", ts=2, text=" hello "),
        msg(mid="m1", ts=1, attachments=[{"type": "ig_reel", "payload": {"url": "https://cdn/v.mp4", "title": "cap"}}]),
        msg(sender="BOT", mid="m3", ts=3, text="our reply", is_echo=True),
        msg(mid="m4", ts=4, is_deleted=True),
        {"sender": {"id": "u1"}, "recipient": {"id": "BOT"}, "timestamp": 5, "read": {"mid": "m2"}},
        msg(mid="m6", ts=6, text="Ask a question", quick_reply={"payload": "ASK"}),
    )
    parsed = instagram.parse(body)
    assert [m.mid for m in parsed] == ["m1", "m2", "m6"]  # oldest first
    assert parsed[0].attachments[0].type == "ig_reel" and parsed[0].attachments[0].url == "https://cdn/v.mp4"
    assert parsed[0].attachments[0].payload["title"] == "cap"
    assert parsed[1].text == "hello" and parsed[2].quick_reply == "ASK"
    assert instagram.parse({"object": "page", "entry": []}) == []


def test_split_text_respects_byte_limit():
    text = "\n".join(["line ✨ " * 20] * 20)
    parts = instagram.split_text(text, 300)
    assert len(parts) > 1 and all(len(p.encode()) <= 300 for p in parts)
    assert instagram.split_text("x" * 700, 300) == ["x" * 300, "x" * 300, "x" * 100]
    assert instagram.split_text("short") == ["short"]


@pytest.fixture
def no_db_token(monkeypatch):
    async def none(key):
        return None

    monkeypatch.setattr(instagram.db, "kv_get", none)


def messenger(handler) -> instagram.Messenger:
    return instagram.Messenger(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_text_with_quick_replies(no_db_token):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"recipient_id": "u1", "message_id": "x"})

    out = messenger(handler)
    await out.send_text("u1", "a" * 1500, [("💬 Ask a question", "ASK")])
    assert len(seen) == 2 and seen[0][0].endswith("/me/messages") and seen[0][1] == "Bearer ig-token"
    assert "quick_replies" not in seen[0][2]["message"]
    assert seen[1][2]["message"]["quick_replies"] == [{"content_type": "text", "title": "💬 Ask a question", "payload": "ASK"}]


async def test_send_retries_rate_limits_then_gives_up(no_db_token, monkeypatch):
    monkeypatch.setattr(instagram.asyncio, "sleep", lambda s: _noop())
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"code": 4, "message": "rate"}})
        return httpx.Response(200, json={})

    await messenger(handler).send_text("u1", "hi")
    assert len(calls) == 2

    def closed(request):
        return httpx.Response(400, json={"error": {"code": 10, "error_subcode": 2534022, "message": "outside window"}})

    with pytest.raises(instagram.SendError) as e:
        await messenger(closed).send_text("u1", "hi")
    assert e.value.window_closed and not e.value.retryable


async def _noop():
    return None


async def test_token_refresh_is_stored(monkeypatch):
    stored = {}

    async def kv_get(key):
        return None

    async def kv_set(key, value):
        stored[key] = value

    monkeypatch.setattr(instagram.db, "kv_get", kv_get)
    monkeypatch.setattr(instagram.db, "kv_set", kv_set)

    def handler(request):
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["access_token"] == "ig-token"
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 5184000})

    out = messenger(handler)
    assert await out.refresh_token()
    assert stored == {instagram.TOKEN_KEY: "fresh"} and await out.token() == "fresh"
    assert config.GRAPH_API_URL.startswith("https://graph.instagram.com/v")
