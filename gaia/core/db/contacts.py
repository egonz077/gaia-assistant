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
    """Names of people *this user* has actually worked with, most recently
    touched first.

    Names only. Full profiles are fetched on demand via lookup(), so the
    request does not grow with the company's whole contact book.

    Scoped by ownership as well as visibility, which is unusual for a read and
    is the point. The system prompt states this list in words — "people {name}
    has worked with recently" — and a purely visibility-scoped query returned
    the *company's* 40 most recently touched contacts. Ana's prompt then
    asserted, as fact, that she had recently worked with Sofia's clients, and
    the model acted on it: "how did it go with Rivera?" about someone she has
    never met, in a brokerage where agents guard their books. A false premise
    in a system prompt makes everything downstream of it confidently wrong.

    A contact is reachable from a user's own work in exactly two ways —
    through a meeting (meeting_contacts) or through a lead. Commitments are
    only ever created by meetings.save alongside the meeting they came from,
    so they are covered transitively. `visible()` still applies: ownership
    narrows this list, it never widens it.
    """
    cur = await conn.execute(
        f"""SELECT t.name FROM contacts t
            WHERE {visible('t')}
              AND (EXISTS (SELECT 1 FROM meeting_contacts mc
                             JOIN meetings m ON m.id = mc.meeting_id
                            WHERE mc.contact_id = t.id AND m.user_id = %(uid)s)
                OR EXISTS (SELECT 1 FROM leads l
                            WHERE l.contact_id = t.id AND l.user_id = %(uid)s))
            ORDER BY t.updated_at DESC LIMIT %(limit)s""",
        {"scope_user_id": user.id, "uid": user.id, "limit": limit},
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
    bounded until a consolidation pass rewrites profiles properly. Both
    statements are scoped by `visible()` — a caller cannot write into a
    contact they cannot read, even one whose id they somehow obtained
    outside a scoped query.
    """
    await conn.execute(
        f"""UPDATE contacts t
           SET profile = CASE WHEN t.profile = '' THEN %(u)s ELSE t.profile || ' | ' || %(u)s END,
               updated_at = now()
           WHERE t.id = %(id)s AND {visible('t')}""",
        {"u": update, "id": contact_id, "scope_user_id": user.id},
    )
    await conn.execute(
        f"""UPDATE contacts t SET profile = right(t.profile, %(cap)s)
           WHERE t.id = %(id)s AND length(t.profile) > %(cap)s AND {visible('t')}""",
        {"cap": PROFILE_CAP, "id": contact_id, "scope_user_id": user.id},
    )
