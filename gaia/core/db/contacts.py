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
    never met, in a firm where each developer runs their own deals. A false premise
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


async def merge(conn, user: User, source_id: UUID, target_id: UUID) -> bool:
    """Fold `source` into `target` and delete the source row. Returns False if
    either row is not one this user can see, or if they are the same row.

    The remedy the schema already assumes exists. `idx_contacts_name` is
    deliberately non-unique — two different people share a name often enough
    that a unique constraint would force bad data — and `get_or_create` has a
    check-then-insert race that can produce a second row for the same person.
    Both were accepted on the grounds that this command resolves the result,
    and it did not exist.

    Scoped by `visible()`, like every other contact write: a shared entity may
    be edited by anyone who can read it, but nobody can fold a colleague's
    private contact into an org row — which would publish its profile to the
    company — or touch a private row at all.

    Lossy, and knowingly so (spec §10.1): `profile` is an append-only string,
    so the two histories are concatenated rather than interleaved. Everything
    else moves cleanly. Contact details are filled in rather than overwritten,
    so a merge never discards a phone number the survivor lacked.
    """
    if source_id == target_id:
        return False

    cur = await conn.execute(
        f"""SELECT t.id FROM contacts t
            WHERE t.id = ANY(%(ids)s) AND {visible('t')}""",
        {"ids": [source_id, target_id], "scope_user_id": user.id},
    )
    if len({r["id"] for r in await cur.fetchall()}) != 2:
        return False

    both = {"src": source_id, "dst": target_id}
    for table in ("leads", "commitments", "memory_chunks"):
        await conn.execute(
            f"UPDATE {table} SET contact_id = %(dst)s WHERE contact_id = %(src)s", both
        )

    # meeting_contacts is keyed (meeting_id, contact_id), so a meeting that
    # named both duplicates would collide on a plain UPDATE.
    await conn.execute(
        """INSERT INTO meeting_contacts (meeting_id, contact_id)
           SELECT meeting_id, %(dst)s FROM meeting_contacts WHERE contact_id = %(src)s
           ON CONFLICT DO NOTHING""",
        both,
    )
    await conn.execute("DELETE FROM meeting_contacts WHERE contact_id = %(src)s", both)

    await conn.execute(
        """UPDATE contacts dst SET
               profile = right(
                   CASE
                       WHEN src.profile = '' THEN dst.profile
                       WHEN dst.profile = '' THEN src.profile
                       ELSE dst.profile || ' | ' || src.profile
                   END, %(cap)s),
               phone = COALESCE(dst.phone, src.phone),
               email = COALESCE(dst.email, src.email),
               updated_at = now()
           FROM contacts src
           WHERE dst.id = %(dst)s AND src.id = %(src)s""",
        {**both, "cap": PROFILE_CAP},
    )
    await conn.execute("DELETE FROM contacts WHERE id = %(src)s", both)
    return True


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
