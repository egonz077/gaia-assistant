from uuid import UUID

from gaia.core.embeddings import embed
from gaia.core.db.scope import visible
from gaia.core.models import User


async def index_meeting(
    conn,
    user: User,
    meeting_id: UUID,
    content: str,
    visibility: str,
    contact_id: UUID | None = None,
) -> None:
    """Visibility is passed in from the parent meeting, never defaulted.

    A private meeting whose embedding defaults to org-visible is hidden from
    the meetings list while remaining fully searchable by the whole company —
    worse than no privacy, because it looks private.
    """
    vector = (await embed([content], input_type="document"))[0]
    await conn.execute(
        """INSERT INTO memory_chunks
               (user_id, visibility, meeting_id, contact_id, content, embedding)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user.id, visibility, meeting_id, contact_id, content, vector),
    )


async def search(
    conn, user: User, query: str, contact_name: str | None = None, limit: int = 6
) -> list[dict]:
    vector = (await embed([query], input_type="query"))[0]

    name_clause = ""
    params: dict = {"emb": vector, "scope_user_id": user.id, "limit": limit}
    if contact_name:
        name_clause = "AND lower(ct.name) = lower(%(name)s)"
        params["name"] = contact_name

    cur = await conn.execute(
        f"""SELECT t.meeting_id, t.content, t.created_at, ct.name AS contact
            FROM memory_chunks t
            LEFT JOIN contacts ct ON ct.id = t.contact_id
            WHERE {visible('t')} {name_clause}
            ORDER BY t.embedding <=> %(emb)s::vector
            LIMIT %(limit)s""",
        params,
    )
    return [
        {
            # meeting_id is nullable in the schema (ON DELETE CASCADE clears
            # it) - a chunk is not guaranteed to trace back to a meeting.
            "meeting_id": str(r["meeting_id"]) if r["meeting_id"] else None,
            "content": r["content"],
            "date": r["created_at"].date().isoformat(),
            "contact": r["contact"],
        }
        for r in await cur.fetchall()
    ]
