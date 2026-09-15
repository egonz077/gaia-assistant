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
