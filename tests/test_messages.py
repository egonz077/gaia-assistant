from gaia.core.db import messages as messages_db


async def test_thread_is_private_to_its_owner(conn, ana, sofia):
    await messages_db.log(conn, ana, "user", "Notes from the Delgado showing", "wamid.1")
    assert await messages_db.recent(conn, sofia) == []
    assert len(await messages_db.recent(conn, ana)) == 1


async def test_duplicate_wa_msg_id_is_ignored(conn, ana):
    await messages_db.log(conn, ana, "user", "hello", "wamid.1")
    assert await messages_db.seen(conn, "wamid.1") is True
    await messages_db.log(conn, ana, "user", "hello", "wamid.1")
    assert len(await messages_db.recent(conn, ana)) == 1


async def test_recent_excludes_the_current_message(conn, ana):
    """The prototype logged the inbound message then read it straight back as
    history, sending it twice — once as text without its image."""
    await messages_db.log(conn, ana, "user", "older", "wamid.1")
    await messages_db.log(conn, ana, "user", "current", "wamid.2")
    history = await messages_db.recent(conn, ana, exclude_wa_ids=["wamid.2"])
    assert [m["content"] for m in history] == ["older"]


async def test_recent_returns_oldest_first(conn, ana):
    await messages_db.log(conn, ana, "user", "first", "wamid.1")
    await messages_db.log(conn, ana, "assistant", "second", None)
    assert [m["content"] for m in await messages_db.recent(conn, ana)] == ["first", "second"]


async def test_recent_exclusion_does_not_drop_assistant_turns(conn, ana):
    """Assistant rows have wa_msg_id = NULL. In SQL, NULL = ANY(array) is NULL,
    and NOT NULL is NULL (not true) — a naive exclusion predicate silently
    drops every assistant turn whenever exclude_wa_ids is non-empty, which is
    every real turn in production. The bot would have no memory of anything
    it had said. This test pins the fix."""
    await messages_db.log(conn, ana, "user", "first user turn", "wamid.1")
    await messages_db.log(conn, ana, "assistant", "assistant reply", None)
    await messages_db.log(conn, ana, "user", "second user turn", "wamid.2")

    history = await messages_db.recent(conn, ana, exclude_wa_ids=["wamid.2"])
    assert [m["content"] for m in history] == ["first user turn", "assistant reply"]
