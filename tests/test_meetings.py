from datetime import datetime, timedelta, timezone

from gaia.core.db import commitments as commitments_db
from gaia.core.db import meetings as meetings_db


async def test_save_creates_contacts_and_commitments(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Showed Coral Gables to the Delgados",
        source="text",
        contact_names=["Maria Delgado"],
        commitments=[{"description": "Send comps", "contact_name": "Maria Delgado"}],
    )
    assert mid is not None
    open_items = await commitments_db.open_for(conn, ana, within_days=365)
    assert [c["description"] for c in open_items] == ["Send comps"]


async def test_happened_at_can_differ_from_now(conn, ana):
    """Notes photographed the next morning file under the day the meeting
    happened, not the day they were typed up.

    Compared as an instant, not as a rendered date. `happened_at` is
    `timestamptz` and comes back in the *session's* timezone, which is
    America/New_York on the compose `db` service, while `yesterday` is built
    in UTC — so a `.date()`-to-`.date()` comparison disagreed with itself
    every night between UTC midnight and New York midnight. That is a real
    four-hour window in which this failed for no reason at all.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    mid = await meetings_db.save(
        conn, ana, summary="Yesterday's showing", source="photo_notes", happened_at=yesterday
    )
    cur = await conn.execute("SELECT happened_at FROM meetings WHERE id = %s", (mid,))
    stored = (await cur.fetchone())["happened_at"]

    assert stored == yesterday
    assert stored.astimezone(timezone.utc).date() == yesterday.date()
    assert stored < datetime.now(timezone.utc) - timedelta(hours=23)


async def test_derived_rows_inherit_visibility_on_insert(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Sensitive divorce sale",
        source="text",
        visibility="private",
        commitments=[{"description": "Call the attorney"}],
    )
    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (mid,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_flipping_a_meeting_to_private_cascades(conn, ana):
    mid = await meetings_db.save(
        conn,
        ana,
        summary="Routine showing",
        source="text",
        commitments=[{"description": "Send comps"}],
    )
    assert await meetings_db.set_visibility(conn, ana, mid, "private") is True

    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (mid,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_set_visibility_reports_false_when_nothing_matched(conn, ana, sofia):
    """Wrong id, or someone else's meeting: the caller must be told nothing
    happened rather than assuming success."""
    mid = await meetings_db.save(conn, sofia, summary="Sofia's meeting", source="text")
    assert await meetings_db.set_visibility(conn, ana, mid, "private") is False

    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (mid,))
    assert (await cur.fetchone())["visibility"] == "org"


async def test_open_commitments_are_scoped_by_ownership_not_visibility(conn, ana, sofia):
    """Sofia's org-visible commitment is readable by Ana, but is not her work."""
    await meetings_db.save(
        conn, sofia, summary="Sofia's meeting", source="text",
        commitments=[{"description": "Sofia's task"}],
    )
    assert await commitments_db.open_for(conn, ana, within_days=365) == []
