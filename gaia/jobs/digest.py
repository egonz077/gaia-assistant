"""Per-user morning digest.

Runs every 15 minutes and sends to each user whose *local* time has just
crossed 08:00, which is not expressible as a single cron line once users have
their own timezones.
"""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

SYSTEM = """Write a short, warm morning WhatsApp message for a real-estate agent listing what \
needs follow-up today. Group by person. Plain text, no markdown, no bullet characters.

Each item carries a nudge_count: how many mornings it has already appeared without being acted \
on. Vary the wording accordingly — 0 is new, 1-2 should note it is still open, and 3 or more \
should gently ask whether to snooze or close it. Never repeat yesterday's phrasing verbatim.

End by offering to draft any of the follow-up texts."""


async def due_users(conn, now_utc: datetime | None = None) -> list[User]:
    now_utc = now_utc or datetime.now(timezone.utc)
    out = []
    for user in await users_db.list_users(conn):
        if not user.active:
            continue
        local = now_utc.astimezone(ZoneInfo(user.timezone))
        if local.hour < SEND_HOUR:
            continue
        cur = await conn.execute(
            "SELECT last_digest_on FROM users WHERE id = %s", (user.id,)
        )
        last = (await cur.fetchone())["last_digest_on"]
        if last == local.date():
            continue  # already sent today; a restart must not double-send
        out.append(user)
    return out


async def _within_window(conn, user: User) -> bool:
    """True unless the user has messaged us before and it was more than 24h
    ago. A user who has never messaged in (no row yet, e.g. freshly onboarded)
    has no known-stale session to reject the send over, so NULL counts as
    in-window; only a *known* last inbound message older than the window
    forces the template path."""
    cur = await conn.execute(
        f"""SELECT last_inbound_at IS NULL
                   OR last_inbound_at > now() - interval '{WINDOW_HOURS} hours' AS ok
            FROM users WHERE id = %s""",
        (user.id,),
    )
    return bool((await cur.fetchone())["ok"])


async def compose_digest(client, user: User, leads: list[dict], commitments: list[dict]) -> str:
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
    response = await client.messages.create(
        model=settings.model,
        max_tokens=2000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": str(payload)}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def send_digest(conn, client, wa, user: User) -> bool:
    """Returns whether anything was sent. A digest on an empty day trains
    people to ignore the thread."""
    leads = await leads_db.due_for(conn, user)
    commitments = await commitments_db.open_for(conn, user)
    if not leads and not commitments:
        return False

    text = await compose_digest(client, user, leads, commitments)
    if not text:
        log.warning("empty digest for user %s", user.id)
        return False

    if await _within_window(conn, user):
        await wa.send_text(user.wa_id, text)
    else:
        # Free-form sends are rejected outside the 24-hour service window.
        await wa.send_template(user.wa_id, text)

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
        async with tx(pool) as conn:
            if await send_digest(conn, client, wa, user):
                sent += 1
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
