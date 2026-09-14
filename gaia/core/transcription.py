"""Speech to text, behind one interface.

Claude accepts no audio input at all, so a voice note is transcribed before the
model ever sees it. That makes this the one place in the system where a third
party hears a client — and the reason the vendor is isolated here the way
core/embeddings.py isolates Voyage: swapping it, including for a locally hosted
model later, must not reach the pipeline.
"""

import logging

import httpx

from gaia.core.config import settings

log = logging.getLogger("gaia.transcription")

ENDPOINT = "https://api.deepgram.com/v1/listen"

# Documented Nova-3 limits: 100 keyterms, 500 tokens across all of them.
# An unbounded roster returns an error instead of a transcript.
MAX_KEYTERMS = 100


class TranscriptionError(Exception):
    """The vendor failed, or heard nothing.

    Either way there is no transcript, and the caller must degrade visibly
    rather than file a meeting built from silence.
    """


async def transcribe(
    audio: bytes, mime_type: str, keyterms: list[str]
) -> tuple[str, float]:
    """Returns (transcript, audio_seconds). Raises TranscriptionError.

    Every parameter below is fixed rather than configurable.

    `mip_opt_out` in particular is a correctness property and not a setting:
    the model-improvement program is documented as opt-in and the API also
    exposes a per-request opt-out, and which default applies to a given plan is
    not something this code should depend on. A request without it is a bug.

    `keyterm` is the reason this vendor was chosen at all. General English
    transcription is solved; rare proper nouns are not, and this client book is
    almost entirely rare proper nouns. The caller passes the contact roster,
    which is ownership- and visibility-scoped, so only names that developer
    actually works with leave the box.
    """
    params = [
        ("model", "nova-3"),
        ("language", "en"),
        ("mip_opt_out", "true"),
        ("smart_format", "true"),
    ]
    params += [("keyterm", term) for term in keyterms[:MAX_KEYTERMS]]

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                ENDPOINT,
                params=params,
                content=audio,
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": mime_type,
                },
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        raise TranscriptionError(f"transcription request failed: {exc}") from exc

    try:
        alternative = body["results"]["channels"][0]["alternatives"][0]
        text = (alternative.get("transcript") or "").strip()
        seconds = float(body.get("metadata", {}).get("duration", 0.0))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # A 200 with an unexpected body must not become an empty transcript,
        # which would file a meeting built from nothing.
        raise TranscriptionError(f"unexpected transcription response: {exc}") from exc

    if not text:
        # Silence, or a pocket recording.
        raise TranscriptionError("empty transcript")

    return text, seconds
