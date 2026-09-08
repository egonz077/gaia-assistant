from gaia.core.db import contacts as contacts_db
from gaia.core.db import meetings as meetings_db
from gaia.core.db import memory as memory_db
from gaia.core.models import User


async def save_meeting(conn, user: User, args: dict) -> dict:
    visibility = "private" if args.get("private") else "org"
    contacts = args.get("contacts", [])

    meeting_id = await meetings_db.save(
        conn,
        user,
        summary=args["summary"],
        source="photo_notes" if args.get("raw_transcription") else "text",
        raw_input=args.get("raw_transcription"),
        happened_at=args.get("happened_at"),
        visibility=visibility,
        contact_names=tuple(c["name"] for c in contacts),
        commitments=tuple(args.get("commitments", [])),
    )

    # Only the first contact is linked to the memory chunk (memory_chunks has
    # a single contact_id) - deliberate, not an oversight. A meeting with
    # several contacts still gets one chunk; a richer many-to-many link would
    # need its own join table.
    #
    # A contact is a shared company address-book entry: `get_or_create` returns
    # rows owned by colleagues, and `contacts.profile` is one org-visible string
    # with no per-entry visibility. So there is nowhere on a shared contact to
    # put a fact learned in confidence — merging one there publishes it to the
    # whole company through lookup_contact, which is exactly what `private`
    # exists to prevent. A private meeting therefore contributes no profile
    # text at all. The fact is not lost: it stays in the meeting summary and
    # its memory chunk, both of which inherit `private` and so are searchable
    # only by the owner.
    first_contact_id = None
    skipped: list[str] = []
    for entry in contacts:
        contact_id = await contacts_db.get_or_create(conn, user, entry["name"])
        first_contact_id = first_contact_id or contact_id
        if not entry.get("profile_update"):
            continue
        if visibility == "org":
            await contacts_db.merge_profile(conn, user, contact_id, entry["profile_update"])
        else:
            skipped.append(entry["name"])

    # Visibility comes from the meeting, never defaulted — a private note whose
    # embedding is org-visible is searchable by the whole company.
    await memory_db.index_meeting(
        conn, user, meeting_id, args["summary"], visibility, first_contact_id
    )

    result = {
        "saved": True,
        "meeting_id": str(meeting_id),
        "contacts": [c["name"] for c in contacts],
        "visibility": visibility,
    }
    # Told, not silently dropped: the model must be able to say "I kept that
    # off their profile" rather than implying it was filed there.
    if skipped:
        result["profile_updates_skipped"] = skipped
        result["note"] = (
            "This meeting is private, so what you learned about "
            f"{', '.join(skipped)} was not added to the shared company profile. "
            "It is kept in the private meeting note instead. Say so plainly."
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
    """
    visibility = "private" if args.get("private") else "org"
    changed = await meetings_db.set_visibility(conn, user, args["meeting_id"], visibility)
    if not changed:
        return {"changed": False, "note": "no such meeting, or it belongs to someone else"}
    return {"changed": True, "visibility": visibility}
