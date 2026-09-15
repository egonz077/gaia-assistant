from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from tests.factories import make_row


async def test_set_and_look_up_a_lead_by_event(conn, ana):
    lead_id = await make_row(conn, "leads", ana)
    assert await leads_db.set_calendar_event(conn, ana, lead_id, "evt-1")
    assert (await leads_db.by_event_ids(conn, ana, ["evt-1"]))["evt-1"]["lead_id"] == lead_id


async def test_lookup_is_ownership_scoped(conn, ana, sofia):
    """Sofia's digest must not describe Ana's event, even though an org-visible
    lead is readable. This answers 'what does this person's day hold'."""
    lead_id = await make_row(conn, "leads", ana, visibility="org")
    await leads_db.set_calendar_event(conn, ana, lead_id, "evt-1")
    assert await leads_db.by_event_ids(conn, sofia, ["evt-1"]) == {}


async def test_missing_event_is_not_an_error(conn, ana):
    """People delete events. A calendar_event_id pointing at nothing is normal."""
    assert await leads_db.by_event_ids(conn, ana, ["gone"]) == {}


async def test_commitments_correlate_too(conn, ana):
    cid = await make_row(conn, "commitments", ana)
    assert await commitments_db.set_calendar_event(conn, ana, cid, "evt-2")
    assert (await commitments_db.by_event_ids(conn, ana, ["evt-2"]))["evt-2"]["commitment_id"] == cid


async def test_setting_an_event_on_someone_elses_lead_fails(conn, ana, sofia):
    lead_id = await make_row(conn, "leads", ana)
    assert await leads_db.set_calendar_event(conn, sofia, lead_id, "evt-1") is False
