import pytest

from gaia.core.db.scope import DOMAIN_TABLES, visible
from tests.factories import make_row


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_private_rows_are_invisible_to_other_users(conn, ana, sofia, table):
    """Ana files something private; Sofia must not be able to read it."""
    row_id = await make_row(conn, table, ana, visibility="private")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": sofia.id},
    )
    assert await cur.fetchone() is None, f"{table}: Sofia can read Ana's private row"


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_org_rows_are_visible_to_other_users(conn, ana, sofia, table):
    row_id = await make_row(conn, table, ana, visibility="org")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": sofia.id},
    )
    assert await cur.fetchone() is not None, f"{table}: org row hidden from Sofia"


@pytest.mark.parametrize("table", DOMAIN_TABLES)
async def test_owners_always_see_their_own_private_rows(conn, ana, table):
    row_id = await make_row(conn, table, ana, visibility="private")

    cur = await conn.execute(
        f"SELECT id FROM {table} t WHERE t.id = %(id)s AND {visible('t')}",
        {"id": row_id, "scope_user_id": ana.id},
    )
    assert await cur.fetchone() is not None
