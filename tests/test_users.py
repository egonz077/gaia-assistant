from gaia.core.db import users as users_db


async def test_create_and_look_up_by_wa_id(conn):
    created = await users_db.create_user(conn, name="Ana", wa_id="13055550001")
    found = await users_db.get_by_wa_id(conn, "13055550001")
    assert found is not None
    assert found.id == created.id
    assert found.name == "Ana"
    assert found.role == "agent"
    assert found.timezone == "America/New_York"


async def test_unknown_number_is_none(conn):
    assert await users_db.get_by_wa_id(conn, "19998887777") is None


async def test_deactivated_user_does_not_resolve(conn, ana):
    assert await users_db.deactivate(conn, ana.wa_id) is True
    assert await users_db.get_by_wa_id(conn, ana.wa_id) is None


async def test_wa_id_is_unique(conn, ana):
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.UniqueViolation):
        await users_db.create_user(conn, name="Impostor", wa_id=ana.wa_id)


async def test_list_users_returns_all_in_creation_order(conn, ana, sofia):
    listed = await users_db.list_users(conn)
    assert [u.wa_id for u in listed] == [ana.wa_id, sofia.wa_id]


async def test_deactivate_unknown_number_returns_false(conn):
    assert await users_db.deactivate(conn, "19998887777") is False


async def test_touch_inbound_sets_last_inbound_at(conn, ana):
    await users_db.touch_inbound(conn, ana)
    row = await (
        await conn.execute("SELECT last_inbound_at FROM users WHERE id = %s", (ana.id,))
    ).fetchone()
    assert row["last_inbound_at"] is not None
