from gaia.capabilities.leads import CAPABILITY, create_lead, query_leads, update_lead


async def test_create_then_query(conn, ana):
    created = await create_lead(conn, ana, {
        "contact_name": "Maria Delgado",
        "description": "Buying in Coral Gables, ~600k",
        "next_action_at": "2026-09-15T14:00:00Z",
        "next_action_note": "Send Friday listings",
    })
    assert created["created"] is True

    rows = (await query_leads(conn, ana, {}))["leads"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Maria Delgado"


async def test_update_marks_a_lead_lost(conn, ana):
    created = await create_lead(conn, ana, {"contact_name": "Rivera", "description": "Selling"})
    result = await update_lead(conn, ana, {"lead_id": created["lead_id"], "status": "lost"})
    assert result["updated"] is True
    assert (await query_leads(conn, ana, {}))["leads"][0]["status"] == "lost"


async def test_a_users_private_lead_is_invisible_to_others(conn, ana, sofia):
    await create_lead(conn, ana, {
        "contact_name": "Quiet Client", "description": "Discreet sale", "private": True,
    })
    assert (await query_leads(conn, sofia, {}))["leads"] == []


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert {t.name for t in CAPABILITY.tools} == {
        "create_lead", "query_leads", "update_lead", "complete_commitment"
    }
