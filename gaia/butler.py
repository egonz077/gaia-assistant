import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from gaia.capabilities.base import registry
from gaia.core.db import contacts as contacts_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import get_pool, tx
from gaia.core.llm import run_agent
from gaia.core.models import User

log = logging.getLogger("gaia.butler")

APOLOGY_TEXT = "Sorry, something went wrong on my end and I couldn't finish that. Could you send it again?"

# Gaia has many agents, of any gender. The prototype this grew from served one
# person and said "she" throughout; a product that is handed the user's real
# name and then tells the model, eight times, what pronouns to use will
# misgender people in the first sentence of its reply. Use {name}, or "they".
BASE_PROMPT = """You are the assistant for {name}, an agent at the Gaia real-estate company, \
reachable over WhatsApp.

When {name} sends meeting notes — typed or photographed handwriting — transcribe if needed, then \
extract a short summary, the people involved, commitments made, and any follow-up dates. Save \
them with your tools. Echo back what you understood and ask {name} to confirm anything ambiguous: \
names, numbers, dates. Give the weekday whenever you name a date — "Friday, Sept 11" — so a wrong \
day is obvious at a glance rather than acted on.

Answer questions about past meetings, leads and contacts using your tools. Never contact third \
parties.

Style: brief and warm, like a text message. No markdown headers or bullet lists.
"""

# Everything that changes turn to turn lives here, after the cache breakpoint.
# The weekday belongs on this side of it too: it changes daily, by definition.
CONTEXT_PROMPT = """Today is {today} in {name}'s timezone ({tz}).
People {name} has worked with recently: {roster}
Use lookup_contact for details on any of them.
"""

# Not strftime("%A"): that is locale-dependent, and a scheduling assistant
# whose weekday changes with the container's LANG is worse than one with no
# weekday at all.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def today_line(now: datetime) -> str:
    """The weekday and the ISO date, from one instant so they cannot disagree.

    Injecting the bare ISO date left the model to derive the day of the week
    itself, and it is unreliable at it: in one live run it called 2026-09-11
    "Friday" in one turn and "Thu" in the next. For a scheduling assistant
    that is material — she says "send comps by Friday", the bot files it and
    tells her Thursday, and she works the wrong day. Two halves of one
    conversation contradicting each other also costs trust in everything else
    it says.
    """
    return f"{WEEKDAYS[now.weekday()]} {now.date().isoformat()}"


async def build_system_prompt(conn, user: User) -> list[str]:
    """The system prompt in two parts: stable first, volatile second.

    The caller marks only the first as the cache breakpoint (see
    core/llm.py). BASE_PROMPT and the capability fragments are stable per
    user; today's date and the roster are not — the roster reorders on
    essentially every save_meeting, and with it inside the cached prefix
    every reorder was a full miss on the system prompt *and* the tool
    definitions, rewritten at 1.25x. Spec §5.3 asked for exactly this split.
    """
    names = await contacts_db.roster(conn, user)
    today = today_line(datetime.now(ZoneInfo(user.timezone)))
    stable = BASE_PROMPT.format(name=user.name) + registry.prompt_fragments(user)
    volatile = CONTEXT_PROMPT.format(
        name=user.name,
        today=today,
        tz=user.timezone,
        roster=", ".join(names) or "(nobody yet)",
    )
    return [stable, volatile]


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
        if not await wa.send_text(user.wa_id, APOLOGY_TEXT):
            log.error("whatsapp rejected the apology to user %s", user.id)
            return
    except Exception:
        log.exception("failed to send the apology to user %s", user.id)
        return

    try:
        async with tx() as conn:
            await messages_db.log(conn, user, "assistant", APOLOGY_TEXT)
    except Exception:
        log.exception("failed to log the apology for user %s", user.id)


PHOTO_CAPTION_FALLBACK = "Here are my meeting notes."
PHOTO_FAILED_NOTE = "[a photo in this message could not be downloaded]"


def transcript_text(message: dict) -> str:
    """How one inbound message reads in her history. A photo's caption often
    is the content — "offer at 580, wants to close by Nov" — so it is what
    gets written rather than a bare marker."""
    if message["type"] == "image":
        return f"[photo] {message.get('caption') or PHOTO_CAPTION_FALLBACK}"
    return message["text"]


async def receive(conn, user: User, message: dict) -> bool:
    """Log one inbound message, and report whether it is new.

    The dedup check and the row that backs it are one transaction, and both
    happen at the webhook, before the message is queued. Spec §5 step 4:
    "Log the inbound message and take the user's turn lock… Logging before
    reading is crash-safe for dedup."

    Checking `seen` at the webhook while writing the row inside handle_turn
    left a window that was not the 3-second debounce but the whole of any
    turn already in flight — 10 to 30 seconds on a photo, squarely inside
    Meta's retry window. A redelivery in that window passed the check, was
    queued as a fresh burst and ran as a second turn: the model saw the same
    message twice and she got two replies to one message. `ON CONFLICT
    (wa_msg_id) DO NOTHING` protected the table, never the behaviour.
    """
    if await messages_db.seen(conn, message["id"]):
        return False
    await users_db.touch_inbound(conn, user)
    await messages_db.log(conn, user, "user", transcript_text(message), message["id"])
    return True


