import hashlib
import hmac
import logging
import re

import httpx

from gaia.core.config import settings
from gaia.core.images import downscale

log = logging.getLogger("gaia.whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
MAX_BODY = 4000  # WhatsApp caps at 4096
DIGEST_TEMPLATE = "daily_digest"
# Meta's own limit on a template parameter is 1024; leave room for the
# continuation marker and stop well short of a hard truncation.
TEMPLATE_PARAM_MAX = 900


def verify_signature(body: bytes, header: str) -> bool:
    if not header:
        return False
    expected = "sha256=" + hmac.new(
        settings.wa_app_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, expected)


def parse_messages(payload: dict) -> list[dict]:
    out: list[dict] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                base = {"id": m["id"], "from": m["from"], "type": m["type"]}
                if m["type"] == "text":
                    base["text"] = m["text"]["body"]
                elif m["type"] == "image":
                    base["image_id"] = m["image"]["id"]
                    base["caption"] = m["image"].get("caption")
                elif m["type"] == "audio":
                    base["audio_id"] = m["audio"]["id"]
                    base["mime_type"] = m["audio"].get("mime_type", "audio/ogg")
                    # True when recorded with WhatsApp's microphone button,
                    # False for a forwarded audio file. Recorded because it is
                    # free to keep and says what people actually do; not acted
                    # on, because both are meeting notes.
                    base["voice"] = m["audio"].get("voice", False)
                else:
                    base["type"] = "text"
                    base["text"] = f"[unsupported message type: {m['type']}]"
                out.append(base)
    return out


def flatten_for_template(body: str, limit: int = TEMPLATE_PARAM_MAX) -> str:
    """Make a multi-line digest legal as a WhatsApp template parameter.

    The Cloud API rejects a template body parameter containing newlines, tabs
    or four-plus consecutive spaces (error 132000, "parameter format does not
    match"). The digest prompt asks the model to group the day's follow-ups by
    person, which produces exactly that. The template path is the one taken
    when an agent has not texted in 24 hours — including every newly-onboarded
    agent's very first digest — so without this the highest-stakes send is the
    one most likely to be rejected outright.

    Truncation is marked rather than silent: a digest that stops mid-sentence
    reads like a bug, and she has no way to ask for the rest of something she
    cannot tell was cut.
    """
    flat = " · ".join(line.strip() for line in body.splitlines() if line.strip())
    flat = re.sub(r"\s+", " ", flat).strip()
    if len(flat) > limit:
        flat = flat[:limit].rsplit(" ", 1)[0] + "… (reply here for the rest)"
    return flat


class WhatsAppClient:
    def __init__(self, token: str | None = None, phone_number_id: str | None = None):
        self._headers = {"Authorization": f"Bearer {token or settings.wa_access_token}"}
        self._phone_number_id = phone_number_id or settings.wa_phone_number_id

    async def _post(self, payload: dict) -> bool:
        """Returns whether Meta accepted the message.

        The prototype ignored the status entirely, so 24h-window rejections
        were silent. Logging it was only half the fix: returning None made
        every caller's success path unconditional, and a log line nobody reads
        is the same silence one layer up. The digest marked leads nudged,
        wrote itself into her thread as though she had read it, and set
        last_digest_on so it would not retry — for a message she never got.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{GRAPH}/{self._phone_number_id}/messages",
                headers=self._headers,
                json=payload,
            )
        if response.status_code >= 400:
            log.error("whatsapp send failed %s: %s", response.status_code, response.text)
            return False
        return True

    async def send_text(self, to: str, body: str) -> bool:
        for i in range(0, len(body) or 1, MAX_BODY):
            if not await self._post({
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[i:i + MAX_BODY] or " "},
            }):
                # A later chunk after a rejected one is pointless: the usual
                # cause (a closed service window, a dead token) applies to all
                # of them.
                return False
        return True

    async def mark_read(self, message_id: str) -> bool:
        """Blue ticks plus a typing bubble, in one call — the API offers no
        way to show typing without also marking the message read.

        Issued when the turn actually starts, not at the webhook: the receipt
        then means "we have picked this up", not "a server received bytes".
        The indicator clears on our reply or after 25 seconds, whichever
        comes first, so a slow photo turn can outlive it.
        """
        return await self._post({
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        })

    async def send_template(self, to: str, body: str) -> bool:
        """Used outside the 24-hour customer service window, where free-form
        messages are rejected."""
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": DIGEST_TEMPLATE,
                # Must match the language the template was approved under,
                # exactly — register it as `en`, not `en_US`.
                "language": {"code": "en"},
                "components": [
                    {"type": "body",
                     "parameters": [{"type": "text", "text": flatten_for_template(body)}]}
                ],
            },
        })

    async def _fetch_media(self, media_id: str) -> tuple[str, bytes]:
        """The two-step Graph fetch: metadata, then the bytes.

        Deliberately knows nothing about what kind of media it is holding.
        Fetching and image processing used to be one method, which meant audio
        went through Pillow and died inside it rather than at a boundary.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            meta = (await client.get(f"{GRAPH}/{media_id}", headers=self._headers)).json()
            if "url" not in meta:
                raise RuntimeError(f"no media url for {media_id}: {meta}")
            blob = await client.get(meta["url"], headers=self._headers)
            blob.raise_for_status()
        return meta.get("mime_type", ""), blob.content

    async def download_media(self, media_id: str) -> dict:
        """An image, downscaled to the model's resolution ceiling."""
        _, content = await self._fetch_media(media_id)
        media_type, data = downscale(content)
        return {"media_type": media_type, "data": data}

    async def download_audio(self, media_id: str) -> tuple[bytes, str]:
        """Raw bytes and MIME type, untouched.

        Deepgram accepts WhatsApp's OGG/Opus directly, so there is nothing to
        transcode and no ffmpeg in the image.
        """
        mime_type, content = await self._fetch_media(media_id)
        return content, mime_type or "audio/ogg"
