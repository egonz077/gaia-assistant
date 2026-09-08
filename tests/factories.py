"""Minimal valid rows for every domain table, so isolation tests can be
parametrized rather than hand-written per table."""

from uuid import UUID


async def make_row(conn, table: str, user, visibility: str = "org") -> UUID:
    if table == "contacts":
        return await _one(
            conn,
            "INSERT INTO contacts (user_id, visibility, name) VALUES (%s,%s,%s) RETURNING id",
            (user.id, visibility, "Delgado"),
        )
    if table == "meetings":
        return await _one(
            conn,
            """INSERT INTO meetings (user_id, visibility, source, summary)
               VALUES (%s,%s,'text',%s) RETURNING id""",
            (user.id, visibility, "Showing at Coral Gables"),
        )
    if table == "leads":
        contact_id = await make_row(conn, "contacts", user, visibility)
        return await _one(
            conn,
            """INSERT INTO leads (user_id, visibility, contact_id, description)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (user.id, visibility, contact_id, "Buying, ~600k"),
        )
    if table == "commitments":
        return await _one(
            conn,
            """INSERT INTO commitments (user_id, visibility, description)
               VALUES (%s,%s,%s) RETURNING id""",
            (user.id, visibility, "Send the listing update"),
        )
    if table == "memory_chunks":
        return await _one(
            conn,
            """INSERT INTO memory_chunks (user_id, visibility, content, embedding)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (user.id, visibility, "They liked the kitchen", [0.0] * 1024),
        )
    raise AssertionError(f"tests/factories.py has no builder for {table!r}")


async def _one(conn, sql: str, params: tuple) -> UUID:
    cur = await conn.execute(sql, params)
    return (await cur.fetchone())["id"]
