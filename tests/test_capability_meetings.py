from gaia.capabilities.meetings import (
    CAPABILITY,
    lookup_contact,
    save_meeting,
    search_memory,
    set_meeting_visibility,
)


async def test_save_meeting_files_everything(conn, ana):
    result = await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables to the Delgados",
        "contacts": [{"name": "Maria Delgado", "profile_update": "wants a pool"}],
        "commitments": [{"description": "Send comps", "contact_name": "Maria Delgado"}],
    })
    assert result["saved"] is True

    hits = await search_memory(conn, ana, {"query": "Coral Gables"})
    assert any("Coral Gables" in h["content"] for h in hits["results"])


async def test_save_meeting_respects_private(conn, ana, sofia):
    await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera"}],
        "private": True,
    })
    assert (await search_memory(conn, sofia, {"query": "divorce"}))["results"] == []
    assert (await search_memory(conn, ana, {"query": "divorce"}))["results"] != []


CONFIDENCE = "divorcing, must sell by Dec, will take 540 if pushed"


async def test_a_private_meetings_profile_update_never_reaches_the_company(conn, ana, sofia):
    """The failure `private` exists to prevent, one table over.

    `contacts.profile` is a single org-visible string on a row `get_or_create`
    hands back across users, so there is nowhere on a shared contact to put a
    fact learned in confidence. Merging one there published it verbatim to
    every agent at Gaia through lookup_contact, while the meeting itself was
    correctly hidden — hidden from the meetings list and fully readable by
    everyone, which looks private and is not.
    """
    result = await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera", "profile_update": CONFIDENCE}],
        "private": True,
    })

    # --- Sofia reaches it by no path at all.
    sofia_view = await lookup_contact(conn, sofia, {"name": "Rivera"})
    assert CONFIDENCE not in str(sofia_view)
    assert (await search_memory(conn, sofia, {"query": "divorce sale"}))["results"] == []
    # Not merely filtered out of her reads — never written to a shared row.
    cur = await conn.execute("SELECT profile FROM contacts WHERE name = 'Rivera'")
    assert [r["profile"] for r in await cur.fetchall()] == [""]

    # --- Ana does not get it on the shared profile either...
    ana_view = await lookup_contact(conn, ana, {"name": "Rivera"})
    assert CONFIDENCE not in str(ana_view)

    # ...but she can still get it back, which is the half that makes the tool's
    # own message true. Telling her a client confidence was kept somewhere it
    # does not exist would be worse than dropping it visibly: a visible drop
    # lets her retype it.
    hits = await search_memory(conn, ana, {"query": "divorce sale"})
    assert any(CONFIDENCE in h["content"] for h in hits["results"])

    # And it is durable in the meeting itself, not only in the index.
    cur = await conn.execute(
        "SELECT raw_input, visibility FROM meetings WHERE id = %s", (result["meeting_id"],)
    )
    meeting = await cur.fetchone()
    assert CONFIDENCE in meeting["raw_input"]
    assert meeting["visibility"] == "private"

    # --- The model is told, and every claim in what it is told holds above.
    assert result["profile_updates_skipped"] == ["Rivera"]
    assert "search_memory will find it" in result["note"]


async def test_restoring_a_private_meeting_publishes_its_withheld_notes(conn, ana, sofia):
    """Deliberate, and the reason it is pinned here rather than left implicit.

    A private meeting's withheld profile updates live in that meeting's own
    chunk, so putting the meeting back on the record publishes them along with
    the rest of its notes. That is the promise set_meeting_visibility already
    made, applied to all of the meeting's content rather than some of it, and
    it only ever happens because the owner explicitly asked for it.
    """
    filed = await save_meeting(conn, ana, {
        "summary": "Quiet divorce sale",
        "contacts": [{"name": "Rivera", "profile_update": CONFIDENCE}],
        "private": True,
    })
    assert (await search_memory(conn, sofia, {"query": "divorce sale"}))["results"] == []

    await set_meeting_visibility(conn, ana, {"meeting_id": filed["meeting_id"], "private": False})

    hits = await search_memory(conn, sofia, {"query": "divorce sale"})
    assert any(CONFIDENCE in h["content"] for h in hits["results"])


