"""The durable half of the approval gate.

The weak version of this gate is a prompt rule: the model reads the addresses
back, the human agrees, the model calls add_attendees(event_id, emails). That
trusts the model to pass the same list it read back.

Here, propose_invite stores the exact list and confirm_invite takes only an id.
The model cannot smuggle a different recipient into the confirmation because
confirmation accepts no recipients.
"""

from uuid import UUID

from gaia.core.models import User


async def create(conn, user: User, *, event_id: str, emails: list[str],
                 ttl_minutes: int = 60) -> UUID:
    cur = await conn.execute(
        """INSERT INTO pending_invites (user_id, event_id, emails, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(mins => %s)) RETURNING id""",
        (user.id, event_id, emails, ttl_minutes),
    )
    return (await cur.fetchone())["id"]


async def open_for(conn, user: User) -> list[dict]:
    """Every approval this user proposed and has not yet confirmed or let
    expire, oldest first. Ownership-scoped like claim, for the same reason.

    Exists because history is prose: the pending_id that propose_invite
    returned dies at the turn boundary, and on "send it" the model has to
    look the approval up rather than guess an event id -- which it did, live,
    before this existed.
    """
    cur = await conn.execute(
        """SELECT id, event_id, emails, expires_at FROM pending_invites
           WHERE user_id = %s AND confirmed_at IS NULL AND expires_at > now()
           ORDER BY created_at""",
        (user.id,),
    )
    return await cur.fetchall()


async def claim(conn, user: User, pending_id: UUID) -> dict | None:
    """Ownership-scoped, single-use and expiring, in one statement.

    Claiming in the UPDATE rather than reading then writing means two
    confirmations racing cannot both succeed -- one updates the row, the other
    matches nothing and gets None.
    """
    cur = await conn.execute(
        """UPDATE pending_invites SET confirmed_at = now()
           WHERE id = %s AND user_id = %s
             AND confirmed_at IS NULL AND expires_at > now()
           RETURNING event_id, emails""",
        (pending_id, user.id),
    )
    row = await cur.fetchone()
    return {"event_id": row["event_id"], "emails": row["emails"]} if row else None
