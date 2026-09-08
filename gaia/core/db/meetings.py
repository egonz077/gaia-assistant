from datetime import datetime
from uuid import UUID

from gaia.core.db import contacts as contacts_db
from gaia.core.db.scope import visible
from gaia.core.models import User


async def save(
    conn,
    user: User,
    *,
    summary: str,
    source: str,
    raw_input: str | None = None,
    happened_at: datetime | None = None,
    visibility: str = "org",
    contact_names: tuple[str, ...] = (),
    commitments: tuple[dict, ...] = (),
) -> UUID:
    """Create a meeting and everything derived from it, in one transaction.

    Derived rows copy the meeting's visibility and owner. They never take
    their own — a private note whose embedding defaults to org-visible is
    searchable by the whole company.
    """
    cur = await conn.execute(
        """INSERT INTO meetings (user_id, visibility, source, raw_input, summary, happened_at)
           VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (user.id, visibility, source, raw_input, summary, happened_at),
    )
    meeting_id = (await cur.fetchone())["id"]

    contact_ids: dict[str, UUID] = {}
    for name in contact_names:
        cid = await contacts_db.get_or_create(conn, user, name)
        contact_ids[name.lower()] = cid
        await conn.execute(
            """INSERT INTO meeting_contacts (meeting_id, contact_id) VALUES (%s, %s)
               ON CONFLICT DO NOTHING""",
            (meeting_id, cid),
        )

    for item in commitments:
        cid = contact_ids.get((item.get("contact_name") or "").lower())
        await conn.execute(
            """INSERT INTO commitments
                   (user_id, visibility, meeting_id, contact_id, description, due_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user.id, visibility, meeting_id, cid, item["description"], item.get("due_at")),
        )

    return meeting_id


async def set_visibility(conn, user: User, meeting_id: UUID, visibility: str) -> None:
    """Reclassify. The trg_cascade_meeting_visibility trigger propagates to
    memory_chunks and commitments."""
    await conn.execute(
        "UPDATE meetings SET visibility = %s WHERE id = %s AND user_id = %s",
        (visibility, meeting_id, user.id),
    )


async def recent(conn, user: User, limit: int = 10) -> list[dict]:
    cur = await conn.execute(
        f"""SELECT t.id, t.summary, t.happened_at FROM meetings t
            WHERE {visible('t')} ORDER BY t.happened_at DESC LIMIT %(limit)s""",
        {"scope_user_id": user.id, "limit": limit},
    )
    return await cur.fetchall()
