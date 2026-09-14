from datetime import datetime, timedelta, timezone

from gaia.core.db import commitments as commitments_db
from gaia.core.db import meetings as meetings_db


async def _file(conn, user, description, *, due_in_days=None, private=False):
    due = (
        datetime.now(timezone.utc) + timedelta(days=due_in_days)
        if due_in_days is not None
        else None
    )
    await meetings_db.save(
        conn,
        user,
        summary=f"Meeting about {description}",
        source="text",
        visibility="private" if private else "org",
        commitments=[{"description": description, "due_at": due}],
    )


async def test_query_returns_commitments_beyond_the_digest_horizon(conn, ana):
    """`open_for` answers the digest's question — what is due in the next two
    mornings. `query` answers the user's — everything they still owe. A
    commitment due next month is invisible to the first and must not be
    invisible to the second, or "clear my commitments" silently skips it."""
    await _file(conn, ana, "Send the September comps", due_in_days=30)

    assert await commitments_db.open_for(conn, ana) == []
    assert [c["description"] for c in await commitments_db.query(conn, ana)] == [
        "Send the September comps"
    ]


async def test_query_returns_the_id_needed_to_close_one(conn, ana):
    """The whole point of this read. Without an id on the row the model has
    nothing to pass to `complete` and asks the user for one, which they do
    not have."""
    await _file(conn, ana, "Call the attorney")

    rows = await commitments_db.query(conn, ana)
    assert rows[0]["id"] is not None
    assert await commitments_db.complete(conn, ana, [rows[0]["id"]]) == [rows[0]["id"]]


async def test_query_omits_what_is_already_done(conn, ana):
    await _file(conn, ana, "Send comps")
    await _file(conn, ana, "Call the attorney")

    rows = await commitments_db.query(conn, ana)
    await commitments_db.complete(conn, ana, [rows[0]["id"]])

    assert len(await commitments_db.query(conn, ana)) == 1


async def test_query_is_scoped_by_ownership_not_visibility(conn, ana, sofia):
    """Sofia's org-visible commitment is readable by Ana and is not her work.
    Listing it would put a row in front of Ana that `complete` then refuses,
    which reads to her as the assistant breaking."""
    await _file(conn, sofia, "Sofia's task")

    assert await commitments_db.query(conn, ana) == []


async def test_complete_closes_several_at_once(conn, ana):
    await _file(conn, ana, "Send comps")
    await _file(conn, ana, "Call the attorney")
    ids = [c["id"] for c in await commitments_db.query(conn, ana)]

    assert sorted(await commitments_db.complete(conn, ana, ids)) == sorted(ids)
    assert await commitments_db.query(conn, ana) == []


async def test_complete_reports_which_ones_it_actually_closed(conn, ana, sofia):
    """A stale id, or one belonging to a colleague, must come back as "closed
    two of three" rather than as a silent success — the reply the user reads
    is built from this list."""
    await _file(conn, ana, "Send comps")
    await _file(conn, sofia, "Sofia's task")
    mine = [c["id"] for c in await commitments_db.query(conn, ana)]
    hers = [c["id"] for c in await commitments_db.query(conn, sofia)]

    assert await commitments_db.complete(conn, ana, mine + hers) == mine
    assert len(await commitments_db.query(conn, sofia)) == 1


async def test_complete_does_not_count_a_commitment_twice(conn, ana):
    """Already done is not newly closed. Counting it again tells the user
    they cleared work they cleared yesterday."""
    await _file(conn, ana, "Send comps")
    ids = [c["id"] for c in await commitments_db.query(conn, ana)]
    await commitments_db.complete(conn, ana, ids)

    assert await commitments_db.complete(conn, ana, ids) == []


async def test_complete_on_an_empty_list_touches_nothing(conn, ana):
    await _file(conn, ana, "Send comps")

    assert await commitments_db.complete(conn, ana, []) == []
    assert len(await commitments_db.query(conn, ana)) == 1


