from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.core import crypto
from gaia.core.db import commitments as commitments_db
from gaia.core.db import google_accounts as ga
from gaia.core.db import leads as leads_db
from gaia.core.db import users as users_db
from gaia.jobs import digest
from tests.factories import make_row
from tests.fakes import FakeAnthropic, FakeResponse, FakeWhatsApp, TextBlock


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


def _revoking_http():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _event(event_id, start, end, day="2026-09-16"):
    # A fixed offset rather than a real zone: busy_intervals()/overlaps() only
    # compare these datetimes against each other, so the exact day and offset
    # never have to match whatever day the test happens to run on.
    return {
        "id": event_id,
        "start": {"dateTime": f"{day}T{start}:00-04:00"},
        "end": {"dateTime": f"{day}T{end}:00-04:00"},
    }


def _events_http(events):
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        return httpx.Response(200, json={"items": events})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_revocation_is_announced_the_first_time(conn, ana):
    """calendar_section only DECIDES to announce -- it must not stamp
    revoked_notified_at itself, because at this point nobody has been told
    anything yet. Stamping here would be true regardless of whether the
    WhatsApp send that follows actually lands; only send_digest, after
    delivery is confirmed, is in a position to say the notice was given."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    async with _revoking_http() as http:
        out = await digest.calendar_section(conn, ana, http=http, today="2026-09-16")
    assert out == {"revoked": True}
    assert (await ga.get(conn, ana))["revoked_notified_at"] is None


async def test_revocation_is_not_repeated_once_notified(conn, ana):
    """Someone may have revoked us deliberately. Telling them every morning
    is its own failure. revoked_at IS NOT NULL AND revoked_notified_at IS
    NULL is the whole rule -- no date arithmetic, nothing that can be off by
    a day near midnight."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    await ga.revoke(conn, ana)
    await ga.mark_revoked_notified(conn, ana)
    async with _revoking_http() as http:
        out = await digest.calendar_section(conn, ana, http=http, today="2026-09-18")
    assert out is None


async def test_a_user_who_never_connected_is_never_nagged(conn, ana):
    async with _revoking_http() as http:
        out = await digest.calendar_section(conn, ana, http=http, today="2026-09-16")
    assert out is None


async def test_about_accumulates_both_a_lead_and_a_commitment_on_the_same_event(conn, ana):
    """create_event accepts lead_id and commitment_id together (Task 11), so
    one event legitimately links to both. {**leads, **commitments} let the
    commitment silently overwrite the lead; about must keep both."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    lead_id = await make_row(conn, "leads", ana)
    commitment_id = await make_row(conn, "commitments", ana)
    await leads_db.set_calendar_event(conn, ana, lead_id, "evt-1")
    await commitments_db.set_calendar_event(conn, ana, commitment_id, "evt-1")

    events = [_event("evt-1", "10:00", "10:30")]
    async with _events_http(events) as http:
        out = await digest.calendar_section(conn, ana, http=http, today="2026-09-16")

    assert len(out["about"]) == 2
    lead_ids = {item.get("lead_id") for item in out["about"]}
    commitment_ids = {item.get("commitment_id") for item in out["about"]}
    assert lead_id in lead_ids
    assert commitment_id in commitment_ids


async def test_quiet_pipeline_with_a_conflict_still_sends(conn, ana, migrated):
    """The whole point of the feature: an empty pipeline must not hide a
    double-booked morning. 'nothing due' is not 'nothing to say'."""
    await users_db.touch_inbound(conn, ana)
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    events = [_event("e1", "10:00", "11:00"), _event("e2", "10:30", "11:30")]
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Your 10 and 10:30 collide.")])])

    async with _events_http(events) as http:
        sent = await digest.send_digest(conn, client, wa, ana, pool=migrated, http=http)

    assert sent is True
    assert wa.sent
    prompt = str(client.requests[0]["messages"])
    assert "overlaps" in prompt


async def test_quiet_pipeline_with_a_revoked_grant_still_sends_once(conn, ana, migrated):
    """Same rule for a lapsed grant: no leads, no commitments, but there is
    still something the developer needs to hear."""
    await users_db.touch_inbound(conn, ana)
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    wa = FakeWhatsApp()
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Your calendar disconnected.")])])

    async with _revoking_http() as http:
        sent = await digest.send_digest(conn, client, wa, ana, pool=migrated, http=http)

    assert sent is True
    prompt = str(client.requests[0]["messages"])
    assert "revoked" in prompt
    # Stamped only now, because the send that just landed is what makes
    # "we told them" true.
    assert (await ga.get(conn, ana))["revoked_notified_at"] is not None

    # And, since the pipeline is still empty and the grant is still revoked,
    # a second run says nothing further -- announced exactly once.
    client2 = FakeAnthropic([FakeResponse([TextBlock("would not be sent")])])
    async with _revoking_http() as http2:
        sent_again = await digest.send_digest(conn, client2, wa, ana, pool=migrated, http=http2)
    assert sent_again is False


async def test_a_rejected_delivery_does_not_stamp_revoked_notified_at(conn, ana, migrated):
    """A WhatsApp rejection is not an exception -- calendar_section has
    already decided to announce by the time send_digest learns the send
    failed. Stamping regardless would tell the user nothing and then, because
    the notice is deliberately once-only, never ask again: the grant stays
    silently broken forever. That is the exact "not never" failure the column
    exists to prevent, reintroduced one layer down. The next tick must retry,
    which means the column must still be NULL after a rejected send."""
    await users_db.touch_inbound(conn, ana)
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    wa = FakeWhatsApp(reject_sends=True)
    client = FakeAnthropic([FakeResponse([TextBlock("Morning! Your calendar disconnected.")])])

    async with _revoking_http() as http:
        sent = await digest.send_digest(conn, client, wa, ana, pool=migrated, http=http)

    assert sent is False
    assert (await ga.get(conn, ana))["revoked_notified_at"] is None
