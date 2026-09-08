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

    first_contact_id = None
    for entry in contacts:
        contact_id = await contacts_db.get_or_create(conn, user, entry["name"])
        first_contact_id = first_contact_id or contact_id
        if entry.get("profile_update"):
            await contacts_db.merge_profile(conn, user, contact_id, entry["profile_update"])

    # Visibility comes from the meeting, never defaulted — a private note whose
    # embedding is org-visible is searchable by the whole company.
    await memory_db.index_meeting(
        conn, user, meeting_id, args["summary"], visibility, first_contact_id
    )

    return {
        "saved": True,
        "meeting_id": str(meeting_id),
        "contacts": [c["name"] for c in contacts],
        "visibility": visibility,
    }


async def search_memory(conn, user: User, args: dict) -> dict:
    results = await memory_db.search(
        conn, user, args["query"], contact_name=args.get("contact_name")
    )
    return {"results": results}


async def lookup_contact(conn, user: User, args: dict) -> dict:
    found = await contacts_db.lookup(conn, user, args["name"])
    return {"contact": found} if found else {"contact": None, "note": "no such contact"}
