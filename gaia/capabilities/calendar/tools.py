"""Calendar tool handlers.

Every handler takes `http` as a keyword argument with a default, so
registry.dispatch can call it with (conn, user, args) while tests pass their
own transport. The default constructs a client; it is not a test switch.
"""

import logging
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from gaia.capabilities.calendar import client as cal
from gaia.core import google, oauth_link
from gaia.core.config import settings
from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from gaia.core.db import pending_invites as pi_db
from gaia.core.models import User

log = logging.getLogger("gaia.calendar")

CONNECT_HINT = "Ask the user to connect their Google account, then send them this link."


@asynccontextmanager
async def _client(http):
    """Yield the caller's client untouched, or make and close our own.

    `async with` on an httpx client that is already open raises "Cannot open a
    client instance more than once", so a passed-in client must never be
    re-entered -- and one we created must still be closed.
    """
    if http is not None:
        yield http
    else:
        async with httpx.AsyncClient(timeout=20) as own:
            yield own


def _needs_connection(conn, user: User) -> dict:
    """Not a hidden capability. Hiding the tools would make Gaia claim it
    cannot do calendars at all, which is false and unhelpful."""
    return {
        "needs_connection": True,
        "link": f"https://{settings.domain}/oauth/google/start?t={oauth_link.mint(user.id)}",
        "note": CONNECT_HINT,
    }


def _local(user: User, day: str, hhmm: str) -> datetime:
    """Wall clock plus the user's timezone, from the database.

    The model supplies '2026-09-16' and '10:00' and never an offset. A model
    that got Friday and Thursday confused inside one conversation must not be
    the thing deciding whether a date is inside daylight saving.
    """
    d = date_cls.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=ZoneInfo(user.timezone))


async def check_availability(conn, user: User, args: dict, *, http=None) -> dict:
    day = args["date"]
    try:
        async with _client(http) as client:
            events = await cal.list_events(
                conn, user, http=client,
                time_min=_local(user, day, "00:00"),
                time_max=_local(user, day, "00:00") + timedelta(days=1),
            )
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    # Converted before it is formatted. Google answers in the *calendar's*
    # default zone, not the developer's, so strftime on the raw value reports
    # whatever offset her calendar happens to be set to -- "busy 14:00-15:00"
    # about a 10am meeting, for a UTC-default calendar read in New York. The
    # comparisons in busy_intervals are unaffected (aware datetimes compare
    # fine across offsets), which is exactly why only the display was wrong.
    tz = ZoneInfo(user.timezone)
    return {"date": day, "busy": [
        {"from": s.astimezone(tz).strftime("%H:%M"), "to": e.astimezone(tz).strftime("%H:%M")}
        for s, e in cal.busy_intervals(events)
    ]}


async def create_event(conn, user: User, args: dict, *, http=None) -> dict:
    start = _local(user, args["date"], args["start_time"])
    end = start + timedelta(minutes=int(args.get("duration_minutes", 60)))
    extended = {}
    if args.get("lead_id"):
        extended["gaia_lead_id"] = args["lead_id"]
    if args.get("commitment_id"):
        extended["gaia_commitment_id"] = args["commitment_id"]
    try:
        async with _client(http) as client:
            ev = await cal.create_event(
                conn, user, summary=args["summary"], start=start, end=end,
                with_meet=bool(args.get("with_meet")), http=client,
                extended=extended or None,
            )
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    event_id = ev.get("id")
    # Both directions, written together. extendedProperties (set in
    # client.create_event) lets events.list find Gaia's events server-side;
    # the column is the durable half, because the calendar is not a database
    # and people delete events.
    #
    # Swallowed on purpose: the event is already on the calendar by the time
    # this runs. A hallucinated lead_id -- the scar registry.dispatch's own
    # docstring names -- made this raise, dispatch report "tool create_event
    # failed", and the model do the reasonable thing and create the meeting
    # again. requestId makes the Meet conference idempotent, not the event.
    # A lost link is recoverable by asking her; a second identical meeting on
    # a client's calendar is not.
    try:
        if args.get("lead_id"):
            await leads_db.set_calendar_event(conn, user, args["lead_id"], event_id)
        if args.get("commitment_id"):
            await commitments_db.set_calendar_event(conn, user, args["commitment_id"], event_id)
    except Exception:
        log.exception("created event %s but could not link it for user %s", event_id, user.id)
    return {"event_id": event_id,
            "meet_link": ev.get("hangoutLink"),
            "starts": start.strftime("%A %Y-%m-%d %H:%M"),
            "note": "Nobody has been invited. Use propose_invite to ask first."}


