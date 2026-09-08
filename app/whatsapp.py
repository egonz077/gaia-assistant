"""WhatsApp Cloud API helpers."""

import os
import base64
import httpx

TOKEN = os.environ["WA_ACCESS_TOKEN"]           # permanent token from Meta Business
PHONE_NUMBER_ID = os.environ["WA_PHONE_NUMBER_ID"]
GRAPH = "https://graph.facebook.com/v21.0"

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def extract_messages(payload: dict) -> list[dict]:
    """Flatten the deeply nested webhook payload into simple message dicts."""
    out = []
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


async def download_media(media_id: str) -> dict:
    """Two-step: get the media URL, then fetch bytes."""
    async with httpx.AsyncClient() as client:
        meta = (await client.get(f"{GRAPH}/{media_id}", headers=HEADERS)).json()
        blob = await client.get(meta["url"], headers=HEADERS)
        return {
            "mime": meta.get("mime_type", "image/jpeg"),
            "b64": base64.b64encode(blob.content).decode(),
        }


async def send_text(to: str, body: str):
    # WhatsApp caps messages at 4096 chars; split if needed
    chunks = [body[i:i + 4000] for i in range(0, len(body), 4000)] or [""]
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            await client.post(
                f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
                headers=HEADERS,
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": chunk},
                },
            )
