from datetime import datetime
from uuid import UUID

from gaia.core.db import contacts as contacts_db
from gaia.core.models import User


async def save(
    conn,
    user: User,
    *,
    summary: str,
    source: str,
    # Deliberately write-only for now: nothing in gaia/ selects raw_input, and
    # there is no read path on this table at all. Kept anyway — it is the only
    # copy of what was actually filed. The photo itself is not stored, so a
    # transcription dropped here cannot be re-derived from anything, whereas a
    # reader can be added the day a meetings list or "show me my original
    # notes" exists. Protected by the row's `visibility` like every other
    # column, so keeping it widens nothing (tests/test_isolation.py).
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


async def set_visibility(conn, user: User, meeting_id: UUID, visibility: str) -> bool:
    """Reclassify. Returns True iff a row existed and was owned by user - the
    caller must be told when nothing happened, not left to assume success.
    The trg_cascade_meeting_visibility trigger propagates the change to
    memory_chunks and commitments."""
    cur = await conn.execute(
        "UPDATE meetings SET visibility = %s WHERE id = %s AND user_id = %s RETURNING id",
        (visibility, meeting_id, user.id),
    )
    return await cur.fetchone() is not None
