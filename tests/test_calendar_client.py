from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.capabilities.calendar import client as cal
from gaia.core import crypto
from gaia.core.db import google_accounts as ga

TZ = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


@pytest.fixture
async def connected(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    return ana


def _capture(sent, response=None):
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        sent.append({"url": str(request.url), "method": request.method,
                     "body": request.content.decode() or "{}"})
        return httpx.Response(200, json=response or {"id": "evt-1"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_create_event_never_sends_attendees(conn, connected):
    """TRIPWIRE. An event created with an attendee and sendUpdates omitted was
    measured landing on an external attendee's calendar, Meet link and all,
    before any email existed -- sendUpdates suppresses the notification, not
    the intrusion. So attendees may only ever appear via add_attendees, which
    runs downstream of a human approving the list.

    EXEMPT: add_attendees. That call is the one place attendees are supposed
    to appear, and generalising this test to 'no Calendar call may send
    attendees' would block the feature it exists to protect.
    """
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.create_event(
            conn, connected, summary="Site walk",
            start=datetime(2026, 9, 16, 10, tzinfo=TZ),
            end=datetime(2026, 9, 16, 11, tzinfo=TZ),
            with_meet=True, http=http,
        )
    assert "attendees" not in _json.loads(sent[0]["body"])


async def test_create_event_asks_for_a_meet_link(conn, connected):
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.create_event(
            conn, connected, summary="Site walk",
            start=datetime(2026, 9, 16, 10, tzinfo=TZ),
            end=datetime(2026, 9, 16, 11, tzinfo=TZ),
            with_meet=True, http=http,
        )
    body = _json.loads(sent[0]["body"])
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    # Without conferenceDataVersion=1 the whole block is ignored -- silently,
    # with no error and no Meet link.
    assert "conferenceDataVersion=1" in sent[0]["url"]


async def test_add_attendees_announces_them(conn, connected):
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.add_attendees(conn, connected, event_id="evt-1",
                                emails=["x@y.com"], http=http)
    assert sent[0]["method"] == "PATCH"
    assert "sendUpdates=all" in sent[0]["url"]
    assert _json.loads(sent[0]["body"])["attendees"] == [{"email": "x@y.com"}]


async def test_list_events_expands_recurrences(conn, connected):
    """Without singleEvents=true a weekly standup is one row that answers
    nothing about whether Tuesday is free."""
    sent = []
    async with _capture(sent, response={"items": []}) as http:
        await cal.list_events(conn, connected,
                              time_min=datetime(2026, 9, 16, tzinfo=TZ),
                              time_max=datetime(2026, 9, 17, tzinfo=TZ), http=http)
    assert "singleEvents=true" in sent[0]["url"]


def test_busy_intervals_drops_everything_but_times():
    """check_availability answers 'is 10am free'. Meeting titles are not part
    of that answer and there is no reason to spend the model's context on them."""
    events = [{"summary": "Seller call re: Aurea",
               "attendees": [{"email": "client@example.com"}],
               "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
               "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}]
    out = cal.busy_intervals(events)
    assert len(out) == 1
    assert "Aurea" not in repr(out)


def test_busy_intervals_skips_all_day_events():
    """An all-day event carries `date`, not `dateTime`, and blocking the whole
    day on someone's birthday would make every day look full."""
    assert cal.busy_intervals([{"start": {"date": "2026-09-16"},
                                "end": {"date": "2026-09-17"}}]) == []
