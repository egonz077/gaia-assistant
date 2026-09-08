from gaia.core.db import contacts as contacts_db
from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db
from gaia.core.embeddings import EMBED_DIM


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


# --- contact_name filter path ---------------------------------------------
# search() exposes contact_name as a tool argument that an LLM chooses, so
# this path needs coverage beyond "reviewed by hand and looks right".


async def test_search_filters_by_contact_name(conn, ana):
    delgado_id = await contacts_db.create_contact(conn, ana, name="Delgado")
    fields_id = await contacts_db.create_contact(conn, ana, name="Fields")
    mid = await meetings_db.save(conn, ana, summary="notes", source="text")
    await memory_db.index_meeting(conn, ana, mid, "Delgado liked the kitchen", "org", contact_id=delgado_id)
    await memory_db.index_meeting(conn, ana, mid, "Fields liked the kitchen too", "org", contact_id=fields_id)

    hits = await memory_db.search(conn, ana, "kitchen", contact_name="Delgado")

    assert len(hits) == 1
    assert hits[0]["contact"] == "Delgado"


async def test_search_by_contact_name_excludes_another_users_private_chunk(conn, ana, sofia):
    """The name filter must not bypass the visibility filter it's ANDed with."""
    carlos_id = await contacts_db.create_contact(conn, ana, name="Carlos", visibility="private")
    mid = await meetings_db.save(conn, ana, summary="notes", source="text", visibility="private")
    await memory_db.index_meeting(conn, ana, mid, "Private note about Carlos", "private", contact_id=carlos_id)

    hits = await memory_db.search(conn, sofia, "note", contact_name="Carlos")

    assert hits == []


async def test_search_by_contact_name_excludes_chunks_with_no_contact(conn, ana):
    """The LEFT JOIN effectively becomes an inner join when contact_name is
    supplied: a chunk with no linked contact never matches, even if its
    content matches the query. Intended behaviour, pinned so a future change
    to the join can't silently alter it."""
    delgado_id = await contacts_db.create_contact(conn, ana, name="Delgado")
    mid = await meetings_db.save(conn, ana, summary="notes", source="text")
    await memory_db.index_meeting(conn, ana, mid, "Delgado liked the kitchen", "org", contact_id=delgado_id)
    await memory_db.index_meeting(conn, ana, mid, "General kitchen notes, no contact link", "org")

    hits = await memory_db.search(conn, ana, "kitchen", contact_name="Delgado")

    assert len(hits) == 1
    assert hits[0]["content"] == "Delgado liked the kitchen"


# --- index_meeting's contact_id parameter ----------------------------------


async def test_index_meeting_stores_contact_id_and_search_returns_contact_name(conn, ana):
    delgado_id = await contacts_db.create_contact(conn, ana, name="Delgado")
    mid = await meetings_db.save(conn, ana, summary="notes", source="text")

    await memory_db.index_meeting(conn, ana, mid, "Kitchen chat", "org", contact_id=delgado_id)

    cur = await conn.execute("SELECT contact_id FROM memory_chunks WHERE meeting_id = %s", (mid,))
    row = await cur.fetchone()
    assert row["contact_id"] == delgado_id

    hits = await memory_db.search(conn, ana, "kitchen")
    assert hits[0]["contact"] == "Delgado"


# --- embedding dimension vs. schema -----------------------------------------


# --- meeting_id on search results -------------------------------------


async def test_search_results_carry_the_source_meeting_id(conn, ana):
    """Without an id, 'make the Tuesday showing private' is unanswerable -
    search must let the model refer back to what it found."""
    mid = await _meeting_with_chunk(conn, ana, "Showed a house on Elm")
    hits = await memory_db.search(conn, ana, "Elm")
    assert hits[0]["meeting_id"] == str(mid)


async def test_search_handles_a_chunk_with_no_meeting_id(conn, ana):
    """meeting_id is nullable in the schema (ON DELETE CASCADE clears it on
    meeting deletion), so search must not assume every chunk has one."""
    vector = [0.0] * EMBED_DIM
    await conn.execute(
        """INSERT INTO memory_chunks (user_id, visibility, content, embedding)
           VALUES (%s, 'org', %s, %s)""",
        (ana.id, "Orphaned chunk, no meeting", vector),
    )
    hits = await memory_db.search(conn, ana, "Orphaned")
    assert hits[0]["meeting_id"] is None


async def test_embedding_column_dimension_matches_embed_dim(conn):
    """fake_embed derives its vector width from EMBED_DIM, not a literal, so
    this pins EMBED_DIM itself against the live schema — closing the gap
    where changing the model's dimension would otherwise only surface as a
    Postgres insert failure in production."""
    cur = await conn.execute(
        """SELECT atttypmod FROM pg_attribute
           WHERE attrelid = 'memory_chunks'::regclass AND attname = 'embedding'"""
    )
    row = await cur.fetchone()
    assert row["atttypmod"] == EMBED_DIM
