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
        "link": f"https://{settings.domain}/oauth/start?t={oauth_link.mint(user.id)}",
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
    return {"date": day, "busy": [
        {"from": s.strftime("%H:%M"), "to": e.strftime("%H:%M")}
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
    return {"event_id": ev.get("id"),
            "meet_link": ev.get("hangoutLink"),
            "starts": start.strftime("%A %Y-%m-%d %H:%M"),
            "note": "Nobody has been invited. Use propose_invite to ask first."}


async def propose_invite(conn, user: User, args: dict, *, http=None) -> dict:
    """Reaches nobody. Records the exact list so confirm_invite can send it."""
    emails = list(args["emails"])
    pending_id = await pi_db.create(conn, user, event_id=args["event_id"], emails=emails)
    return {"pending_id": str(pending_id), "emails": emails,
            "note": "Read these addresses back and wait for a yes before confirm_invite."}


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
