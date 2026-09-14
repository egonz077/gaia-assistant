"""Per-user morning digest.

Runs every 15 minutes and sends to each user whose *local* time has just
crossed 08:00, which is not expressible as a single cron line once users have
their own timezones.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from gaia.core import usage as usage_mod
from gaia.core.config import settings
from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from gaia.core.db import messages as messages_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import tx
from gaia.core.models import User

log = logging.getLogger("gaia.digest")

SEND_HOUR = 8
WINDOW_HOURS = 24

SYSTEM = """Write a short, warm morning WhatsApp message for a real-estate developer listing \
what needs follow-up today. Group by person. Plain text, no markdown, no bullet characters.

Each item carries a nudge_count: how many mornings it has already appeared without being acted \
on. Vary the wording accordingly — 0 is new, 1-2 should note it is still open, and 3 or more \
should gently ask whether to snooze or close it. Never repeat yesterday's phrasing verbatim.

When an item carries a due time, say it — "call the attorney by 5pm today". The payload has \
always carried the timestamp and nothing used to ask for it, so whether the deadline survived \
into the message was left to the model's own judgement. A follow-up she is told about without \
its deadline is one she cannot prioritise.

End by offering to draft any of the follow-up texts."""


async def due_users(conn, now_utc: datetime | None = None) -> list[User]:
    now_utc = now_utc or datetime.now(timezone.utc)
    out = []
    for user in await users_db.list_users(conn):
        # One unusable row must not cost everyone else their digest. The
        # obvious way in is `users.timezone`, plain TEXT: `ZoneInfo()` on a
        # typo raises here, inside run_once, whose exception is only caught at
        # the top of main() — so a single bad row meant nobody in the company
        # got a digest, ever, with one `digest run failed` line every fifteen
        # minutes. The CLI validates the timezone now (core/admin.py); this
        # guard is the half that holds even for a row that got in some other
        # way, which is the half that matters at 8am.
        try:
            if not user.active:
                continue
            local = now_utc.astimezone(ZoneInfo(user.timezone))
            if local.hour < SEND_HOUR:
                continue
            cur = await conn.execute(
                "SELECT last_digest_on, created_at FROM users WHERE id = %s", (user.id,)
            )
            row = await cur.fetchone()
            if row["last_digest_on"] == local.date():
                continue  # already sent today; a restart must not double-send
            # A NULL `last_digest_on` means "no digest has ever been sent",
            # which for a row created earlier today is not a missed morning —
            # their 08:00 has not come round yet. Without this, adding a user
            # at 14:42 sent them a digest at the next 15-minute tick, built
            # from leads filed minutes earlier and marking them nudged, so the
            # first real digest called brand-new items "still open". Rows from
            # previous days keep the catch-up: a send that failed at 08:00, or
            # a jobs container down across that hour, must still go out later
            # (see the retry note in send_digest).
            created_local = row["created_at"].astimezone(ZoneInfo(user.timezone))
            if row["last_digest_on"] is None and created_local.date() == local.date():
                continue
            out.append(user)
        except Exception:
            log.exception("skipping user %s while selecting digest recipients", user.id)
    return out


async def _within_window(conn, user: User) -> bool:
    """The customer service window opens when the user messages the
    business, not before. A user who has never sent an inbound message has
    no open window at all — not an unknown one — so NULL `last_inbound_at`
    must be treated as *outside* the window, same as one older than 24h:
    both require the approved template rather than a free-form send."""
    cur = await conn.execute(
        f"""SELECT last_inbound_at IS NOT NULL
                   AND last_inbound_at > now() - interval '{WINDOW_HOURS} hours' AS ok
            FROM users WHERE id = %s""",
        (user.id,),
    )
    return bool((await cur.fetchone())["ok"])


async def compose_digest(
    client, user: User, leads: list[dict], commitments: list[dict], pool
) -> str:
    payload = {
        "leads": [
            {"contact": r["name"], "description": r["description"],
             "note": r["next_action_note"], "nudge_count": r["nudge_count"]}
            for r in leads
        ],
        "commitments": [
            {"description": r["description"], "contact": r["contact"],
             "due": r["due_at"].isoformat() if r["due_at"] else None,
             "nudge_count": r["nudge_count"]}
            for r in commitments
        ],
    }
    started = time.monotonic()
    response = await client.messages.create(
        model=settings.digest_model,
        max_tokens=2000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": str(payload)}],
    )
    # `pool` is required and passed by every caller — a seam, not a switch.
    # No turn_id: a digest is one call and not a turn, and counting it as a
    # one-call turn would skew the calls-per-turn distribution.
    #
    # Guarded here as well as inside record(), matching the agent loop: a
    # nobody-gets-their-8am-message failure is far worse than a missing
    # telemetry row.
    try:
        await usage_mod.record(
            pool, job="digest", user=user, model=settings.digest_model,
            usage=response.usage, stop_reason=response.stop_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        log.exception("could not record usage for a digest")
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def send_digest(conn, client, wa, user: User, pool) -> bool:
    """Returns whether anything was sent. A digest on an empty day trains
    people to ignore the thread."""
    leads = await leads_db.due_for(conn, user)
    commitments = await commitments_db.open_for(conn, user)
    if not leads and not commitments:
        return False

    text = await compose_digest(client, user, leads, commitments, pool)
    if not text:
        log.warning("empty digest for user %s", user.id)
        return False

    if await _within_window(conn, user):
        delivered = await wa.send_text(user.wa_id, text)
    else:
        # Free-form sends are rejected outside the 24-hour service window.
        delivered = await wa.send_template(user.wa_id, text)

    # Nothing is recorded unless the send actually landed. Recording it anyway
    # incremented nudge counts, wrote the digest into her thread as though she
    # had read it, and set last_digest_on so today would not be retried — for
    # a message she never received. Tomorrow's then says "still open from
    # yesterday" about something she was never told. Returning here instead
    # leaves the state untouched, so the next 15-minute tick tries again.
    if not delivered:
        log.error("digest send failed for user %s; leaving it unsent", user.id)
        return False

    await leads_db.mark_nudged(conn, user, [r["id"] for r in leads])
    await commitments_db.mark_nudged(conn, user, [r["id"] for r in commitments])
    await messages_db.log(conn, user, "assistant", text)
    await conn.execute(
        "UPDATE users SET last_digest_on = %s WHERE id = %s",
        (datetime.now(ZoneInfo(user.timezone)).date(), user.id),
    )
    return True


async def run_once(pool, client, wa) -> int:
    sent = 0
    async with tx(pool) as conn:
        users = await due_users(conn)
    for user in users:
        # Same rule as due_users: one user's failure — a Graph timeout, a bad
        # row, a model error — must not stop the rest of the company's
        # digests. Each already runs in its own transaction; this makes the
        # failure boundary match.
        try:
            async with tx(pool) as conn:
                if await send_digest(conn, client, wa, user, pool):
                    sent += 1
        except Exception:
            log.exception("digest failed for user %s", user.id)
    return sent


async def main() -> None:
    from anthropic import AsyncAnthropic

    from gaia.core.db.pool import get_pool
    from gaia.core.whatsapp import WhatsAppClient

    logging.basicConfig(level=logging.INFO)
    pool = get_pool()
    await pool.open(wait=True)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    wa = WhatsAppClient()

    while True:
        try:
            count = await run_once(pool, client, wa)
            if count:
                log.info("sent %d digests", count)
        except Exception:
            log.exception("digest run failed")
        await asyncio.sleep(900)  # 15 minutes


if __name__ == "__main__":
    asyncio.run(main())
