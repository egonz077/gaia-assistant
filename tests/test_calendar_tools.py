"""NOTE ON FIXTURES: capability tools get their own transaction via
registry.dispatch, so a user created on this connection and not committed is
invisible to them and the insert dies on a foreign key -- the same trap
documented for usage.record. These tests commit the user first.

tx() also sets row_factory on the pooled connection and that rides back into
the pool, so the connection here sets it explicitly.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import httpx
import pytest
from psycopg.rows import dict_row

from gaia.capabilities.calendar import tools
from gaia.core import crypto
from gaia.core.db import google_accounts as ga
from gaia.core.db import pending_invites as pi
from gaia.core.db import users as users_db


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


@pytest.fixture
async def committed(migrated):
    async with migrated.connection() as c:
        c.row_factory = dict_row
        user = await users_db.create_user(c, name="Ana", wa_id="13055557001",
                                          email="ana@gaiagroupdevelopment.com")
        await ga.upsert(c, user, google_email="ana@gaiagroupdevelopment.com",
                        refresh_token="1//r", scopes="s")
        await c.commit()
    async with migrated.connection() as c:
        c.row_factory = dict_row
        yield c, user


def _http(sent, response):
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        sent.append({"url": str(request.url), "method": request.method,
                     "body": request.content.decode() or "{}"})
        return httpx.Response(200, json=response)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_create_event_uses_the_users_timezone(committed):
    """The model supplies wall-clock time and nothing else. today_line() exists
    because the model called one date Friday in one turn and Thu in the next;
    it must not be trusted to attach a UTC offset across a DST boundary."""
    import json as _json
    conn, user = committed
    sent = []
    async with _http(sent, {"id": "evt-1", "hangoutLink": "https://meet.google.com/abc"}) as http:
        out = await tools.create_event(conn, user, {
            "summary": "Site walk", "date": "2026-09-16",
            "start_time": "10:00", "duration_minutes": 60, "with_meet": True,
        }, http=http)
    assert out["meet_link"] == "https://meet.google.com/abc"
    body = _json.loads(sent[0]["body"])
    # America/New_York on that date is UTC-4.
    assert body["start"]["dateTime"].endswith("-04:00")


async def test_check_availability_returns_times_only(committed):
    conn, user = committed
    events = {"items": [{"summary": "Seller call re: Aurea",
                         "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
                         "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}]}
    async with _http([], events) as http:
        out = await tools.check_availability(conn, user, {"date": "2026-09-16"}, http=http)
    assert "Aurea" not in str(out)
    assert out["busy"] == [{"from": "10:00", "to": "11:00"}]


async def test_propose_invite_reaches_nobody(committed):
    conn, user = committed
    sent = []
    async with _http(sent, {}) as http:
        out = await tools.propose_invite(
            conn, user, {"event_id": "evt-1", "emails": ["a@x.com"]}, http=http)
    assert sent == []          # no Google call at all
    assert out["pending_id"]
    assert out["emails"] == ["a@x.com"]


async def test_confirm_invite_sends_what_was_stored(committed):
    import json as _json
    conn, user = committed
    pid = await pi.create(conn, user, event_id="evt-1", emails=["stored@x.com"])
    sent = []
    async with _http(sent, {"id": "evt-1"}) as http:
        await tools.confirm_invite(conn, user, {"pending_id": str(pid)}, http=http)
    assert _json.loads(sent[0]["body"])["attendees"] == [{"email": "stored@x.com"}]


async def test_confirm_invite_schema_accepts_no_addresses():
    """The gate, as a schema assertion. If an emails field ever appears here,
    the model can confirm a list the human never saw."""
    from gaia.capabilities.calendar import CAPABILITY
    tool = next(t for t in CAPABILITY.tools if t.name == "confirm_invite")
    assert set(tool.input_schema["properties"]) == {"pending_id"}


async def test_tools_offer_a_link_when_not_connected(migrated):
    conn_user = None
    async with migrated.connection() as c:
        c.row_factory = dict_row
        conn_user = await users_db.create_user(c, name="New", wa_id="13055557002")
        await c.commit()
    async with migrated.connection() as c:
        c.row_factory = dict_row
        async with _http([], {}) as http:
            out = await tools.check_availability(c, conn_user, {"date": "2026-09-16"}, http=http)
    assert out["needs_connection"] is True


async def test_busy_times_are_shown_in_the_users_own_timezone(migrated):
    """list_events sends no timeZone parameter, so Google answers in the
    *calendar's* default zone -- which is not necessarily the developer's.
    strftime on that value reports the offset Google happened to use: a 10am
    meeting on a calendar defaulting to UTC is read back as "busy 14:00" to
    someone in New York. The interval arithmetic is fine, because aware
    datetimes compare correctly across offsets; only what she is told is
    wrong, which is what makes it easy to miss.

    Madrid here rather than the fixture's New York, so the event's -04:00 and
    the user's zone cannot agree by construction.
    """
    async with migrated.connection() as c:
        c.row_factory = dict_row
        user = await users_db.create_user(c, name="Ana", wa_id="13055557003",
                                          email="ana@gaiagroupdevelopment.com",
                                          timezone="Europe/Madrid")
        await ga.upsert(c, user, google_email="ana@gaiagroupdevelopment.com",
                        refresh_token="1//r", scopes="s")
        await c.commit()

    events = {"items": [{"summary": "Seller call",
                         "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
                         "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}]}
    async with migrated.connection() as c:
        c.row_factory = dict_row
        async with _http([], events) as http:
            out = await tools.check_availability(c, user, {"date": "2026-09-16"}, http=http)

    # 10:00-04:00 is 14:00 UTC, which is 16:00 in Madrid in September.
    assert out["busy"] == [{"from": "16:00", "to": "17:00"}]


async def test_a_failed_correlation_write_still_returns_the_event_id(committed):
    """By the time the columns are written the event exists on the calendar.
    A hallucinated lead_id -- registry.dispatch's own docstring names "a
    hallucinated uuid" as a scar this codebase already carries -- made
    set_calendar_event raise, dispatch report "tool create_event failed", and
    the model do the reasonable thing: create the event again. requestId makes
    the Meet *conference* idempotent, not the event.

    A missing link between a lead and its event is recoverable by asking. A
    second identical meeting on a client's calendar is not.
    """
    conn, user = committed
    async with _http([], {"id": "evt-1"}) as http:
        out = await tools.create_event(conn, user, {
            "summary": "Site walk", "date": "2026-09-16", "start_time": "10:00",
            "lead_id": "not-a-uuid-at-all",
        }, http=http)
    assert out["event_id"] == "evt-1"
