from gaia.core.models import User


async def log(conn, user: User, role: str, content: str, wa_msg_id: str | None = None) -> None:
    await conn.execute(
        """INSERT INTO messages (user_id, role, content, wa_msg_id)
           VALUES (%s,%s,%s,%s) ON CONFLICT (wa_msg_id) DO NOTHING""",
        (user.id, role, content, wa_msg_id),
    )


async def set_content(conn, user: User, wa_msg_id: str, content: str) -> None:
    """Rewrite an already-logged inbound message.

    The inbound row is written at the webhook, before the burst is debounced
    and before a photo is fetched, so a download that fails afterwards has to
    amend that row. Inserting instead would hit `ON CONFLICT (wa_msg_id) DO
    NOTHING` and the failure note would simply vanish, leaving her history
    claiming the photo arrived fine.
    """
    await conn.execute(
        "UPDATE messages SET content = %s WHERE wa_msg_id = %s AND user_id = %s",
        (content, wa_msg_id, user.id),
    )


async def seen(conn, wa_msg_id: str) -> bool:
    """Dedup is roster-independent: WhatsApp redelivers regardless of who sent
    it, so this deliberately does not take a user as its second parameter —
    see the named exemption in test_db_signatures.py."""
    cur = await conn.execute("SELECT 1 FROM messages WHERE wa_msg_id = %s", (wa_msg_id,))
    return await cur.fetchone() is not None


async def recent(conn, user: User, limit: int = 20, exclude_wa_ids: tuple[str, ...] = ()) -> list[dict]:
    """Oldest first. A thread is always owner-only — no visibility fragment.

    The exclusion predicate must treat NULL wa_msg_id (every assistant row)
    as "not excluded" explicitly. `wa_msg_id = ANY(array)` evaluates to NULL
    when wa_msg_id is NULL, and `NOT NULL` is NULL rather than true, so a
    naive `NOT (wa_msg_id = ANY(...))` silently drops every assistant turn
    from history whenever exclude_wa_ids is non-empty — i.e. on every real
    turn. See test_recent_exclusion_does_not_drop_assistant_turns.
    """
    cur = await conn.execute(
        """SELECT role, content FROM (
               SELECT role, content, created_at FROM messages
               WHERE user_id = %(uid)s
                 AND (%(excluded)s::text[] IS NULL
                      OR wa_msg_id IS NULL
                      OR NOT (wa_msg_id = ANY(%(excluded)s)))
               ORDER BY created_at DESC LIMIT %(limit)s
           ) recent ORDER BY created_at ASC""",
        {"uid": user.id, "limit": limit, "excluded": list(exclude_wa_ids) or None},
    )
    return await cur.fetchall()
