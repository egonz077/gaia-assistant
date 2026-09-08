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


class TestMerge:
    """`admin merge-contacts` is the stated remedy for two accepted risks —
    the deliberately non-unique index on lower(name), and get_or_create's
    check-then-insert race — and it did not exist, so both acceptances rested
    on a false premise."""

    async def _dupes(self, conn, user):
        from gaia.core.db import leads as leads_db
        from gaia.core.db import meetings as meetings_db

        keep = await contacts_db.create_contact(conn, user, name="Maria Delgado")
        dupe = await contacts_db.create_contact(conn, user, name="maria delgado")
        await contacts_db.merge_profile(conn, user, keep, "wants a pool")
        await contacts_db.merge_profile(conn, user, dupe, "budget 600k")
        await conn.execute(
            "UPDATE contacts SET phone = '13055551111' WHERE id = %s", (dupe,)
        )
        await meetings_db.save(
            conn, user, summary="Showing", source="text", contact_names=("maria delgado",),
            commitments=({"description": "Send comps", "contact_name": "maria delgado"},),
        )
        await leads_db.create(conn, user, contact_name="maria delgado", description="Buying")
        await conn.execute(
            """INSERT INTO memory_chunks (user_id, visibility, contact_id, content, embedding)
               VALUES (%s,'org',%s,%s,%s)""",
            (user.id, dupe, "They liked the kitchen", [0.0] * 1024),
        )
        return keep, dupe

    async def test_merge_moves_everything_and_deletes_the_source(self, conn, ana):
        from gaia.core.db import leads as leads_db

        keep, dupe = await self._dupes(conn, ana)

        assert await contacts_db.merge(conn, ana, dupe, keep) is True

        cur = await conn.execute("SELECT id, profile, phone FROM contacts")
        rows = await cur.fetchall()
        assert [r["id"] for r in rows] == [keep]          # the duplicate is gone
        assert "wants a pool" in rows[0]["profile"]
        assert "budget 600k" in rows[0]["profile"]
        assert rows[0]["phone"] == "13055551111"          # detail carried over, not lost

        # Every reference now points at the survivor rather than cascading away.
        for table in ("leads", "commitments", "memory_chunks", "meeting_contacts"):
            cur = await conn.execute(f"SELECT contact_id FROM {table}")
            assert [r["contact_id"] for r in await cur.fetchall()] == [keep], table

        assert [r["name"] for r in await leads_db.query(conn, ana)] == ["Maria Delgado"]

    async def test_merge_survives_a_meeting_that_named_both_duplicates(self, conn, ana):
        """meeting_contacts is keyed (meeting_id, contact_id), so a plain
        UPDATE would collide on the row that already exists."""
        from gaia.core.db import meetings as meetings_db

        keep = await contacts_db.create_contact(conn, ana, name="Maria Delgado")
        dupe = await contacts_db.create_contact(conn, ana, name="maria delgado")
        meeting = await meetings_db.save(conn, ana, summary="Joint", source="text")
        for cid in (keep, dupe):
            await conn.execute(
                "INSERT INTO meeting_contacts (meeting_id, contact_id) VALUES (%s,%s)",
                (meeting, cid),
            )

        assert await contacts_db.merge(conn, ana, dupe, keep) is True

        cur = await conn.execute("SELECT contact_id FROM meeting_contacts")
        assert [r["contact_id"] for r in await cur.fetchall()] == [keep]

    async def test_merge_refuses_a_contact_the_user_cannot_see(self, conn, ana, sofia):
        """Otherwise a merge publishes a private profile into an org row —
        the same leak visibility exists to prevent, through a side door."""
        hidden = await contacts_db.create_contact(
            conn, ana, name="Rivera", visibility="private"
        )
        await contacts_db.merge_profile(conn, ana, hidden, "divorcing, must sell")
        shared = await contacts_db.create_contact(conn, sofia, name="Rivera")

        assert await contacts_db.merge(conn, sofia, hidden, shared) is False
        assert await contacts_db.merge(conn, sofia, shared, hidden) is False

        found = await contacts_db.lookup(conn, sofia, "Rivera")
        assert "divorcing" not in found["profile"]

    async def test_merge_refuses_a_row_into_itself(self, conn, ana):
        cid = await contacts_db.create_contact(conn, ana, name="Maria Delgado")
        assert await contacts_db.merge(conn, ana, cid, cid) is False

    async def test_merged_profile_respects_the_cap(self, conn, ana):
        keep = await contacts_db.create_contact(conn, ana, name="Maria Delgado")
        dupe = await contacts_db.create_contact(conn, ana, name="maria delgado")
        for cid in (keep, dupe):
            for i in range(300):
                await contacts_db.merge_profile(conn, ana, cid, f"fact number {i}")

        await contacts_db.merge(conn, ana, dupe, keep)

        found = await contacts_db.lookup(conn, ana, "Maria Delgado")
        assert len(found["profile"]) <= contacts_db.PROFILE_CAP
