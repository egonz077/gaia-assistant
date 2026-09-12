"""Real Voyage calls — the half of semantic memory the hermetic suite cannot reach.

tests/conftest.py's fake_embed derives every vector from string length, which
is the right call for tests asserting on scoping: it keeps them fast, free and
deterministic. The cost is that similarity is meaningless by construction
there, so nothing checks that retrieval actually retrieves.
"""

import pytest

from gaia.core.db import memory as memory_db
from gaia.core.db.pool import tx
from gaia.core.embeddings import EMBED_DIM, MODEL, embed
from tests.factories import make_row

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("voyage")]


async def test_the_real_model_returns_the_declared_dimension():
    """Closes the gap gaia/core/embeddings.py:11 documents in prose.

    A hermetic test pins EMBED_DIM against the vector(N) column, but as that
    comment says, it "cannot catch MODEL being pointed at a Voyage model whose
    real output width differs, since verifying that would require calling the
    real API". Without this, such a change surfaces as every memory_chunks
    insert failing in production and nowhere earlier.
    """
    vectors = await embed(["Delgado wants to close by November"])

    assert len(vectors) == 1
    assert len(vectors[0]) == EMBED_DIM, (
        f"{MODEL} returned {len(vectors[0])} dimensions; EMBED_DIM says "
        f"{EMBED_DIM} and memory_chunks.embedding is vector({EMBED_DIM})"
    )


async def test_a_query_embedding_has_the_same_width_as_a_document_embedding():
    """search() embeds with input_type="query" while index_meeting() uses
    "document". Both land in the same <=> comparison, so a width difference
    between the two would be a Postgres error at search time only.
    """
    document = await embed(["The kitchen was renovated"], input_type="document")
    query = await embed(["what about the kitchen?"], input_type="query")

    assert len(document[0]) == len(query[0]) == EMBED_DIM


async def test_semantic_search_ranks_the_relevant_chunk_first(migrated, ana):
    """Real embeddings through real pgvector distance ordering.

    Two chunks on the same meeting, one about money and one about the kitchen,
    retrieved by a question about money. Under fake_embed the winner would be
    whichever content string happened to have the right length modulo 7.
    """
    async with tx(migrated) as conn:
        meeting_id = await make_row(conn, "meetings", ana)
        await memory_db.index_meeting(
            conn, ana, meeting_id,
            "Marta Delgado's budget tops out at 600k and she needs three bedrooms",
            "org",
        )
        await memory_db.index_meeting(
            conn, ana, meeting_id,
            "The Coral Gables kitchen was renovated last year with granite counters",
            "org",
        )

    async with tx(migrated) as conn:
        hits = await memory_db.search(conn, ana, "how much can Marta afford?")

    assert hits, "real semantic search returned nothing at all"
    assert "600k" in hits[0]["content"], (
        "a question about budget retrieved the kitchen chunk first: "
        f"{hits[0]['content']!r}"
    )


async def test_search_still_honours_visibility_with_real_vectors(migrated, ana, sofia):
    """The scoping guarantee, re-checked with real embeddings.

    test_isolation.py proves the predicate in core/db/scope.py hides a private
    row, using fake vectors. This proves the ordering clause added by search()
    cannot smuggle one past it — a private chunk that is the single closest
    match to Sofia's query is the hardest case for that predicate, and it is
    only constructible with real embeddings.
    """
    async with tx(migrated) as conn:
        meeting_id = await make_row(conn, "meetings", ana, visibility="private")
        await memory_db.index_meeting(
            conn, ana, meeting_id,
            "Marta Delgado's budget tops out at 600k and she needs three bedrooms",
            "private",
        )

    async with tx(migrated) as conn:
        mine = await memory_db.search(conn, ana, "how much can Marta afford?")
        theirs = await memory_db.search(conn, sofia, "how much can Marta afford?")

    assert any("600k" in h["content"] for h in mine), (
        "Ana cannot retrieve her own private chunk — the test proves nothing "
        "about Sofia if the chunk is simply unreachable"
    )
    assert not any("600k" in h["content"] for h in theirs), (
        "Sofia retrieved Ana's private chunk as the nearest neighbour"
    )
