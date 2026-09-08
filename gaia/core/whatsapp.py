import hashlib
import hmac
import logging

import httpx

from gaia.core.config import settings
from gaia.core.images import downscale

log = logging.getLogger("gaia.whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
MAX_BODY = 4000  # WhatsApp caps at 4096
DIGEST_TEMPLATE = "daily_digest"


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
                else:
                    base["type"] = "text"
                    base["text"] = f"[unsupported message type: {m['type']}]"
                out.append(base)
    return out


class WhatsAppClient:
    def __init__(self, token: str | None = None, phone_number_id: str | None = None):
        self._headers = {"Authorization": f"Bearer {token or settings.wa_access_token}"}
        self._phone_number_id = phone_number_id or settings.wa_phone_number_id

    async def _post(self, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{GRAPH}/{self._phone_number_id}/messages",
                headers=self._headers,
                json=payload,
            )
        if response.status_code >= 400:
            # The prototype ignored this, so 24h-window rejections were silent.
            log.error("whatsapp send failed %s: %s", response.status_code, response.text)

    async def send_text(self, to: str, body: str) -> None:
        for i in range(0, len(body) or 1, MAX_BODY):
            await self._post({
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[i:i + MAX_BODY] or " "},
            })

    async def send_template(self, to: str, body: str) -> None:
        """Used outside the 24-hour customer service window, where free-form
        messages are rejected."""
        await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": DIGEST_TEMPLATE,
                "language": {"code": "en"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": body[:1000]}]}
                ],
            },
        })

    async def download_media(self, media_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = (await client.get(f"{GRAPH}/{media_id}", headers=self._headers)).json()
            if "url" not in meta:
                raise RuntimeError(f"no media url for {media_id}: {meta}")
            blob = await client.get(meta["url"], headers=self._headers)
            blob.raise_for_status()

        media_type, data = downscale(blob.content)
        return {"media_type": media_type, "data": data}
