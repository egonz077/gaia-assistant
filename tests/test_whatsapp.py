import base64
import hashlib
import hmac
import io
import json

import httpx
import pytest
from PIL import Image

from gaia.core import whatsapp
from gaia.core.images import MAX_EDGE


def test_parse_extracts_a_text_message():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.1", "from": "13055550001", "type": "text", "text": {"body": "hello"}}
    ]}}]}]}
    assert whatsapp.parse_messages(payload) == [
        {"id": "wamid.1", "from": "13055550001", "type": "text", "text": "hello"}
    ]


def test_parse_extracts_an_image_with_a_caption():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.2", "from": "1305", "type": "image",
         "image": {"id": "media.9", "caption": "my notes"}}
    ]}}]}]}
    [msg] = whatsapp.parse_messages(payload)
    assert msg["image_id"] == "media.9"
    assert msg["caption"] == "my notes"


def test_parse_image_without_caption_yields_none():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.4", "from": "1305", "type": "image", "image": {"id": "media.10"}}
    ]}}]}]}
    [msg] = whatsapp.parse_messages(payload)
    assert msg["image_id"] == "media.10"
    assert msg["caption"] is None


def test_parse_ignores_status_callbacks():
    assert whatsapp.parse_messages({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []


def test_unsupported_types_degrade_to_text():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.3", "from": "1305", "type": "sticker"}
    ]}}]}]}
    [msg] = whatsapp.parse_messages(payload)
    assert msg["type"] == "text"
    assert "sticker" in msg["text"]


def test_signature_verification(monkeypatch):
    monkeypatch.setattr(whatsapp.settings, "wa_app_secret", "shh")
    body = b'{"hello":"world"}'
    good = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(body, good) is True
    assert whatsapp.verify_signature(body, "sha256=deadbeef") is False
    assert whatsapp.verify_signature(body, "") is False


def _stub_async_client(monkeypatch, handler):
    """Point whatsapp.httpx.AsyncClient at an in-process MockTransport so no
    real network call is made."""
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_send_text_posts_to_graph_and_logs_nothing_on_success(monkeypatch, caplog):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    with caplog.at_level("ERROR"):
        await client.send_text("13055550001", "hello there")

    assert len(calls) == 1
    request = calls[0]
    assert request.url == f"{whatsapp.GRAPH}/123/messages"
    assert request.headers["authorization"] == "Bearer tok"
    assert not caplog.records


@pytest.mark.asyncio
async def test_post_logs_error_when_send_fails(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "outside 24h window"}})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    with caplog.at_level("ERROR"):
        await client.send_text("13055550001", "hello")

    assert any("403" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_download_media_returns_downscaled_base64(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (5000, 3000), "white").save(buf, format="JPEG")
    raw = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://cdn.example/media.9"})
        return httpx.Response(200, content=raw, headers={"content-type": "image/jpeg"})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    result = await client.download_media("media.9")

    assert set(result.keys()) == {"media_type", "data"}
    assert result["media_type"] == "image/jpeg"
    out = Image.open(io.BytesIO(base64.b64decode(result["data"])))
    assert max(out.size) == MAX_EDGE


@pytest.mark.asyncio
async def test_send_template_sends_correct_payload_shape(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    await client.send_template("13055550001", "Good morning! You have 3 new leads.")

    assert len(calls) == 1
    body = json.loads(calls[0].read())

    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "13055550001"
    assert body["type"] == "template"
    assert body["template"]["name"] == whatsapp.DIGEST_TEMPLATE
    assert "code" in body["template"]["language"]

    components = body["template"]["components"]
    assert len(components) == 1
    [component] = components
    assert component["type"] == "body"
    assert len(component["parameters"]) == 1
    [parameter] = component["parameters"]
    assert parameter["type"] == "text"
    assert parameter["text"] == "Good morning! You have 3 new leads."


def test_flatten_makes_a_grouped_digest_legal_as_a_template_parameter():
    """Meta rejects a template body parameter containing newlines, tabs or
    4+ consecutive spaces (error 132000). The digest prompt asks the model to
    group follow-ups by person, which produces all three."""
    digest = (
        "Morning!\n\n"
        "Maria Delgado:\n"
        "\tsend comps    (still open from Tuesday)\n"
        "Rivera:\n"
        "  confirm the Thursday walkthrough\n"
    )

    flat = whatsapp.flatten_for_template(digest)

    assert "\n" not in flat
    assert "\t" not in flat
    assert "    " not in flat
    assert "Maria Delgado" in flat and "Rivera" in flat


def test_flatten_marks_a_truncation_instead_of_cutting_mid_sentence():
    flat = whatsapp.flatten_for_template("word " * 500)
    assert len(flat) <= whatsapp.TEMPLATE_PARAM_MAX + len("… (reply here for the rest)")
    assert flat.endswith("… (reply here for the rest)")


@pytest.mark.asyncio
async def test_send_template_flattens_the_body_it_sends(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    assert await client.send_template("13055550001", "Morning!\nMaria: send comps") is True

    body = json.loads(calls[0].read())
    [parameter] = body["template"]["components"][0]["parameters"]
    assert "\n" not in parameter["text"]
    assert "Maria" in parameter["text"]


@pytest.mark.asyncio
async def test_sends_report_whether_meta_accepted_them(monkeypatch):
    """Returning None made every caller's success path unconditional — the
    digest recorded an undelivered message as sent."""
    def rejects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "outside 24h window"}})

    _stub_async_client(monkeypatch, rejects)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    assert await client.send_text("13055550001", "hello") is False
    assert await client.send_template("13055550001", "hello") is False


@pytest.mark.asyncio
async def test_mark_read_sends_a_read_status_carrying_a_typing_indicator(monkeypatch):
    """One call does both: WhatsApp has no way to show typing without also
    marking the message read."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"success": True})

    _stub_async_client(monkeypatch, handler)
    client = whatsapp.WhatsAppClient(token="tok", phone_number_id="123")

    assert await client.mark_read("wamid.in") is True

    [request] = calls
    assert request.url == f"{whatsapp.GRAPH}/123/messages"
    body = json.loads(request.read())
    assert body["messaging_product"] == "whatsapp"
    assert body["status"] == "read"
    assert body["message_id"] == "wamid.in"
    assert body["typing_indicator"] == {"type": "text"}
