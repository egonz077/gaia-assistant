from uuid import UUID

from gaia.core.db import contacts as contacts_db
from gaia.core.db.scope import visible
from gaia.core.models import User

_SELECT = """SELECT l.id, ct.name, l.description, l.status, l.next_action_at,
                    l.next_action_note, l.nudge_count
             FROM leads l JOIN contacts ct ON ct.id = l.contact_id"""

_UPDATABLE = ("status", "next_action_at", "next_action_note", "description")


async def create(
    conn,
    user: User,
    *,
    contact_name: str,
    description: str,
    status: str = "new",
    next_action_at=None,
    next_action_note: str | None = None,
    visibility: str = "org",
) -> UUID:
    """The prototype could read and update leads but never insert one, so the
    pipeline was permanently empty and the digest could only ever see
    commitments. This is that missing half."""
    contact_id = await contacts_db.get_or_create(conn, user, contact_name)
    cur = await conn.execute(
        """INSERT INTO leads (user_id, visibility, contact_id, description, status,
                              next_action_at, next_action_note)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (user.id, visibility, contact_id, description, status,
         next_action_at, next_action_note),
    )
    return (await cur.fetchone())["id"]


async def due_for(conn, user: User) -> list[dict]:
    """Leads this user must act on. Ownership-scoped, not visibility-scoped:
    a colleague's org-visible due lead is readable but is not this person's
    work — filtering by visible() would nag every agent about everyone
    else's follow-ups every morning."""
    cur = await conn.execute(
        f"""{_SELECT}
            WHERE l.user_id = %(uid)s
              AND l.next_action_at IS NOT NULL AND l.next_action_at <= now()
              AND l.status IN ('new','active')
            ORDER BY l.next_action_at""",
        {"uid": user.id},
    )
    return await cur.fetchall()


async def query(conn, user: User, *, due_only: bool = False, status: str | None = None) -> list[dict]:
    """Answering a question the user asked, so visibility-scoped rather than
    ownership-scoped — the same table as due_for() answering a different
    question."""
    clauses = [visible("l")]
    params: dict = {"scope_user_id": user.id}
    if due_only:
        clauses.append("l.next_action_at <= now()")
    if status:
        clauses.append("l.status = %(status)s")
        params["status"] = status

    cur = await conn.execute(
        f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY l.next_action_at NULLS LAST LIMIT 25",
        params,
    )
    return await cur.fetchall()


async def update(conn, user: User, lead_id: UUID, **fields) -> bool:
    sets, params = ["updated_at = now()"], {"id": lead_id, "uid": user.id}
    for key in _UPDATABLE:
        if fields.get(key) is not None:
            sets.append(f"{key} = %({key})s")
            params[key] = fields[key]

    cur = await conn.execute(
        f"UPDATE leads SET {', '.join(sets)} WHERE id = %(id)s AND user_id = %(uid)s RETURNING id",
        params,
    )
    return await cur.fetchone() is not None


async def mark_nudged(conn, user: User, ids: list[UUID]) -> None:
    if not ids:
        return
    await conn.execute(
        """UPDATE leads SET nudge_count = nudge_count + 1, last_nudged_at = now()
           WHERE id = ANY(%s) AND user_id = %s""",
        (ids, user.id),
    )
