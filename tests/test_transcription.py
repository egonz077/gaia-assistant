"""The vendor call, against an in-process transport.

No hook in the module itself: a module-level `_transport` that only tests ever
set is production code existing for tests, which is the thing the required
`pool` parameter on usage.record was written to avoid. These monkeypatch
transcription.httpx.AsyncClient instead, copying the helper
tests/test_whatsapp.py already uses for the Graph API.
"""

import httpx
import pytest

from gaia.core import transcription

OK_BODY = {
    "metadata": {"duration": 12.5},
    "results": {"channels": [{"alternatives": [
        {"transcript": "Met Marta Delgado at the Coral Gables listing."}
    ]}]},
}


def _stub_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr(transcription.httpx, "AsyncClient", factory)


def _recording_handler(seen, body=None):
    def handler(request):
        seen["url"] = str(request.url)
        seen["content"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=body or OK_BODY)
    return handler


async def test_the_transcript_and_duration_come_back(monkeypatch):
    _stub_async_client(monkeypatch, _recording_handler({}))

    text, seconds = await transcription.transcribe(b"audio", "audio/ogg", [])

    assert text == "Met Marta Delgado at the Coral Gables listing."
    assert seconds == 12.5


async def test_every_request_opts_out_of_model_training(monkeypatch):
    """Not a setting and not a default to rely on. The program is documented as
    opt-in and the API also exposes a per-request opt-out; which one applies to
    a given plan is not something this code should depend on."""
    seen = {}
    _stub_async_client(monkeypatch, _recording_handler(seen))

    await transcription.transcribe(b"audio", "audio/ogg", [])

    assert "mip_opt_out=true" in seen["url"]
    assert "model=nova-3" in seen["url"]
    assert "language=en" in seen["url"]


async def test_the_audio_is_posted_with_its_own_content_type(monkeypatch):
    seen = {}
    _stub_async_client(monkeypatch, _recording_handler(seen))

    await transcription.transcribe(b"OggS-bytes", "audio/ogg; codecs=opus", [])

    assert seen["content"] == b"OggS-bytes"
    assert seen["headers"]["content-type"] == "audio/ogg; codecs=opus"


async def test_keyterms_are_sent_one_parameter_each(monkeypatch):
    """The whole reason this vendor was chosen. Rare proper nouns are what the
    feature gets wrong without them."""
    seen = {}
    _stub_async_client(monkeypatch, _recording_handler(seen))

    await transcription.transcribe(b"a", "audio/ogg", ["Cesia", "Marta Delgado"])

    assert "keyterm=Cesia" in seen["url"]
    assert ("keyterm=Marta+Delgado" in seen["url"]
            or "keyterm=Marta%20Delgado" in seen["url"])


async def test_keyterms_are_capped_at_the_documented_limit(monkeypatch):
    """100 terms, 500 tokens. An unbounded roster returns an error instead of a
    transcript."""
    seen = {}
    _stub_async_client(monkeypatch, _recording_handler(seen))

    await transcription.transcribe(b"a", "audio/ogg", [f"Name{i}" for i in range(250)])

    assert seen["url"].count("keyterm=") == transcription.MAX_KEYTERMS


async def test_a_vendor_error_raises(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"err_msg": "bad key"})

    _stub_async_client(monkeypatch, handler)

    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"a", "audio/ogg", [])


async def test_an_unexpected_response_shape_raises(monkeypatch):
    """A 200 with the wrong body must not become an empty transcript that files
    an empty meeting."""
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    _stub_async_client(monkeypatch, handler)

    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"a", "audio/ogg", [])


async def test_an_empty_transcript_raises_rather_than_filing_nothing(monkeypatch):
    """Silence, or a pocket recording. A meeting filed from an empty transcript
    is worse than one visibly not filed."""
    _stub_async_client(monkeypatch, _recording_handler({}, body={
        "metadata": {"duration": 3.0},
        "results": {"channels": [{"alternatives": [{"transcript": "   "}]}]},
    }))

    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"a", "audio/ogg", [])
