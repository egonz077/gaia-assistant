from datetime import datetime, timedelta, timezone

from gaia.core.db import leads as leads_db


async def test_a_lead_can_be_created_and_read_back(conn, ana):
    lead_id = await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying in Coral Gables, ~600k"
    )
    rows = await leads_db.query(conn, ana)
    assert [r["id"] for r in rows] == [lead_id]
    assert rows[0]["name"] == "Maria Delgado"


async def test_due_leads_are_scoped_by_ownership(conn, ana, sofia):
    """Sofia's due lead is org-visible, and still must not appear in Ana's."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await leads_db.create(
        conn, sofia, contact_name="Rivera", description="Selling", next_action_at=past
    )
    assert await leads_db.due_for(conn, ana) == []
    assert len(await leads_db.due_for(conn, sofia)) == 1


async def test_query_is_scoped_by_visibility(conn, ana, sofia):
    await leads_db.create(
        conn, ana, contact_name="Private Client", description="Quiet sale",
        visibility="private",
    )
    assert await leads_db.query(conn, sofia) == []
    assert len(await leads_db.query(conn, ana)) == 1


async def test_update_changes_status_and_next_action(conn, ana):
    lead_id = await leads_db.create(
        conn, ana, contact_name="Maria Delgado", description="Buying"
    )
    when = datetime.now(timezone.utc) + timedelta(days=3)
    assert await leads_db.update(
        conn, ana, lead_id, status="active", next_action_at=when,
        next_action_note="Send Friday listings",
    ) is True

    row = (await leads_db.query(conn, ana))[0]
    assert row["status"] == "active"
    assert row["next_action_note"] == "Send Friday listings"


async def test_update_cannot_touch_another_users_lead(conn, ana, sofia):
    lead_id = await leads_db.create(conn, ana, contact_name="Maria", description="Buying")
    assert await leads_db.update(conn, sofia, lead_id, status="lost") is False
