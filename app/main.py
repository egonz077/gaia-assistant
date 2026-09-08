"""
WhatsApp -> Claude agent webhook.

Flow:
  1. WhatsApp Cloud API POSTs inbound messages here.
  2. We fetch media (if photo of notes), build context (recent thread,
     contact profiles, semantic recall), and run an agent loop with tools.
  3. Reply goes back through the WhatsApp send API.
"""

import os
import hmac
import hashlib
import logging

import anthropic
import httpx
from fastapi import FastAPI, Request, Response, HTTPException

import db
import tools as agent_tools
import whatsapp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent")

app = FastAPI()
claude = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env

VERIFY_TOKEN = os.environ["WA_VERIFY_TOKEN"]        # you invent this, set in Meta console
APP_SECRET = os.environ["WA_APP_SECRET"]            # Meta app secret, for signature check
OWNER_WA_ID = os.environ["OWNER_WA_ID"]             # your wife's number; ignore everyone else

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = """You are a personal assistant for a real-estate agent, reachable via WhatsApp.

Your jobs:
1. When she sends meeting notes (typed or photographed handwriting), transcribe if needed,
   then extract: a short summary, the people involved, commitments made, and any follow-up
   dates. Save these with your tools. Echo back what you understood and ask her to confirm
   anything ambiguous (names, numbers, dates).
2. Answer questions about past meetings, leads, and contacts using search_memory and the DB tools.
3. Help schedule: propose times, create commitments/reminders. Never contact third parties.

Style: brief, warm, WhatsApp-appropriate. No markdown headers. Today's date and known
contact profiles are provided in context.
"""


# ---------- webhook verification (GET) ----------
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403)


# ---------- inbound messages (POST) ----------
@app.post("/webhook")
async def inbound(request: Request):
    body = await request.body()

    # verify Meta signature
    sig = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403)

    payload = await request.json()
    for msg in whatsapp.extract_messages(payload):
        if msg["from"] != OWNER_WA_ID:
            log.info("ignoring message from unknown sender %s", msg["from"])
            continue
        if db.message_seen(msg["id"]):
            continue  # WhatsApp retries webhooks; dedup on message id
        try:
            await handle_message(msg)
        except Exception:
            log.exception("failed handling message")
            await whatsapp.send_text(OWNER_WA_ID, "Something went wrong on my end — try that again?")
    return {"status": "ok"}


async def handle_message(msg: dict):
    content_blocks = []

    if msg["type"] == "image":
        media = await whatsapp.download_media(msg["image_id"])
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media["mime"], "data": media["b64"]},
        })
        caption = msg.get("caption") or "Here are my meeting notes."
        content_blocks.append({"type": "text", "text": caption})
        db.log_message("user", f"[photo] {caption}", msg["id"])
    else:
        content_blocks.append({"type": "text", "text": msg["text"]})
        db.log_message("user", msg["text"], msg["id"])

    # ---- build context ----
    history = db.recent_messages(limit=20)          # rolling thread
    profiles = db.all_contact_profiles()            # small; inject whole thing for v1
    context_preamble = (
        f"<today>{db.today_str()}</today>\n"
        f"<contact_profiles>\n{profiles}\n</contact_profiles>"
    )

    messages = history + [{"role": "user", "content": content_blocks}]
    # prepend context to first message of this turn
    messages[-1]["content"] = [{"type": "text", "text": context_preamble}] + content_blocks

    # ---- agent loop ----
    reply_text = await run_agent(messages)

    db.log_message("assistant", reply_text, None)
    await whatsapp.send_text(OWNER_WA_ID, reply_text)


async def run_agent(messages: list) -> str:
    """Tool-use loop until Claude produces a final text reply."""
    while True:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=agent_tools.TOOL_DEFS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = agent_tools.dispatch(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": results})
