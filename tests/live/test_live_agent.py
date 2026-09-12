"""The real model driving the real tools against real Postgres.

FakeAnthropic returns scripted tool_use blocks, so the hermetic suite proves
the dispatch path handles whatever it is handed. What it cannot prove is that
the schemas in gaia/capabilities/*/__init__.py are good enough for a real model
to fill in correctly — a required field it cannot infer, or a description that
misleads it, is invisible until a real call.

Assertions are behavioural, never string-exact: which tool fired, which row
landed. Anything asserting on exact wording would be a flake generator.
"""

import pytest

from gaia.butler import build_system_prompt
from gaia.capabilities.base import registry
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent

pytestmark = pytest.mark.live

NOTE = (
    "Met Marta Delgado at the Coral Gables listing this morning. She's buying, "
    "budget around 600k, needs three bedrooms. I promised to send her "
    "comparable sales by Friday."
)


async def _run(claude, pool, user, text: str) -> str:
    async with tx(pool) as conn:
        system = await build_system_prompt(conn, user)
    return await run_agent(
        claude,
        pool,
        user,
        [{"role": "user", "content": [{"type": "text", "text": text}]}],
        system,
        registry.tool_defs(user),
    )


async def test_the_real_model_files_a_meeting_from_a_note(claude, migrated, ana):
    """One turn, real tool schemas, and the rows that should exist afterwards.

    Also the first real exercise of the prompt-caching block layout in
    llm.py:_system_blocks — FakeAnthropic records cache_control without
    validating it, so an illegal breakpoint arrangement would only surface as a
    400 from the real API.
    """
    reply = await _run(claude, migrated, ana, NOTE)

    assert reply, "run_agent must never return empty; WhatsApp rejects an empty body"

    async with tx(migrated) as conn:
        cur = await conn.execute(
            "SELECT id, summary, visibility FROM meetings WHERE user_id = %s",
            (ana.id,),
        )
        meetings = await cur.fetchall()
        cur = await conn.execute(
            "SELECT name FROM contacts WHERE user_id = %s", (ana.id,)
        )
        contacts = [r["name"] for r in await cur.fetchall()]

    assert len(meetings) == 1, f"expected exactly one filed meeting, got {len(meetings)}"
    assert meetings[0]["visibility"] == "org", (
        "a note with no privacy request must default to org-visible"
    )
    assert any("delgado" in name.lower() for name in contacts), (
        f"the model did not file Marta Delgado as a contact: {contacts}"
    )


async def test_the_real_model_records_the_commitment_in_the_note(claude, migrated, ana):
    """"I promised to send her comparable sales by Friday" is the part that has
    to reach the digest. A meeting filed without it reads correct to the user
    and then silently never nudges her.
    """
    await _run(claude, migrated, ana, NOTE)

    async with tx(migrated) as conn:
        cur = await conn.execute(
            "SELECT description, due_at FROM commitments WHERE user_id = %s",
            (ana.id,),
        )
        commitments = await cur.fetchall()

    assert commitments, "the promise in the note was not recorded as a commitment"
    assert any(
        "comp" in c["description"].lower() or "sale" in c["description"].lower()
        for c in commitments
    ), f"no commitment resembles the one in the note: {commitments}"


async def test_the_real_model_marks_a_meeting_private_when_asked(claude, migrated, ana):
    """The `private` flag on SAVE_SCHEMA is the user-facing half of the
    visibility rule, and it is the model that has to set it. A schema
    description the model reads as optional advice rather than an instruction
    produces an org-visible row for a note that explicitly asked otherwise —
    the exact failure the README calls worse than no privacy.
    """
    await _run(
        claude,
        migrated,
        ana,
        "Keep this one off the company record please. Sat down with the Okonkwo "
        "sellers — they are divorcing and will take 540 if it moves quickly.",
    )

    async with tx(migrated) as conn:
        cur = await conn.execute(
            "SELECT visibility, summary FROM meetings WHERE user_id = %s", (ana.id,)
        )
        meetings = await cur.fetchall()

    assert len(meetings) == 1, f"expected exactly one filed meeting, got {len(meetings)}"
    assert meetings[0]["visibility"] == "private", (
        "the model filed an explicitly off-the-record note as org-visible: "
        f"{meetings[0]['summary']!r}"
    )


async def test_the_real_model_answers_from_memory_it_filed_earlier(claude, migrated, ana):
    """Two turns: file, then ask. Covers the retrieval tools end to end —
    real embeddings written by save_meeting, real pgvector search behind
    search_memory, real model deciding to call it.
    """
    await _run(claude, migrated, ana, NOTE)
    reply = await _run(claude, migrated, ana, "What was Marta's budget again?")

    assert "600" in reply, (
        f"the model could not recover a fact it filed one turn earlier: {reply!r}"
    )