def _when(user: User, ev: dict) -> str:
    """An event's start as the user would say it. All-day events carry `date`
    rather than `dateTime`; those are shown as the date."""
    start = ev.get("start", {})
    if "dateTime" not in start:
        return start.get("date", "?")
    local = datetime.fromisoformat(start["dateTime"]).astimezone(ZoneInfo(user.timezone))
    return local.strftime("%A %Y-%m-%d %H:%M")


def _event_brief(user: User, ev: dict) -> dict:
    return {"summary": ev.get("summary"), "starts": _when(user, ev)}


async def propose_invite(conn, user: User, args: dict, *, http=None) -> dict:
    """Reaches nobody. Records the exact list so confirm_invite can send it.

    Checks the event exists first. Live, the model invented an event id --
    history is prose, so it had lost the real one at the turn boundary -- the
    row was written anyway, and the 404 surfaced at confirm time as a dead
    end. Refusing here, with no row written, leaves it a way forward:
    list_pending_invites, or create_event again.

    Returns what the invite is *to*, so the read-back can name the meeting
    and not only the addresses; approving *who* without seeing *what* was
    the gap the whole-branch review flagged.
    """
    emails = list(args["emails"])
    try:
        async with _client(http) as client:
            ev = await cal.get_event(conn, user, event_id=args["event_id"], http=client)
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    if ev is None:
        return {"proposed": False,
                "note": "No such event on the calendar. Event ids come only from "
                        "create_event or list_pending_invites -- never guess one."}
    pending_id = await pi_db.create(conn, user, event_id=args["event_id"], emails=emails)
    return {"pending_id": str(pending_id), "emails": emails,
            "event": _event_brief(user, ev),
            "note": "Read the addresses AND what they are being invited to back to "
                    "the user, and wait for a yes before confirm_invite."}


async def list_pending_invites(conn, user: User, args: dict, *, http=None) -> dict:
    """The model's way back to an approval from a previous turn.

    Each row is looked up on the calendar so the list can say what it is an
    invite to -- and say plainly when the event no longer exists, so the
    model tells the user instead of confirming into a 404.
    """
    rows = await pi_db.open_for(conn, user)
    pending = []
    try:
        async with _client(http) as client:
            for r in rows:
                ev = await cal.get_event(conn, user, event_id=r["event_id"], http=client)
                pending.append({
                    "pending_id": str(r["id"]),
                    "event_id": r["event_id"],
                    "emails": list(r["emails"]),
                    "event": _event_brief(user, ev) if ev else None,
                    "expires_at": r["expires_at"].isoformat(),
                })
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"pending": pending}


async def confirm_invite(conn, user: User, args: dict, *, http=None) -> dict:
    """Takes no addresses. That is the gate: the model cannot confirm a list
    the human was never shown, because it has nowhere to put one."""
    claimed = await pi_db.claim(conn, user, args["pending_id"])
    if claimed is None:
        return {"sent": False, "note": "That approval has expired or was already used."}
    try:
        async with _client(http) as client:
            await cal.add_attendees(conn, user, event_id=claimed["event_id"],
                                    emails=claimed["emails"], http=client)
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"sent": True, "invited": claimed["emails"]}


async def cancel_event(conn, user: User, args: dict, *, http=None) -> dict:
    try:
        async with _client(http) as client:
            await cal.delete_event(conn, user, event_id=args["event_id"], http=client)
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"cancelled": True}
