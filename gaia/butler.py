import logging

from gaia.capabilities.base import registry
from gaia.core.db import contacts as contacts_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent
from gaia.core.models import User

log = logging.getLogger("gaia.butler")

BASE_PROMPT = """You are the assistant for {name}, an agent at the Gaia real-estate company, \
reachable over WhatsApp.

When she sends meeting notes — typed or photographed handwriting — transcribe if needed, then \
extract a short summary, the people involved, commitments made, and any follow-up dates. Save \
them with your tools. Echo back what you understood and ask her to confirm anything ambiguous: \
names, numbers, dates.

Answer questions about past meetings, leads and contacts using your tools. Never contact third \
parties.

Style: brief and warm, like a text message. No markdown headers or bullet lists.

Today is {today} in her timezone ({tz}).
People she has worked with recently: {roster}
Use lookup_contact for details on any of them.
"""


async def build_system_prompt(conn, user: User) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    names = await contacts_db.roster(conn, user)
    today = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
    return BASE_PROMPT.format(
        name=user.name,
        today=today,
        tz=user.timezone,
        roster=", ".join(names) or "(nobody yet)",
    ) + registry.prompt_fragments(user)


async def handle_turn(user: User, batch: list[dict], wa) -> None:
    """One agent turn for one user, over a whole debounced burst.

    Opens its own transactions — two of them — rather than taking a
    connection from the caller: the turn queue serialises per user but does
    not own a database connection, and this function's own send to WhatsApp
    happens outside any transaction so a slow Graph API call never holds a
    pooled connection.
    """
    async with tx() as conn:
        await users_db.touch_inbound(conn, user)

        blocks, wa_ids = [], []
        for message in batch:
            wa_ids.append(message["id"])
            if message["type"] == "image":
                media = await wa.download_media(message["image_id"])
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", **media},
                })
                caption = message.get("caption") or "Here are my meeting notes."
                blocks.append({"type": "text", "text": caption})
                await messages_db.log(conn, user, "user", f"[photo] {caption}", message["id"])
            else:
                blocks.append({"type": "text", "text": message["text"]})
                await messages_db.log(conn, user, "user", message["text"], message["id"])

        history = await messages_db.recent(conn, user, exclude_wa_ids=tuple(wa_ids))
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": blocks})

        system = await build_system_prompt(conn, user)
        tool_defs = registry.tool_defs(user)

    from anthropic import AsyncAnthropic

    # Zero-arg: the SDK resolves credentials from the process environment
    # (compose env_file: in production). Never pass api_key= — that would
    # turn workload identity federation into a code change later instead of
    # a config-only one.
    client = AsyncAnthropic()
    async with tx() as conn:
        reply = await run_agent(client, conn, user, messages, system, tool_defs)
        await messages_db.log(conn, user, "assistant", reply)

    await wa.send_text(user.wa_id, reply)
