from uuid import UUID

from gaia.core.models import User


async def open_for(conn, user: User, within_days: int = 2) -> list[dict]:
    """Commitments this user owes. Scoped by OWNERSHIP, not visibility —
    a colleague's org-visible task is readable but is not this user's work."""
    cur = await conn.execute(
        """SELECT c.id, c.description, c.due_at, c.nudge_count, ct.name AS contact
           FROM commitments c
           LEFT JOIN contacts ct ON ct.id = c.contact_id
           WHERE c.user_id = %(uid)s
             AND c.done_at IS NULL
             AND (c.due_at IS NULL OR c.due_at <= now() + make_interval(days => %(days)s))
           ORDER BY c.due_at NULLS LAST""",
        {"uid": user.id, "days": within_days},
    )
    return await cur.fetchall()


async def complete(conn, user: User, commitment_id: UUID) -> bool:
    cur = await conn.execute(
        """UPDATE commitments SET done_at = now()
           WHERE id = %s AND user_id = %s AND done_at IS NULL RETURNING id""",
        (commitment_id, user.id),
    )
    return await cur.fetchone() is not None


async def mark_nudged(conn, user: User, ids: list[UUID]) -> None:
    if not ids:
        return
    await conn.execute(
        """UPDATE commitments
           SET nudge_count = nudge_count + 1, last_nudged_at = now()
           WHERE id = ANY(%s) AND user_id = %s""",
        (ids, user.id),
    )
