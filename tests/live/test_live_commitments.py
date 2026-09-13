"""The real model clearing real commitments, end to end.

This tier exists for this bug specifically. Asked to clear her commitments,
the assistant replied that it needed their ids — which a user never has, since
the only place she ever sees a commitment is as a sentence in her 8am digest.
Nothing hermetic could catch that: the dispatch path worked, every tool did
what it was handed, and the schemas were internally valid. What was missing
was a tool the model could reach for at all, and only a real model choosing
real tools demonstrates that.

Assertions are behavioural — which tool fired, which rows closed — never on
wording, per tests/live/test_live_agent.py.
"""

import pytest

from gaia.butler import build_system_prompt
from gaia.capabilities.base import registry
from gaia.core.db import meetings as meetings_db
from gaia.core.db.pool import tx
from gaia.core.llm import run_agent

pytestmark = pytest.mark.live

COMMITMENTS = ("Send Marta the comparable sales", "Call the closing attorney",
               "Email the Okonkwo inspection report")


class RecordingRegistry:
    """The real registry, with a note of what the model actually called.

    `run_agent` takes a registry precisely so it can be substituted; it only
    ever calls `dispatch`. Recording rather than faking keeps every tool
    doing its real work against real Postgres — the point here is *which*
    tool the model reached for, not what happens when one is stubbed.
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, pool, user, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return await self.inner.dispatch(pool, user, name, args)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


async def _seed(pool, user) -> None:
    async with tx(pool) as conn:
        await meetings_db.save(
            conn,
            user,
            summary="Showed Coral Gables and met the Okonkwos",
            source="text",
            contact_names=["Marta Delgado"],
            commitments=[
                {"description": COMMITMENTS[0], "contact_name": "Marta Delgado"},
                {"description": COMMITMENTS[1]},
                {"description": COMMITMENTS[2]},
            ],
        )


async def _open_descriptions(pool, user) -> list[str]:
    async with tx(pool) as conn:
        cur = await conn.execute(
            "SELECT description FROM commitments WHERE user_id = %s AND done_at IS NULL",
            (user.id,),
        )
        return sorted(r["description"] for r in await cur.fetchall())


async def _say(claude, pool, user, history: list[dict], text: str, spy) -> str:
    """One turn, carrying the conversation forward the way butler.py does.

    butler.handle_turn replays history from `messages` as plain role/content
    strings and hands run_agent a fresh list each turn — run_agent never
    writes conversation state back. A confirmation spans two turns, so the
    history has to be carried explicitly here or the second turn arrives with
    the model having no idea what it just offered to close.
    """
    history.append({"role": "user", "content": [{"type": "text", "text": text}]})
    async with tx(pool) as conn:
        system = await build_system_prompt(conn, user)
    reply = await run_agent(
        claude, pool, user, history, system, registry.tool_defs(user), registry=spy
    )
    history.append({"role": "assistant", "content": reply})
    return reply


async def test_the_real_model_looks_commitments_up_instead_of_asking_for_ids(
    claude, migrated, ana
):
    """The reported bug, as a test. She asked for her commitments to be
    cleared and was asked for uuids in return."""
    await _seed(migrated, ana)
    spy = RecordingRegistry(registry)

    reply = await _say(claude, migrated, ana, [], "Can you clear all my commitments?", spy)

    assert "list_commitments" in spy.names(), (
        "the model did not look the commitments up; with no way to see them the "
        f"only thing left is to ask the user for ids. Reply: {reply!r}"
    )
    assert "complete_commitments" not in spy.names(), (
        f"a bulk clear must be offered before it happens, not reported after: {reply!r}"
    )
    assert await _open_descriptions(migrated, ana) == sorted(COMMITMENTS), (
        "commitments were closed before the user agreed to it"
    )


async def test_the_real_model_clears_them_all_once_the_user_agrees(claude, migrated, ana):
    """Turn two. The preview is only worth having if "yes" then works."""
    await _seed(migrated, ana)
    spy = RecordingRegistry(registry)
    history: list[dict] = []

    await _say(claude, migrated, ana, history, "Can you clear all my commitments?", spy)
    reply = await _say(claude, migrated, ana, history, "Yes, close them all please.", spy)

    assert await _open_descriptions(migrated, ana) == [], (
        f"the user confirmed and her commitments are still open: {reply!r}"
    )


async def test_the_real_model_closes_one_commitment_named_in_plain_words(
    claude, migrated, ana
):
    """The everyday case, and the one that proves the ids never surface: she
    names a commitment the way she would to a person, and exactly that one
    closes."""
    await _seed(migrated, ana)
    spy = RecordingRegistry(registry)

    reply = await _say(
        claude, migrated, ana, [], "I just sent Marta those comps, mark that one done.", spy
    )

    assert await _open_descriptions(migrated, ana) == sorted(COMMITMENTS[1:]), (
        f"expected only Marta's comps to close: {reply!r}"
    )
