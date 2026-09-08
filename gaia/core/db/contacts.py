from uuid import UUID

from gaia.core.db.scope import visible
from gaia.core.models import User

PROFILE_CAP = 4000  # characters; consolidation is deferred, the cap is not


async def create_contact(conn, user: User, *, name: str, visibility: str = "org") -> UUID:
    cur = await conn.execute(
        "INSERT INTO contacts (user_id, visibility, name) VALUES (%s,%s,%s) RETURNING id",
        (user.id, visibility, name),
    )
    return (await cur.fetchone())["id"]


async def get_or_create(conn, user: User, name: str) -> UUID:
    cur = await conn.execute(
        f"""SELECT id FROM contacts t
            WHERE lower(t.name) = lower(%(name)s) AND {visible('t')}
            ORDER BY t.updated_at DESC LIMIT 1""",
        {"name": name, "scope_user_id": user.id},
    )
    row = await cur.fetchone()
    return row["id"] if row else await create_contact(conn, user, name=name)


async def roster(conn, user: User, limit: int = 40) -> list[str]:
    """Names only. Full profiles are fetched on demand via lookup(), so the
    request does not grow with the company's whole contact book."""
    cur = await conn.execute(
        f"""SELECT t.name FROM contacts t
            WHERE {visible('t')}
            ORDER BY t.updated_at DESC LIMIT %(limit)s""",
        {"scope_user_id": user.id, "limit": limit},
    )
    return [r["name"] for r in await cur.fetchall()]


async def lookup(conn, user: User, name: str) -> dict | None:
    cur = await conn.execute(
        f"""SELECT t.name, t.profile, t.phone, t.email FROM contacts t
            WHERE lower(t.name) = lower(%(name)s) AND {visible('t')}
            ORDER BY t.updated_at DESC LIMIT 1""",
        {"name": name, "scope_user_id": user.id},
    )
    return await cur.fetchone()


async def merge_profile(conn, user: User, contact_id: UUID, update: str) -> None:
    """Append a fact, then trim from the front if the profile exceeds the cap.

    Append-only accretion is inherited from the prototype; the cap keeps it
    bounded until a consolidation pass rewrites profiles properly.
    """
    await conn.execute(
        """UPDATE contacts
           SET profile = CASE WHEN profile = '' THEN %(u)s ELSE profile || ' | ' || %(u)s END,
               updated_at = now()
           WHERE id = %(id)s""",
        {"u": update, "id": contact_id},
    )
    await conn.execute(
        """UPDATE contacts SET profile = right(profile, %(cap)s)
           WHERE id = %(id)s AND length(profile) > %(cap)s""",
        {"cap": PROFILE_CAP, "id": contact_id},
    )
