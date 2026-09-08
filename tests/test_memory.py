from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db


async def _meeting_with_chunk(conn, user, summary, visibility="org"):
    mid = await meetings_db.save(conn, user, summary=summary, source="text", visibility=visibility)
    await memory_db.index_meeting(conn, user, mid, summary, visibility)
    return mid


async def test_search_finds_an_org_chunk_from_another_user(conn, ana, sofia):
    await _meeting_with_chunk(conn, ana, "Delgados liked the kitchen")
    hits = await memory_db.search(conn, sofia, "kitchen")
    assert any("kitchen" in h["content"] for h in hits)


async def test_search_excludes_another_users_private_chunk(conn, ana, sofia):
    await _meeting_with_chunk(conn, ana, "Divorce sale, motivated", visibility="private")
    hits = await memory_db.search(conn, sofia, "divorce")
    assert hits == []


async def test_owner_can_search_their_own_private_chunk(conn, ana):
    await _meeting_with_chunk(conn, ana, "Divorce sale, motivated", visibility="private")
    hits = await memory_db.search(conn, ana, "divorce")
    assert len(hits) == 1


async def test_flipping_a_meeting_private_removes_it_from_others_search(conn, ana, sofia):
    mid = await _meeting_with_chunk(conn, ana, "Routine showing notes")
    assert await memory_db.search(conn, sofia, "showing") != []

    await meetings_db.set_visibility(conn, ana, mid, "private")
    assert await memory_db.search(conn, sofia, "showing") == []
