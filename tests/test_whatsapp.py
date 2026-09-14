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


def test_an_audio_message_is_parsed_rather_than_marked_unsupported():
    """Before this, every voice note reached the model as the literal string
    '[unsupported message type: audio]'."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.1", "from": "13055550001", "type": "audio",
        "audio": {"id": "1908647269898587", "mime_type": "audio/ogg; codecs=opus",
                  "sha256": "abc=", "voice": True},
    }]}}]}]}

    assert whatsapp.parse_messages(payload) == [{
        "id": "wamid.1", "from": "13055550001", "type": "audio",
        "audio_id": "1908647269898587", "mime_type": "audio/ogg; codecs=opus",
        "voice": True,
    }]


def test_an_uploaded_audio_file_parses_the_same_way():
    """voice=False is a forwarded recording rather than the microphone button.
    The same content by a different route, so it takes the same path — the flag
    is recorded, not acted on."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.2", "from": "13055550001", "type": "audio",
        "audio": {"id": "999", "mime_type": "audio/mpeg", "voice": False},
    }]}}]}]}

    assert whatsapp.parse_messages(payload)[0]["voice"] is False


def test_an_unknown_message_type_still_degrades_to_a_note():
    """A sticker or a location is not a crash, it is a message the model can
    explain. That fallback must survive the audio branch."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.3", "from": "1", "type": "sticker", "sticker": {"id": "x"},
    }]}}]}]}

    assert "unsupported message type" in whatsapp.parse_messages(payload)[0]["text"]


@pytest.mark.asyncio
async def test_audio_is_returned_raw_and_never_reaches_pillow(monkeypatch):
    """download_media downscales, which is Pillow. Audio bytes down that path
    die inside Pillow rather than at a boundary — which is why fetching and
    image processing were separated."""
    def handler(request):
        if request.url.path.endswith("media-1"):
            return httpx.Response(200, json={
                "url": "https://lookaside.example/blob",
                "mime_type": "audio/ogg; codecs=opus",
            })
        return httpx.Response(200, content=b"OggS-raw-bytes")

    _stub_async_client(monkeypatch, handler)
    called = []
    monkeypatch.setattr(
        whatsapp, "downscale", lambda b: called.append(b) or ("image/jpeg", "x")
    )

    client = whatsapp.WhatsAppClient(token="t", phone_number_id="1")
    content, mime_type = await client.download_audio("media-1")

    assert content == b"OggS-raw-bytes", "audio must come back untouched"
    assert mime_type == "audio/ogg; codecs=opus"
    assert called == [], "downscale must not be called for audio"


@pytest.mark.asyncio
async def test_images_still_go_through_downscale(monkeypatch):
    """The other half: separating fetch from processing must not have changed
    the image path."""
    def handler(request):
        if request.url.path.endswith("media-2"):
            return httpx.Response(200, json={
                "url": "https://lookaside.example/blob", "mime_type": "image/jpeg",
            })
        return httpx.Response(200, content=b"jpeg-bytes")

    _stub_async_client(monkeypatch, handler)
    called = []
    monkeypatch.setattr(
        whatsapp, "downscale", lambda b: called.append(b) or ("image/jpeg", "ZmFrZQ==")
    )

    client = whatsapp.WhatsAppClient(token="t", phone_number_id="1")
    out = await client.download_media("media-2")

    assert called == [b"jpeg-bytes"]
    assert out == {"media_type": "image/jpeg", "data": "ZmFrZQ=="}
