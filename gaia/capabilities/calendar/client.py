"""Calendar REST calls. The only place an event payload is built.

Kept separate from tools.py so the tripwire test has one surface to assert on:
if every outbound create goes through create_event(), then proving create_event
never sends attendees proves it for the whole feature.
"""

import urllib.parse
from datetime import datetime

from gaia.core import google
from gaia.core.models import User

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _path(event_id: str) -> str:
    """safe="" so slashes are escaped too.

    The id arrives from the model, which read it off a photographed note or a
    transcribed voice message as readily as off a tool result. httpx
    normalises `..` segments, so an unescaped id does not stay in the path
    segment it was written into: "../../calendars/someone/events/x" addressed
    a different calendar entirely. calendar.events.owned bounds what that can
    reach to calendars this developer already owns, so it was never a route to
    a colleague's client -- but this module is where third-party text meets a
    URL, and a boundary that holds only because of a scope is one failed
    assumption from not holding.
    """
    return urllib.parse.quote(event_id, safe="")


async def create_event(conn, user: User, *, summary: str, start: datetime,
                       end: datetime, with_meet: bool, http, extended: dict | None = None) -> dict:
    """Creates the event BARE -- no attendees, ever.

    An event created with an attendee and sendUpdates omitted was measured
    already sitting on that attendee's calendar, Meet link and all, before any
    email was sent. sendUpdates suppresses the notification, not the intrusion.
    So a freshly created event is purely this developer's own calendar entry,
    invisible to anyone else, and attendees arrive only via add_attendees().
    """
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if extended:
        body["extendedProperties"] = {"private": extended}
    params = {}
    if with_meet:
        body["conferenceData"] = {"createRequest": {
            # Derived from the slot, so a retry after a timeout does not mint a
            # second conference for the same meeting.
            "requestId": f"gaia-{user.id}-{int(start.timestamp())}",
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
        # Without this the conferenceData block is ignored silently.
        params["conferenceDataVersion"] = "1"
    url = f"{BASE}?{urllib.parse.urlencode(params)}" if params else BASE
    return await google.request(conn, user, "POST", url, http=http, json=body)


async def add_attendees(conn, user: User, *, event_id: str, emails: list[str], http) -> dict:
    """The one call in this feature that reaches a third party. It runs only
    from confirm_invite, downstream of a human who saw the address list."""
    params = urllib.parse.urlencode({"conferenceDataVersion": "1", "sendUpdates": "all"})
    return await google.request(
        conn, user, "PATCH", f"{BASE}/{_path(event_id)}?{params}",
        http=http, json={"attendees": [{"email": e} for e in emails]},
    )


async def get_event(conn, user: User, *, event_id: str, http) -> dict | None:
    """None for a missing event, rather than an exception.

    The model invented an event id once, live -- it had lost the real one at
    the turn boundary -- and the 404 surfaced two calls later at confirm time
    with nothing it could act on. Answering "does this exist" at the point of
    asking is what makes that a recoverable turn instead of a dead end.
    """
    try:
        ev = await google.request(conn, user, "GET", f"{BASE}/{_path(event_id)}", http=http)
    except google.GoogleAPIError as e:
        if e.status == 404:
            return None
        raise
    # Google soft-deletes. A deleted event drops out of events.list but
    # events.get still returns it, status "cancelled", attendees and all --
    # live, a deleted #6 came back looking exactly like a real one. To every
    # caller here, deleted means gone.
    if ev.get("status") == "cancelled":
        return None
    return ev


async def list_events(conn, user: User, *, time_min: datetime, time_max: datetime, http) -> list[dict]:
    params = urllib.parse.urlencode({
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        # A weekly standup is otherwise one row that says nothing about Tuesday.
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    return (await google.request(conn, user, "GET", f"{BASE}?{params}", http=http)).get("items", [])


async def delete_event(conn, user: User, *, event_id: str, http) -> None:
    await google.request(
        conn, user, "DELETE", f"{BASE}/{_path(event_id)}?sendUpdates=all", http=http
    )


def busy_intervals(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Times only. Summaries and attendees are dropped here rather than at the
    tool boundary, so nothing downstream has to remember to do it."""
    out = []
    for ev in events:
        start, end = ev.get("start", {}), ev.get("end", {})
        # All-day events carry `date`, not `dateTime`. Treating one as a busy
        # block would make every birthday look like a full day.
        if "dateTime" not in start or "dateTime" not in end:
            continue
        if ev.get("transparency") == "transparent":
            continue  # marked "free" by the developer; not a conflict
        out.append((datetime.fromisoformat(start["dateTime"]),
                    datetime.fromisoformat(end["dateTime"])))
    return sorted(out)
