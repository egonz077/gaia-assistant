from uuid import UUID

from gaia.core.models import User

_SELECT = """SELECT c.id, c.description, c.due_at, c.nudge_count, ct.name AS contact
             FROM commitments c
             LEFT JOIN contacts ct ON ct.id = c.contact_id"""


async def open_for(conn, user: User, within_days: int = 2) -> list[dict]:
    """Commitments this user owes *soon* — the digest's question. Scoped by
    OWNERSHIP, not visibility — a colleague's org-visible task is readable
    but is not this user's work.

    The `within_days` horizon is what makes this the digest's read and not
    the user's: an 8am message listing something due in six weeks is noise.
    `query` below is the same table answering the other question."""
    cur = await conn.execute(
        f"""{_SELECT}
           WHERE c.user_id = %(uid)s
             AND c.done_at IS NULL
             AND (c.due_at IS NULL OR c.due_at <= now() + make_interval(days => %(days)s))
           ORDER BY c.due_at NULLS LAST""",
        {"uid": user.id, "days": within_days},
    )
    return await cur.fetchall()


async def query(conn, user: User, limit: int = 25) -> list[dict]:
    """Everything this user still owes — what they mean by "my commitments".

    Ownership-scoped like `open_for`, and for a second reason on top of the
    first: `complete` refuses anything the user does not own, so a
    visibility-scoped list here would show rows that then decline to close.

    Carries `id`, which is the point of it. Nothing else hands the model a
    commitment id, so without this read the only commitment tool it has
    demands a uuid the user has never seen and cannot be expected to know —
    which is exactly what it used to ask them for."""
    cur = await conn.execute(
        f"""{_SELECT}
           WHERE c.user_id = %(uid)s AND c.done_at IS NULL
           ORDER BY c.due_at NULLS LAST LIMIT %(limit)s""",
        {"uid": user.id, "limit": limit},
    )
    return await cur.fetchall()


async def complete(conn, user: User, ids: list[UUID]) -> list[UUID]:
    """Close one or many, and report which ones actually closed.

    Takes a list because "clear all my commitments" is one request, not five,
    and because the reply the user reads is built from the return value: a
    stale id or a colleague's row has to degrade to "closed two of three"
    rather than to a silent success. `done_at IS NULL` keeps an
    already-finished item out of that count — it is not newly closed."""
    if not ids:
        return []
    cur = await conn.execute(
        """UPDATE commitments SET done_at = now()
           WHERE id = ANY(%s) AND user_id = %s AND done_at IS NULL RETURNING id""",
        (list(ids), user.id),
    )
    return [row["id"] for row in await cur.fetchall()]


async def mark_nudged(conn, user: User, ids: list[UUID]) -> None:
    if not ids:
        return
    await conn.execute(
        """UPDATE commitments
           SET nudge_count = nudge_count + 1, last_nudged_at = now()
           WHERE id = ANY(%s) AND user_id = %s""",
        (ids, user.id),
    )
