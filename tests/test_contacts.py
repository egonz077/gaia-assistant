from gaia.core.db import contacts as contacts_db


async def test_get_or_create_reuses_an_existing_visible_contact(conn, ana):
    first = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    second = await contacts_db.get_or_create(conn, ana, "maria delgado")
    assert first == second


async def test_get_or_create_does_not_reuse_another_users_private_contact(conn, ana, sofia):
    hidden = await contacts_db.create_contact(
        conn, ana, name="Maria Delgado", visibility="private"
    )
    mine = await contacts_db.get_or_create(conn, sofia, "Maria Delgado")
    assert mine != hidden


async def test_roster_returns_names_not_profiles(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    names = await contacts_db.roster(conn, ana)
    assert names == ["Maria Delgado"]


async def test_lookup_returns_the_profile(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    await contacts_db.merge_profile(conn, ana, cid, "budget 600k")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert found is not None
    assert "wants a pool" in found["profile"]
    assert "budget 600k" in found["profile"]


async def test_lookup_cannot_read_another_users_private_contact(conn, ana, sofia):
    cid = await contacts_db.create_contact(
        conn, ana, name="Maria Delgado", visibility="private"
    )
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    assert await contacts_db.lookup(conn, sofia, "Maria Delgado") is None


async def test_profile_is_capped(conn, ana):
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    for i in range(400):
        await contacts_db.merge_profile(conn, ana, cid, f"fact number {i}")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert len(found["profile"]) <= contacts_db.PROFILE_CAP