async def test_an_org_meetings_profile_update_still_merges(conn, ana, sofia):
    """The gate is on privacy, not on profile_update as such — a normal
    meeting must still build the shared profile, or the company's contact
    book stops growing."""
    result = await save_meeting(conn, ana, {
        "summary": "Showed the Coral Gables place",
        "contacts": [{"name": "Rivera", "profile_update": "wants a pool"}],
    })

    assert "profile_updates_skipped" not in result
    colleague_view = await lookup_contact(conn, sofia, {"name": "Rivera"})
    assert "wants a pool" in colleague_view["contact"]["profile"]


def test_capability_is_public():
    assert CAPABILITY.allowed_roles is None
    assert CAPABILITY.allowed_user_ids is None
    assert {t.name for t in CAPABILITY.tools} == {
        "save_meeting", "search_memory", "lookup_contact", "set_meeting_visibility"
    }


async def test_lookup_contact_finds_an_existing_contact(conn, ana):
    await save_meeting(conn, ana, {
        "summary": "Met with a buyer",
        "contacts": [{"name": "Jorge", "profile_update": "wants waterfront"}],
    })

    result = await lookup_contact(conn, ana, {"name": "Jorge"})

    assert result["contact"]["name"] == "Jorge"
    assert "waterfront" in result["contact"]["profile"]


async def test_lookup_contact_reports_a_missing_contact(conn, ana):
    result = await lookup_contact(conn, ana, {"name": "Nobody"})
    assert result == {"contact": None, "note": "no such contact"}


async def test_set_meeting_visibility_cascades_through_the_tool(conn, ana):
    filed = await save_meeting(conn, ana, {
        "summary": "Routine showing",
        "contacts": [{"name": "Buyer"}],
        "commitments": [{"description": "Follow up", "contact_name": "Buyer"}],
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, ana, {"meeting_id": meeting_id, "private": True})
    assert outcome == {"changed": True, "visibility": "private"}

    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "private"

    cur = await conn.execute(
        "SELECT visibility FROM commitments WHERE meeting_id = %s", (meeting_id,)
    )
    assert (await cur.fetchone())["visibility"] == "private"

    cur = await conn.execute(
        "SELECT visibility FROM memory_chunks WHERE meeting_id = %s", (meeting_id,)
    )
    assert (await cur.fetchone())["visibility"] == "private"


async def test_set_meeting_visibility_colleague_cannot_reclassify(conn, ana, sofia):
    filed = await save_meeting(conn, ana, {
        "summary": "Ana's meeting",
        "contacts": [{"name": "Buyer"}],
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, sofia, {"meeting_id": meeting_id, "private": True})

    assert outcome == {"changed": False, "note": "no such meeting, or it belongs to someone else"}
    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "org"


async def test_set_meeting_visibility_can_restore_to_org(conn, ana):
    filed = await save_meeting(conn, ana, {
        "summary": "Sensitive meeting",
        "contacts": [{"name": "Buyer"}],
        "private": True,
    })
    meeting_id = filed["meeting_id"]

    outcome = await set_meeting_visibility(conn, ana, {"meeting_id": meeting_id, "private": False})

    assert outcome == {"changed": True, "visibility": "org"}
    cur = await conn.execute("SELECT visibility FROM meetings WHERE id = %s", (meeting_id,))
    assert (await cur.fetchone())["visibility"] == "org"


async def test_a_dictated_meeting_is_filed_as_a_voice_note(conn, ana):
    """Without this it lands as photo_notes: source was derived from
    raw_transcription being present, which is just as true of a voice
    transcript as of a photographed one."""
    await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables",
        "raw_transcription": "Met Marta at the listing this morning...",
        "source": "voice_note",
    })

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "voice_note"


async def test_a_photo_still_defaults_to_photo_notes(conn, ana):
    """The derivation stays as the fallback; only an explicit source overrides."""
    await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables",
        "raw_transcription": "handwriting, transcribed",
    })

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "photo_notes"


async def test_a_typed_note_still_defaults_to_text(conn, ana):
    await save_meeting(conn, ana, {"summary": "Quick note"})

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "text"


def test_the_schema_offers_the_source_so_the_model_can_set_it():
    from gaia.capabilities.meetings import SAVE_SCHEMA

    assert SAVE_SCHEMA["properties"]["source"]["enum"] == [
        "text", "photo_notes", "voice_note"
    ]
