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
