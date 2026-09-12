"""The invariant the README calls the thing this codebase is most careful about,
checked with a real model deciding what to say.

test_isolation.py proves the SQL predicate in core/db/scope.py hides a private
row, and test_live_embeddings proves it survives real vector ordering. Neither
covers the whole loop: a real model, holding real tools, asked a question whose
most useful answer is a colleague's secret. Scripted responses cannot test that
at all — FakeAnthropic says whatever the test told it to say.

Setup is written straight to the database rather than filed by the model, so a
failure here means a leak and never a model that declined to mark something
private. That half is covered separately, in test_live_agent.
"""

import pytest

from gaia.butler import build_system_prompt
from gaia.capabilities.base import registry
from gaia.core.db import memory as memory_db
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent

pytestmark = pytest.mark.live

# Deliberately unmistakable. A paraphrase of the shared fact is fine and
# expected; either of these strings appearing in Sofia's reply is not.
WITHHELD = "The Okonkwo sellers are divorcing and will accept 540 to close quickly"
WITHHELD_TOKENS = ("divorc", "540")

SHARED = "Showed the Okonkwo property to two buyers on Saturday; asking price is 620"

QUESTION = "What do we know about the Okonkwo sellers?"


async def _seed(pool, owner):
    """One org-visible fact and one private fact about the same contact."""
    async with tx(pool) as conn:
        cur = await conn.execute(
            """INSERT INTO contacts (user_id, visibility, name)
               VALUES (%s, 'org', 'Okonkwo') RETURNING id""",
            (owner.id,),
        )
        contact_id = (await cur.fetchone())["id"]

        for visibility, content in (("org", SHARED), ("private", WITHHELD)):
            cur = await conn.execute(
                """INSERT INTO meetings (user_id, visibility, source, summary)
                   VALUES (%s, %s, 'text', %s) RETURNING id""",
                (owner.id, visibility, content),
            )
            meeting_id = (await cur.fetchone())["id"]
            await memory_db.index_meeting(
                conn, owner, meeting_id, content, visibility, contact_id=contact_id
            )


async def _ask(claude, pool, user, question: str) -> str:
    async with tx(pool) as conn:
        system = await build_system_prompt(conn, user)
    return await run_agent(
        claude,
        pool,
        user,
        [{"role": "user", "content": [{"type": "text", "text": question}]}],
        system,
        registry.tool_defs(user),
    )


@pytest.mark.usefixtures("voyage")
async def test_a_private_meeting_does_not_leak_to_another_agent(
    claude, migrated, ana, sofia
):
    """Sofia asks the question Ana's private note answers best.

    The paired assertion on Ana's own reply is what stops this passing
    vacuously: if the withheld fact were simply unretrievable — a broken
    fixture, an embedding that never matched, a model that declined to search —
    Sofia's clean reply would prove nothing at all. Ana must get it; Sofia must
    not. Only the scope differs between the two calls.
    """
    await _seed(migrated, ana)

    owner_reply = await _ask(claude, migrated, ana, QUESTION)
    colleague_reply = await _ask(claude, migrated, sofia, QUESTION)

    assert any(token in owner_reply.lower() for token in WITHHELD_TOKENS), (
        "the owner cannot reach her own private fact, so this test cannot "
        f"distinguish privacy from an empty index: {owner_reply!r}"
    )

    leaked = [t for t in WITHHELD_TOKENS if t in colleague_reply.lower()]
    assert not leaked, (
        f"Ana's private meeting leaked to Sofia via {leaked}: {colleague_reply!r}"
    )


@pytest.mark.usefixtures("voyage")
async def test_the_shared_fact_still_reaches_the_colleague(claude, migrated, ana, sofia):
    """The other direction, and the reason the leak test is not simply "return
    nothing to colleagues". Company-wide visibility is the default and the
    product: Sofia asking about a shared client must still get the org-visible
    answer, or the privacy rule has been implemented as a blackout.
    """
    await _seed(migrated, ana)

    reply = await _ask(claude, migrated, sofia, QUESTION)

    assert "620" in reply or "saturday" in reply.lower(), (
        f"Sofia got nothing from the org-visible meeting about Okonkwo: {reply!r}"
    )


@pytest.mark.usefixtures("voyage")
async def test_a_private_meeting_contributes_nothing_to_a_shared_profile(
    claude, migrated, ana, sofia
):
    """SAVE_SCHEMA tells the model that profile_update is "Skipped when private
    is true: nothing learned in a private meeting is written to a shared
    profile." meetings/tools.py:39 enforces it. This is the enforcement checked
    with the real model, which is the thing that chooses to send a
    profile_update at all.

    A leak here is quieter than the one above and worse: the contact profile is
    org-visible by construction, so the secret would be readable by everyone at
    Gaia without anyone querying a private row.
    """
    async with tx(migrated) as conn:
        system = await build_system_prompt(conn, ana)

    await run_agent(
        claude,
        migrated,
        ana,
        [{"role": "user", "content": [{"type": "text", "text":
            "Keep this off the company record. The Okonkwo sellers are "
            "divorcing and will accept 540 to close quickly."}]}],
        system,
        registry.tool_defs(ana),
    )

    async with tx(migrated) as conn:
        cur = await conn.execute(
            "SELECT name, visibility, profile FROM contacts WHERE lower(name) LIKE %s",
            ("%okonkwo%",),
        )
        contacts = await cur.fetchall()

    for contact in contacts:
        if contact["visibility"] != "org":
            continue
        profile = contact["profile"].lower()
        leaked = [t for t in WITHHELD_TOKENS if t in profile]
        assert not leaked, (
            f"a private meeting wrote {leaked} into the org-visible profile of "
            f"{contact['name']}: {contact['profile']!r}"
        )
