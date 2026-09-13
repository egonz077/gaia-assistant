from gaia.capabilities.leads import (
    CAPABILITY,
    complete_commitments,
    create_lead,
    list_commitments,
    query_leads,
    update_lead,
)
from gaia.core.db import meetings as meetings_db


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


async def _commitments(conn, user, *descriptions):
    await meetings_db.save(
        conn,
        user,
        summary="Showed Coral Gables",
        source="text",
        commitments=[{"description": d} for d in descriptions],
    )


async def test_list_commitments_hands_back_the_id_needed_to_close_one(conn, ana):
    """The bug this tool exists for: the model had no way to see a commitment,
    so the only thing it could do with "clear my commitments" was ask the user
    for a uuid."""
    await _commitments(conn, ana, "Send comps")

    rows = (await list_commitments(conn, ana, {}))["commitments"]
    assert [r["description"] for r in rows] == ["Send comps"]
    assert rows[0]["id"]


async def test_complete_commitments_closes_everything_it_was_given(conn, ana):
    await _commitments(conn, ana, "Send comps", "Call the attorney")
    ids = [r["id"] for r in (await list_commitments(conn, ana, {}))["commitments"]]

    result = await complete_commitments(conn, ana, {"commitment_ids": ids})

    assert result["completed"] == 2
    assert (await list_commitments(conn, ana, {}))["commitments"] == []


async def test_complete_commitments_says_so_when_some_did_not_close(conn, ana, sofia):
    """The model builds its reply from this. Reporting two of three as three
    tells the user something was cleared that is still open."""
    await _commitments(conn, ana, "Send comps")
    await _commitments(conn, sofia, "Sofia's task")
    mine = [r["id"] for r in (await list_commitments(conn, ana, {}))["commitments"]]
    hers = [r["id"] for r in (await list_commitments(conn, sofia, {}))["commitments"]]

    result = await complete_commitments(conn, ana, {"commitment_ids": mine + hers})

    assert result["completed"] == 1
    assert result["not_found"] == 1
    assert len((await list_commitments(conn, sofia, {}))["commitments"]) == 1


async def test_a_users_commitments_are_not_listed_to_a_colleague(conn, ana, sofia):
    await _commitments(conn, sofia, "Sofia's task")

    assert (await list_commitments(conn, ana, {}))["commitments"] == []


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert {t.name for t in CAPABILITY.tools} == {
        "create_lead", "query_leads", "update_lead",
        "list_commitments", "complete_commitments",
    }


def test_completing_commitments_requires_ids_the_model_looked_up_itself():
    """A guard on the wording, because the wording is the fix. The schema
    cannot stop the model asking the user for an id; the tool descriptions
    and the prompt fragment are the only things that can, so they are the
    part worth a tripwire."""
    tools = {t.name: t for t in CAPABILITY.tools}
    assert "list_commitments" in tools["complete_commitments"].description
    assert "never ask the user for an id" in CAPABILITY.prompt_fragment.lower()


async def test_commitment_ids_arrive_from_the_model_as_strings(conn, ana):
    """What the real model actually sends. Tool arguments come out of JSON, so
    `commitment_ids` is a list of strings and never a list of UUIDs — every
    other test here passes the objects it got back from the db layer, which is
    the one shape production never sees."""
    await _commitments(conn, ana, "Send comps")
    ids = [str(r["id"]) for r in (await list_commitments(conn, ana, {}))["commitments"]]

    assert (await complete_commitments(conn, ana, {"commitment_ids": ids}))["completed"] == 1
