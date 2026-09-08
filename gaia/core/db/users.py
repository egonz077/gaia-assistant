from gaia.core.models import User

_COLUMNS = "id, name, wa_id, role, timezone, active"


def _row_to_user(row: dict) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        wa_id=row["wa_id"],
        role=row["role"],
        timezone=row["timezone"],
        active=row["active"],
    )


async def create_user(
    conn,
    *,
    name: str,
    wa_id: str,
    role: str = "agent",
    timezone: str = "America/New_York",
) -> User:
    cur = await conn.execute(
        f"""INSERT INTO users (name, wa_id, role, timezone)
            VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}""",
        (name, wa_id, role, timezone),
    )
    return _row_to_user(await cur.fetchone())


async def get_by_wa_id(conn, wa_id: str) -> User | None:
    """Resolve an inbound number. Inactive users resolve to None, so
    deactivation is immediate revocation."""
    cur = await conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE wa_id = %s AND active", (wa_id,)
    )
    row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def list_users(conn) -> list[User]:
    # wa_id tiebreaker: two rows created in the same transaction share an
    # identical created_at (Postgres now() is frozen per-transaction), so
    # ORDER BY created_at alone leaves their relative order unspecified.
    cur = await conn.execute(f"SELECT {_COLUMNS} FROM users ORDER BY created_at, wa_id")
    return [_row_to_user(r) for r in await cur.fetchall()]


async def deactivate(conn, wa_id: str) -> bool:
    cur = await conn.execute(
        "UPDATE users SET active = false WHERE wa_id = %s RETURNING id", (wa_id,)
    )
    return await cur.fetchone() is not None


async def touch_inbound(conn, user: User) -> None:
    """Record that this user just messaged us — drives the 24h window check."""
    await conn.execute("UPDATE users SET last_inbound_at = now() WHERE id = %s", (user.id,))
