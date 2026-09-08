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
    from gaia.core.db import meetings as meetings_db

    await meetings_db.save(
        conn, ana, summary="Showing", source="text", contact_names=("Maria Delgado",)
    )
    cid = await contacts_db.get_or_create(conn, ana, "Maria Delgado")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    names = await contacts_db.roster(conn, ana)
    assert names == ["Maria Delgado"]


async def test_roster_is_the_users_own_contacts_not_the_companys(conn, ana, sofia):
    """The system prompt states this list in words — "people {name} has worked
    with recently". Visibility-scoping alone returned the whole company's
    recently-touched contacts, so Ana's prompt asserted as fact that she had
    worked with Sofia's clients and the model asked her how it went."""
    from gaia.core.db import leads as leads_db
    from gaia.core.db import meetings as meetings_db

    await meetings_db.save(
        conn, sofia, summary="Sofia's listing appointment", source="text",
        contact_names=("Rivera",),
    )
    await meetings_db.save(
        conn, ana, summary="Ana's showing", source="text", contact_names=("Delgado",),
    )
    await leads_db.create(conn, ana, contact_name="Marco", description="Buying")

    # Rivera is org-visible and Ana could look him up — he is simply not
    # someone she has worked with.
    assert await contacts_db.lookup(conn, ana, "Rivera") is not None
    assert sorted(await contacts_db.roster(conn, ana)) == ["Delgado", "Marco"]
    assert await contacts_db.roster(conn, sofia) == ["Rivera"]


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


async def test_merge_profile_cannot_write_into_another_users_private_contact(conn, ana, sofia):
    cid = await contacts_db.create_contact(conn, ana, name="Maria Delgado", visibility="private")
    await contacts_db.merge_profile(conn, sofia, cid, "wants a pool")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert found["profile"] == ""


async def test_merge_profile_works_for_the_owner_of_a_private_contact(conn, ana):
    cid = await contacts_db.create_contact(conn, ana, name="Maria Delgado", visibility="private")
    await contacts_db.merge_profile(conn, ana, cid, "wants a pool")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert "wants a pool" in found["profile"]


async def test_merge_profile_works_for_a_colleague_on_an_org_visible_contact(conn, ana, sofia):
    cid = await contacts_db.create_contact(conn, ana, name="Maria Delgado", visibility="org")
    await contacts_db.merge_profile(conn, sofia, cid, "wants a pool")
    found = await contacts_db.lookup(conn, ana, "Maria Delgado")
    assert "wants a pool" in found["profile"]
