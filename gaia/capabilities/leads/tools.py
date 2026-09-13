from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from gaia.core.models import User


async def create_lead(conn, user: User, args: dict) -> dict:
    lead_id = await leads_db.create(
        conn,
        user,
        contact_name=args["contact_name"],
        description=args["description"],
        status=args.get("status", "new"),
        next_action_at=args.get("next_action_at"),
        next_action_note=args.get("next_action_note"),
        visibility="private" if args.get("private") else "org",
    )
    return {"created": True, "lead_id": str(lead_id)}


async def query_leads(conn, user: User, args: dict) -> dict:
    rows = await leads_db.query(
        conn, user, due_only=args.get("due_only", False), status=args.get("status")
    )
    return {"leads": rows}


async def update_lead(conn, user: User, args: dict) -> dict:
    updated = await leads_db.update(
        conn,
        user,
        args["lead_id"],
        status=args.get("status"),
        next_action_at=args.get("next_action_at"),
        next_action_note=args.get("next_action_note"),
        description=args.get("description"),
    )
    return {"updated": updated} if updated else {
        "updated": False, "note": "no such lead, or it belongs to someone else"
    }


async def list_commitments(conn, user: User, args: dict) -> dict:
    rows = await commitments_db.query(conn, user)
    return {"commitments": rows}


async def complete_commitments(conn, user: User, args: dict) -> dict:
    ids = args.get("commitment_ids") or []
    closed = await commitments_db.complete(conn, user, ids)
    # Both numbers, always. "Closed 2" when three were asked for reads as
    # success to a model that is not also told one did not land, and the
    # difference is what the user needs to hear about.
    return {"completed": len(closed), "not_found": len(ids) - len(closed)}