async def _build_blocks(conn, user: User, batch: list[dict], wa) -> tuple[list[dict], list[str]]:
    """Turn a debounced burst into content blocks for the model, turning a
    single unreadable image into a note in the transcript instead of losing
    the whole batch.

    `download_media` failing for one photo must not roll back — and thereby
    silently drop — every other message in the same burst. Each failure is
    recorded as its own visible fact, both amended into the row `receive`
    already wrote and as a text block the model sees, so its reply can tell
    her which part did not land.
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
                # The caption is the one thing of hers we still have on this
                # path. Surface it alongside the failure note rather than
                # discarding it along with the photo she can't easily resend
                # from memory.
                caption = message.get("caption")
                if caption:
                    blocks.append({"type": "text", "text": caption})
                blocks.append({"type": "text", "text": PHOTO_FAILED_NOTE})
                logged = f"{caption}\n{PHOTO_FAILED_NOTE}" if caption else PHOTO_FAILED_NOTE
                await messages_db.set_content(conn, user, message["id"], logged)
                continue
            blocks.append({
                "type": "image",
                "source": {"type": "base64", **media},
            })
            blocks.append({
                "type": "text",
                "text": message.get("caption") or PHOTO_CAPTION_FALLBACK,
            })
        else:
            blocks.append({"type": "text", "text": message["text"]})
    return blocks, wa_ids


async def handle_turn(user: User, batch: list[dict], wa) -> None:
    """One agent turn for one user, over a whole debounced burst.

    Opens its own short transactions rather than taking a connection from the
    caller: the turn queue serialises per user but does not own a database
    connection. No transaction is held across network I/O — not the Graph API
    sends, and not the agent loop, which is 10-30 seconds of Anthropic calls
    on a photo. The loop is handed the pool instead, and each tool call opens
    and commits its own transaction (see Registry.dispatch).

    The inbound log is already committed by the time this runs — `receive`
    writes it at the webhook, in the same transaction as the dedup check —
    which is deliberately not one transaction with the reply. The agent loop
    calls the Anthropic API and arbitrary capability tools — the riskiest,
    slowest part of the turn — and the webhook has already returned 200
    by the time this runs, so nothing will ever retry it. If that part fails
    while the inbound messages were still sitting uncommitted in the same
    transaction as the reply, the rollback would erase the only record that
    she ever wrote in — silence, with no way for her to know it did not save.
    Committing the inbound log first means a failure downstream degrades to
    "no reply yet, and I got an apology", not "it vanished and I have no
    idea". The same argument applies within the loop: work a tool has already
    done is committed, so a failure two tool calls later cannot erase it.

    Any unhandled failure here — a bad download, the model call, a database
    error while running the loop — is caught and turned into a short WhatsApp
    apology asking her to resend, rather than the turn queue's own bare
    log-and-swallow leaving her with silence.
    """
    try:
        async with tx() as conn:
            blocks, wa_ids = await _build_blocks(conn, user, batch, wa)
    except Exception:
        log.exception("failed preparing inbound messages for user %s", user.id)
        await _apologize(wa, user)
        return

    try:
        from anthropic import AsyncAnthropic

        # Zero-arg: the SDK resolves credentials from the process environment
        # (compose env_file: in production). Never pass api_key= — that would
        # turn workload identity federation into a code change later instead
        # of a config-only one.
        client = AsyncAnthropic()
        pool = get_pool()

        # Everything the model needs is read up front and the connection is
        # given back before the first API call.
        async with tx(pool) as conn:
            history = await messages_db.recent(conn, user, exclude_wa_ids=tuple(wa_ids))
            messages = [{"role": m["role"], "content": m["content"]} for m in history]
            messages.append({"role": "user", "content": blocks})
            system = await build_system_prompt(conn, user)

        tool_defs = registry.tool_defs(user)
        reply = await run_agent(client, pool, user, messages, system, tool_defs)

        delivered = await wa.send_text(user.wa_id, reply)
    except Exception:
        log.exception("turn failed after logging inbound messages for user %s", user.id)
        await _apologize(wa, user)
        return

    if not delivered:
        # Rejected, not raised — a closed service window or a dead token. The
        # reply is not history if it never arrived.
        log.error("whatsapp rejected the reply to user %s", user.id)
        return

    # Logged only once the send has actually happened, the same way
    # _apologize does it: a reply sitting in her history that she never
    # received tells both her and the model something untrue. Recording it is
    # strictly secondary to delivering it, so a failure here degrades quietly
    # rather than turning a delivered reply into an apology.
    try:
        async with tx(pool) as conn:
            await messages_db.log(conn, user, "assistant", reply)
    except Exception:
        log.exception("failed to log the reply for user %s", user.id)