async def test_query_carries_the_contact_and_due_date(conn, ana):
    """What the reply needs to name an item back to the user: "send Marta the
    comps by Friday", not "commitment 3"."""
    await meetings_db.save(
        conn,
        ana,
        summary="Showed Coral Gables",
        source="text",
        contact_names=["Marta Delgado"],
        commitments=[{
            "description": "Send comps",
            "contact_name": "Marta Delgado",
            "due_at": datetime.now(timezone.utc) + timedelta(days=3),
        }],
    )

    row = (await commitments_db.query(conn, ana))[0]
    assert row["contact"] == "Marta Delgado"
    assert row["due_at"] is not None


async def test_update_changes_the_description(conn, ana):
    """The correction path. The assistant reads a commitment back, hears "no,
    that's not what I said", and needs somewhere to put the answer."""
    await _file(conn, ana, "Call Sasha")
    row = (await commitments_db.query(conn, ana))[0]

    assert await commitments_db.update(
        conn, ana, row["id"], description="Call Cesia"
    ) is True
    assert (await commitments_db.query(conn, ana))[0]["description"] == "Call Cesia"


async def test_update_changes_the_due_date(conn, ana):
    await _file(conn, ana, "Send comps", due_in_days=7)
    row = (await commitments_db.query(conn, ana))[0]
    new_due = datetime.now(timezone.utc) + timedelta(days=2)

    assert await commitments_db.update(conn, ana, row["id"], due_at=new_due) is True

    assert (await commitments_db.query(conn, ana))[0]["due_at"] == new_due


async def test_update_can_clear_a_due_date(conn, ana):
    """"Actually there's no deadline on that one" has to be expressible. A
    sentinel is needed because None already means "leave this field alone"."""
    await _file(conn, ana, "Send comps", due_in_days=7)
    row = (await commitments_db.query(conn, ana))[0]

    assert await commitments_db.update(
        conn, ana, row["id"], due_at=commitments_db.CLEAR
    ) is True

    assert (await commitments_db.query(conn, ana))[0]["due_at"] is None


async def test_update_leaves_untouched_fields_alone(conn, ana):
    """Passing one field must not blank the others."""
    await _file(conn, ana, "Send comps", due_in_days=3)
    row = (await commitments_db.query(conn, ana))[0]

    await commitments_db.update(conn, ana, row["id"], description="Send comps today")

    after = (await commitments_db.query(conn, ana))[0]
    assert after["description"] == "Send comps today"
    assert after["due_at"] == row["due_at"]


async def test_reopen_brings_a_closed_commitment_back(conn, ana):
    """"I closed the wrong one" is the most expensive correction of the set,
    and until now it was the one with no way back."""
    await _file(conn, ana, "Call the attorney")
    row = (await commitments_db.query(conn, ana))[0]
    await commitments_db.complete(conn, ana, [row["id"]])
    assert await commitments_db.query(conn, ana) == []

    assert await commitments_db.update(conn, ana, row["id"], reopen=True) is True

    assert [c["description"] for c in await commitments_db.query(conn, ana)] == [
        "Call the attorney"
    ]


async def test_update_refuses_a_colleagues_commitment(conn, ana, sofia):
    await _file(conn, sofia, "Sofia's task")
    hers = (await commitments_db.query(conn, sofia))[0]

    assert await commitments_db.update(
        conn, ana, hers["id"], description="hijacked"
    ) is False
    assert (await commitments_db.query(conn, sofia))[0]["description"] == "Sofia's task"


async def test_update_reports_false_for_an_unknown_id(conn, ana):
    """The caller must be told nothing happened rather than assuming success —
    the same contract meetings.set_visibility has."""
    from uuid import uuid4

    assert await commitments_db.update(
        conn, ana, uuid4(), description="nothing to update"
    ) is False


async def test_update_with_nothing_to_change_reports_false(conn, ana):
    """A no-op is not a success. Reporting True would let the assistant say
    "updated" when it changed nothing."""
    await _file(conn, ana, "Send comps")
    row = (await commitments_db.query(conn, ana))[0]

    assert await commitments_db.update(conn, ana, row["id"]) is False
