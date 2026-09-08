import logging

from gaia.capabilities.base import registry
from gaia.core.db import contacts as contacts_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent
from gaia.core.models import User

log = logging.getLogger("gaia.butler")

APOLOGY_TEXT = "Sorry, something went wrong on my end and I couldn't finish that. Could you send it again?"

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


async def _apologize(wa, user: User) -> None:
    """Best-effort: a failed apology must never mask the original failure,
    or blow up the turn a second time.

    Logged as her assistant turn only once the send actually succeeds — a
    logged-but-undelivered apology would tell her history something she was
    never told. Without this row at all, when she resends after a failure
    the model sees two of her messages back to back with no sign anything
    went wrong, and can't say "sorry, that one didn't land, I've got it now."
    Logging opens its own short transaction, since the inbound messages
    were already committed separately by this point; if that transaction
    can't even be opened (no database reachable), this still degrades
    quietly rather than raising a second failure on top of the first.
    """
    try:
        await wa.send_text(user.wa_id, APOLOGY_TEXT)
    except Exception:
        log.exception("failed to send the apology to user %s", user.id)
        return

    try:
        async with tx() as conn:
            await messages_db.log(conn, user, "assistant", APOLOGY_TEXT)
    except Exception:
        log.exception("failed to log the apology for user %s", user.id)


async def _log_inbound(conn, user: User, batch: list[dict], wa) -> tuple[list[dict], list[str]]:
    """Log every message in the burst, turning a single unreadable image into
    a note in the transcript instead of losing the whole batch.

    `download_media` failing for one photo must not roll back — and thereby
    silently drop — every other message in the same burst. Each failure is
    logged as its own visible fact, both to the user's history and as a text
    block the model sees, so its reply can tell her which part did not land.
    """
    blocks, wa_ids = [], []
    for message in batch:
        wa_ids.append(message["id"])
        if message["type"] == "image":
            try:
                media = await wa.download_media(message["image_id"])
            except Exception:
                log.exception(
                    "failed to download image %s for user %s", message["image_id"], user.id
                )
                # The caption often carries the actual content — "offer at
                # 580, wants to close by Nov" — and is the one thing of hers
                # we still have on this path. Surface it alongside the
                # failure note rather than discarding it along with the
                # photo she can't easily resend from memory.
                note = "[a photo in this message could not be downloaded]"
                caption = message.get("caption")
                if caption:
                    blocks.append({"type": "text", "text": caption})
                blocks.append({"type": "text", "text": note})
                logged = f"{caption}\n{note}" if caption else note
                await messages_db.log(conn, user, "user", logged, message["id"])
                continue
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
    return blocks, wa_ids


async def handle_turn(user: User, batch: list[dict], wa) -> None:
    """One agent turn for one user, over a whole debounced burst.

    Opens its own transactions — two of them — rather than taking a
    connection from the caller: the turn queue serialises per user but does
    not own a database connection, and this function's own sends to WhatsApp
    happen outside any transaction so a slow Graph API call never holds a
    pooled connection.

    The inbound log is committed *before* the agent loop runs, deliberately
    splitting what was one transaction in the original plan. The agent loop
    calls the Anthropic API and arbitrary capability tools — the riskiest,
    slowest part of the turn — and the webhook has already returned 200
    by the time this runs, so nothing will ever retry it. If that part fails
    while the inbound messages were still sitting uncommitted in the same
    transaction as the reply, the rollback would erase the only record that
    she ever wrote in — silence, with no way for her to know it did not save.
    Committing the inbound log first means a failure downstream degrades to
    "no reply yet, and I got an apology", not "it vanished and I have no
    idea".

    Any unhandled failure here — a bad download, the model call, a database
    error while running the loop — is caught and turned into a short WhatsApp
    apology asking her to resend, rather than the turn queue's own bare
    log-and-swallow leaving her with silence.
    """
    try:
        async with tx() as conn:
            await users_db.touch_inbound(conn, user)
            blocks, wa_ids = await _log_inbound(conn, user, batch, wa)
    except Exception:
        log.exception("failed logging inbound messages for user %s", user.id)
        await _apologize(wa, user)
        return

    try:
        from anthropic import AsyncAnthropic

        # Zero-arg: the SDK resolves credentials from the process environment
        # (compose env_file: in production). Never pass api_key= — that would
        # turn workload identity federation into a code change later instead
        # of a config-only one.
        client = AsyncAnthropic()
        async with tx() as conn:
            history = await messages_db.recent(conn, user, exclude_wa_ids=tuple(wa_ids))
            messages = [{"role": m["role"], "content": m["content"]} for m in history]
            messages.append({"role": "user", "content": blocks})

            system = await build_system_prompt(conn, user)
            tool_defs = registry.tool_defs(user)

            reply = await run_agent(client, conn, user, messages, system, tool_defs)
            await messages_db.log(conn, user, "assistant", reply)

        await wa.send_text(user.wa_id, reply)
    except Exception:
        log.exception("turn failed after logging inbound messages for user %s", user.id)
        await _apologize(wa, user)
