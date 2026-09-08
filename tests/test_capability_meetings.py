from gaia.capabilities.meetings import CAPABILITY, save_meeting, search_memory


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
        "save_meeting", "search_memory", "lookup_contact"
    }
