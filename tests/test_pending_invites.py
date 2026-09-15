from uuid import uuid4

from gaia.core.db import pending_invites as pi


async def test_claim_returns_what_was_stored(conn, ana):
    """The whole design. confirm_invite takes only a pending_id, so the model
    cannot confirm a different list than the one the human was read back."""
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com", "b@x.com"])
    assert (await pi.claim(conn, ana, pid))["emails"] == ["a@x.com", "b@x.com"]


async def test_claim_is_single_use(conn, ana):
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"])
    assert await pi.claim(conn, ana, pid) is not None
    assert await pi.claim(conn, ana, pid) is None


async def test_expired_invite_cannot_be_claimed(conn, ana):
    """An approval from Tuesday must not fire on Friday."""
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"], ttl_minutes=-1)
    assert await pi.claim(conn, ana, pid) is None


async def test_one_user_cannot_claim_anothers(conn, ana, sofia):
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"])
    assert await pi.claim(conn, sofia, pid) is None


async def test_unknown_id_is_none(conn, ana):
    assert await pi.claim(conn, ana, uuid4()) is None


async def test_open_for_lists_only_live_own_rows(conn, ana, sofia):
    """The model loses every id at the turn boundary -- history is prose --
    so on "send it" it must be able to look the pending approval up rather
    than guess an event id. Same pattern as list_commitments."""
    live = await pi.create(conn, ana, event_id="evt-live", emails=["a@x.com"])
    used = await pi.create(conn, ana, event_id="evt-used", emails=["b@x.com"])
    await pi.claim(conn, ana, used)
    await pi.create(conn, ana, event_id="evt-old", emails=["c@x.com"], ttl_minutes=-1)
    await pi.create(conn, sofia, event_id="evt-sofia", emails=["d@x.com"])

    rows = await pi.open_for(conn, ana)
    assert [r["event_id"] for r in rows] == ["evt-live"]
    assert rows[0]["id"] == live and rows[0]["emails"] == ["a@x.com"]
