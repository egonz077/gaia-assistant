from gaia.capabilities.meetings import (
    CAPABILITY,
    lookup_contact,
    save_meeting,
    search_memory,
    set_meeting_visibility,
)


async def test_save_meeting_files_everything(conn, ana):
    result = await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables to the Delgados",
        "contacts": [{"name": "Maria Delgado", "profile_update": "wants a pool"}],
        "commitments": [{"description": "Send comps", "contact_name": "Maria Delgado"}],
    })
    assert result["saved"] is True

    hits = await search_memory(conn, ana, {"query": "Coral Gables"})
    assert any("Coral Gables" in h["content"] for h in hits["results"])


async def test_save_meeting_respects_private(conn, ana, sofia):
    await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera"}],
        "private": True,
    })
    assert (await search_memory(conn, sofia, {"query": "divorce"}))["results"] == []
    assert (await search_memory(conn, ana, {"query": "divorce"}))["results"] != []


CONFIDENCE = "divorcing, must sell by Dec, will take 540 if pushed"


async def test_a_private_meetings_profile_update_never_reaches_the_company(conn, ana, sofia):
    """The failure `private` exists to prevent, one table over.

    `contacts.profile` is a single org-visible string on a row `get_or_create`
    hands back across users, so there is nowhere on a shared contact to put a
    fact learned in confidence. Merging one there published it verbatim to
    every agent at Gaia through lookup_contact, while the meeting itself was
    correctly hidden — hidden from the meetings list and fully readable by
    everyone, which looks private and is not.
    """
    result = await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera", "profile_update": CONFIDENCE}],
        "private": True,
    })

    # Not through the tool that reads profiles...
    sofia_view = await lookup_contact(conn, sofia, {"name": "Rivera"})
    assert CONFIDENCE not in str(sofia_view)

    # ...nor through semantic search...
    assert (await search_memory(conn, sofia, {"query": "divorce sale"}))["results"] == []

    # ...nor anywhere else, because it was never written to a shared row at all.
    cur = await conn.execute("SELECT profile FROM contacts WHERE name = 'Rivera'")
    assert [r["profile"] for r in await cur.fetchall()] == [""]

    # Even the owner does not get it on the shared profile; it lives in the
    # private meeting note, which only she can search.
    ana_view = await lookup_contact(conn, ana, {"name": "Rivera"})
    assert CONFIDENCE not in str(ana_view)
    assert (await search_memory(conn, ana, {"query": "divorce sale"}))["results"] != []

    # And the model is told, not silently ignored — otherwise it reports back
    # that it filed something it did not file.
    assert result["profile_updates_skipped"] == ["Rivera"]


async def test_an_org_meetings_profile_update_still_merges(conn, ana, sofia):
    """The gate is on privacy, not on profile_update as such — a normal
    meeting must still build the shared profile, or the company's contact
    book stops growing."""
    result = await save_meeting(conn, ana, {
        "summary": "Showed the Coral Gables place",
        "contacts": [{"name": "Rivera", "profile_update": "wants a pool"}],
    })

    assert "profile_updates_skipped" not in result
    colleague_view = await lookup_contact(conn, sofia, {"name": "Rivera"})
    assert "wants a pool" in colleague_view["contact"]["profile"]


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert CAPABILITY.allowed_user_ids is None
    assert {t.name for t in CAPABILITY.tools} == {
        "save_meeting", "search_memory", "lookup_contact", "set_meeting_visibility"
    }


async def test_lookup_contact_finds_an_existing_contact(conn, ana):
    await save_meeting(conn, ana, {
        "summary": "Met with a buyer",
        "contacts": [{"name": "Jorge", "profile_update": "wants waterfront"}],
    })

    result = await lookup_contact(conn, ana, {"name": "Jorge"})

    assert result["contact"]["name"] == "Jorge"
    assert "waterfront" in result["contact"]["profile"]


async def test_lookup_contact_reports_a_missing_contact(conn, ana):
    result = await lookup_contact(conn, ana, {"name": "Nobody"})
    assert result == {"contact": None, "note": "no such contact"}


async def test_set_meeting_visibility_cascades_through_the_tool(conn, ana):
    filed = await save_meeting(conn, ana, {
        "summary": "Routine showing",
        "contacts": [{"name": "Buyer"}],
        "commitments": [{"description": "Follow up", "contact_name": "Buyer"}],
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, ana, {"meeting_id": meeting_id, "private": True})
    assert outcome == {"changed": True, "visibility": "private"}

    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "private"

    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (meeting_id,)
    )
    assert (await cur.fetchone())["visibility"] == "private"

    cur = await conn.execute(
        "SELECT visibility FROM memory_chunks WHERE meeting_id = %s", (meeting_id,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_set_meeting_visibility_colleague_cannot_reclassify(conn, ana, sofia):
    filed = await save_meeting(conn, ana, {
        "summary": "Ana's meeting",
        "contacts": [{"name": "Buyer"}],
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, sofia, {"meeting_id": meeting_id, "private": True})

    assert outcome == {"changed": False, "note": "no such meeting, or it belongs to someone else"}
    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "org"


async def test_set_meeting_visibility_can_restore_to_org(conn, ana):
    filed = await save_meeting(conn, ana, {
        "summary": "Sensitive meeting",
        "contacts": [{"name": "Buyer"}],
        "private": True,
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, ana, {"meeting_id": meeting_id, "private": False})

    assert outcome == {"changed": True, "visibility": "org"}
    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "org"
