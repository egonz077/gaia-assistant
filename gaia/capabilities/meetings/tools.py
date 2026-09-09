from gaia.core.db import contacts as contacts_db
from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db
from gaia.core.models import User


def _private_profile_notes(visibility: str, contacts: list[dict]) -> str:
    """The profile updates a private meeting may not publish, as one block.

    A contact is a shared company address-book entry: `get_or_create` returns
    rows owned by colleagues, and `contacts.profile` is a single org-visible
    string with no per-entry visibility. There is therefore nowhere on a shared
    contact to put a fact learned in confidence — merging one there publishes
    it to the whole company through lookup_contact, which is exactly what
    `private` exists to prevent.

    Refusing the write is only half an answer. Telling someone a client
    confidence was saved somewhere it does not exist is worse than dropping it
    visibly, because a visible drop at least lets her retype it. So the text
    goes into the meeting itself, which already carries `private`, and the
    caller writes it to both places that make that true: `raw_input`, the
    durable record of what was filed, and the indexed chunk, which is what
    makes it findable again through search_memory — by its owner alone.
    """
    if visibility != "private":
        return ""
    return "\n".join(
        f"Note on {e['name']}: {e['profile_update']}"
        for e in contacts
        if e.get("profile_update")
    )


async def save_meeting(conn, user: User, args: dict) -> dict:
    visibility = "private" if args.get("private") else "org"
    contacts = args.get("contacts", [])

    kept = _private_profile_notes(visibility, contacts)
    skipped = [e["name"] for e in contacts if kept and e.get("profile_update")]

    raw_transcription = args.get("raw_transcription")
    raw_input = "\n\n".join(part for part in (raw_transcription, kept) if part) or None

    meeting_id = await meetings_db.save(
        conn,
        user,
        summary=args["summary"],
        # `source` describes where the meeting came from, so it still keys off
        # the transcription alone — a dictated private note is not a photo.
        source="photo_notes" if raw_transcription else "text",
        raw_input=raw_input,
        happened_at=args.get("happened_at"),
        visibility=visibility,
        contact_names=tuple(c["name"] for c in contacts),
        commitments=tuple(args.get("commitments", [])),
    )

    # Only the first contact is linked to the memory chunk (memory_chunks has
    # a single contact_id) - deliberate, not an oversight. A meeting with
    # several contacts still gets one chunk; a richer many-to-many link would
    # need its own join table.
    first_contact_id = None
    for entry in contacts:
        contact_id = await contacts_db.get_or_create(conn, user, entry["name"])
        first_contact_id = first_contact_id or contact_id
        if entry.get("profile_update") and visibility == "org":
            await contacts_db.merge_profile(conn, user, contact_id, entry["profile_update"])

    # Visibility comes from the meeting, never defaulted — a private note whose
    # embedding is org-visible is searchable by the whole company. The kept
    # notes ride in the indexed content for the same reason they are in
    # raw_input: this is the path that makes them retrievable again, and it
    # inherits `private` along with everything else derived from the meeting.
    await memory_db.index_meeting(
        conn,
        user,
        meeting_id,
        f"{args['summary']}\n\n{kept}" if kept else args["summary"],
        visibility,
        first_contact_id,
    )

    result = {
        "saved": True,
        "meeting_id": str(meeting_id),
        "contacts": [c["name"] for c in contacts],
        "visibility": visibility,
    }
    # Told, not silently dropped — and told accurately. Every clause here is a
    # claim about where the data is, so each one has to be true.
    if skipped:
        result["profile_updates_skipped"] = skipped
        result["note"] = (
            "This meeting is private, so what you learned about "
            f"{', '.join(skipped)} was not added to the shared company profile, "
            "which every agent at Gaia can read. It is saved in this private "
            f"meeting instead, where only {user.name} can reach it - "
            "search_memory will find it. Say so plainly."
        )
    return result


async def search_memory(conn, user: User, args: dict) -> dict:
    results = await memory_db.search(
        conn, user, args["query"], contact_name=args.get("contact_name")
    )
    return {"results": results}


async def lookup_contact(conn, user: User, args: dict) -> dict:
    found = await contacts_db.lookup(conn, user, args["name"])
    return {"contact": found} if found else {"contact": None, "note": "no such contact"}


async def set_meeting_visibility(conn, user: User, args: dict) -> dict:
    """Reclassify an already-filed meeting. Uses the same private-boolean
    vocabulary as save_meeting rather than the raw org/private enum, so the
    model sees one consistent concept for visibility either way.

    Note what this cannot undo: `contacts.profile` is an append-only string,
    so a profile_update merged when the meeting was org-visible stays on the
    shared profile after the meeting is made private. Retracting it needs
    per-entry profile visibility, which is the deferred consolidation pass.
    The tool description and prompt therefore claim only what this does — it
    hides the meeting and its notes, not text already merged elsewhere.

    In the other direction it does more than it used to, deliberately. A
    private meeting's withheld profile updates live in that meeting's own
    chunk now, so restoring the meeting to the record publishes them to
    company-wide search along with the rest of its notes. That is the same
    promise this tool already made — "put it back on the record" — applied to
    all of the meeting's content rather than some of it, and it only ever
    happens because the owner explicitly asked for it.
    """
    visibility = "private" if args.get("private") else "org"
    changed = await meetings_db.set_visibility(conn, user, args["meeting_id"], visibility)
    if not changed:
        return {"changed": False, "note": "no such meeting, or it belongs to someone else"}
    return {"changed": True, "visibility": visibility}
