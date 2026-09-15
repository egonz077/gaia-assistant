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
    # propose_invite now reads the event to check it exists, so "no Google
    # call at all" is no longer the property -- and never was the point. What
    # must not happen here is a WRITE: nothing that could put a name on a
    # calendar or an email in an inbox. A GET of the user's own event reaches
    # nobody. That distinction is the approval gate, so it is asserted exactly.
    assert all(c["method"] == "GET" for c in sent), sent
    assert not any("attendees" in c["body"] or "sendUpdates" in c["url"] for c in sent), sent
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


_EVENT = {"id": "evt-1", "summary": "Site walk",
          "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
          "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}


def _http_404():
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        return httpx.Response(404, json={"error": {"message": "Not Found"}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_propose_invite_refuses_an_event_that_does_not_exist(committed):
    """Live, the model invented evt_cal_integration_test_0916 and the failure
    only surfaced at confirm time as a 404 it could do nothing with. Refusing
    here, with no pending row written, is what turns a dead end into a
    recoverable turn."""
    conn, user = committed
    async with _http_404() as http:
        out = await tools.propose_invite(
            conn, user, {"event_id": "made-up", "emails": ["a@x.com"]}, http=http)
    assert out["proposed"] is False
    assert "pending_id" not in out
    cur = await conn.execute("SELECT count(*) AS n FROM pending_invites")
    assert (await cur.fetchone())["n"] == 0


async def test_propose_invite_says_what_the_invite_is_to(committed):
    """The read-back used to name only the addresses, so the human approved
    *who* without seeing *to what*."""
    conn, user = committed
    async with _http([], _EVENT) as http:
        out = await tools.propose_invite(
            conn, user, {"event_id": "evt-1", "emails": ["a@x.com"]}, http=http)
    assert out["pending_id"]
    assert out["event"]["summary"] == "Site walk"
    assert "2026-09-16 10:00" in out["event"]["starts"]


async def test_list_pending_invites_returns_open_approvals_with_their_event(committed):
    conn, user = committed
    pid = await pi.create(conn, user, event_id="evt-1", emails=["a@x.com"])
    async with _http([], _EVENT) as http:
        out = await tools.list_pending_invites(conn, user, {}, http=http)
    assert [p["pending_id"] for p in out["pending"]] == [str(pid)]
    assert out["pending"][0]["emails"] == ["a@x.com"]
    assert out["pending"][0]["event"]["summary"] == "Site walk"


async def test_list_pending_invites_marks_an_event_that_no_longer_exists(committed):
    """A row whose event was deleted -- or never existed -- must still be
    listed, and say so, so the model can tell the user instead of confirming
    into a 404."""
    conn, user = committed
    await pi.create(conn, user, event_id="gone", emails=["a@x.com"])
    async with _http_404() as http:
        out = await tools.list_pending_invites(conn, user, {}, http=http)
    assert out["pending"][0]["event"] is None


async def test_list_pending_invites_schema_takes_nothing():
    from gaia.capabilities.calendar import CAPABILITY
    tool = next(t for t in CAPABILITY.tools if t.name == "list_pending_invites")
    assert tool.input_schema.get("required", []) == []


async def test_list_pending_invites_says_plainly_that_nothing_is_sent(committed):
    """Live, the model read an OPEN pending row as "invite already out" and
    told the user so. A row in this list is by definition unsent; the payload
    has to say it, not leave it to be inferred from the tool's name."""
    conn, user = committed
    await pi.create(conn, user, event_id="evt-1", emails=["a@x.com"])
    async with _http([], _EVENT) as http:
        out = await tools.list_pending_invites(conn, user, {}, http=http)
    assert "not sent" in out["pending"][0]["status"].lower()
    assert "none of these" in out["note"].lower()


async def test_create_event_marks_the_event_as_made_by_gaia(committed):
    """So cleanup can target what Gaia made and leave the developer's own
    entries alone. Set always -- not only when a lead or commitment is linked."""
    import json as _json
    conn, user = committed
    sent = []
    async with _http(sent, {"id": "evt-9"}) as http:
        await tools.create_event(conn, user, {
            "summary": "Test", "date": "2026-09-16", "start_time": "10:00"}, http=http)
    body = _json.loads(sent[0]["body"])
    assert body["extendedProperties"]["private"]["gaia"] == "1"


async def test_list_events_on_returns_ids_titles_and_attendees(committed):
    """cancel_event needs an id, and until this existed nothing returned one
    for an event from an earlier turn: check_availability strips ids and
    titles by design, and list_pending_invites shows only open approvals. The
    model said, truthfully, "the ids are gone on my side" -- and could not
    cancel duplicates it had itself created."""
    conn, user = committed
    events = {"items": [
        {"id": "evt-a", "summary": "Calendar Integration Test #3",
         "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
         "end": {"dateTime": "2026-09-16T10:30:00-04:00"},
         "attendees": [{"email": "ana@gaiagroupdevelopment.com", "organizer": True},
                       {"email": "x@y.com", "responseStatus": "needsAction"}],
         "extendedProperties": {"private": {"gaia": "1"}}},
        {"id": "evt-b", "summary": "Dentist",
         "start": {"dateTime": "2026-09-16T14:00:00-04:00"},
         "end": {"dateTime": "2026-09-16T15:00:00-04:00"}},
    ]}
    async with _http([], events) as http:
        out = await tools.list_events_on(conn, user, {"date": "2026-09-16"}, http=http)
    a, b = out["events"]
    assert a["event_id"] == "evt-a" and a["summary"] == "Calendar Integration Test #3"
    assert a["starts"].endswith("10:00") and a["ends"].endswith("10:30")
    assert a["attendees"] == ["x@y.com"]          # organizer excluded
    assert a["created_by_gaia"] is True
    assert b["event_id"] == "evt-b" and b["attendees"] == [] and b["created_by_gaia"] is False


async def test_list_events_on_schema_requires_a_date():
    from gaia.capabilities.calendar import CAPABILITY
    tool = next(t for t in CAPABILITY.tools if t.name == "list_events_on")
    assert tool.input_schema["required"] == ["date"]
