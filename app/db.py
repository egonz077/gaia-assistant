"""Postgres layer. psycopg3 + pgvector. Embeddings via Voyage (Anthropic's recommended provider)."""

import os
import datetime

import psycopg
from psycopg.rows import dict_row
import voyageai

DSN = os.environ["DATABASE_URL"]
vo = voyageai.Client()  # VOYAGE_API_KEY from env
EMBED_MODEL = "voyage-3.5-lite"  # 1024 dims default — matches schema vector(1024)


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def today_str() -> str:
    return datetime.date.today().isoformat()


# ---------- conversation log ----------

def message_seen(wa_msg_id: str) -> bool:
    with conn() as c:
        row = c.execute("SELECT 1 FROM messages WHERE wa_msg_id = %s", (wa_msg_id,)).fetchone()
        return row is not None


def log_message(role: str, content: str, wa_msg_id: str | None):
    with conn() as c:
        c.execute(
            "INSERT INTO messages (role, content, wa_msg_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (role, content, wa_msg_id),
        )


def recent_messages(limit: int = 20) -> list:
    with conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages ORDER BY created_at DESC LIMIT %s", (limit,)
        ).fetchall()
    rows.reverse()
    # collapse to simple text messages; must alternate roles for the API
    out = []
    for r in rows:
        if out and out[-1]["role"] == r["role"]:
            out[-1]["content"] += "\n" + r["content"]
        else:
            out.append({"role": r["role"], "content": r["content"]})
    # history must start with a user turn
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


# ---------- contacts & profiles ----------

def all_contact_profiles() -> str:
    with conn() as c:
        rows = c.execute("SELECT name, profile FROM contacts WHERE profile != '' ORDER BY updated_at DESC LIMIT 50").fetchall()
    return "\n".join(f"- {r['name']}: {r['profile']}" for r in rows) or "(none yet)"


def get_or_create_contact(c, name: str) -> str:
    row = c.execute("SELECT id FROM contacts WHERE lower(name) = lower(%s)", (name,)).fetchone()
    if row:
        return row["id"]
    row = c.execute("INSERT INTO contacts (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


def merge_profile(c, contact_id: str, update: str):
    """Append new facts; periodically Claude rewrites the whole profile via save_meeting."""
    c.execute(
        """UPDATE contacts
           SET profile = CASE WHEN profile = '' THEN %s ELSE profile || ' | ' || %s END,
               updated_at = now()
           WHERE id = %s""",
        (update, update, contact_id),
    )


# ---------- meetings ----------

def save_meeting(args: dict) -> dict:
    with conn() as c:
        source = "photo_notes" if args.get("raw_transcription") else "text"
        meeting = c.execute(
            "INSERT INTO meetings (source, raw_input, summary) VALUES (%s, %s, %s) RETURNING id",
            (source, args.get("raw_transcription"), args["summary"]),
        ).fetchone()
        mid = meeting["id"]

        contact_ids = {}
        for contact in args.get("contacts", []):
            cid = get_or_create_contact(c, contact["name"])
            contact_ids[contact["name"].lower()] = cid
            c.execute(
                "INSERT INTO meeting_contacts (meeting_id, contact_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (mid, cid),
            )
            if contact.get("profile_update"):
                merge_profile(c, cid, contact["profile_update"])

        for cm in args.get("commitments", []):
            cid = contact_ids.get((cm.get("contact_name") or "").lower())
            c.execute(
                "INSERT INTO commitments (meeting_id, contact_id, description, due_at) VALUES (%s, %s, %s, %s)",
                (mid, cid, cm["description"], cm.get("due_at")),
            )

        # index into semantic memory (one chunk per meeting for v1)
        emb = vo.embed([args["summary"]], model=EMBED_MODEL, input_type="document").embeddings[0]
        first_cid = next(iter(contact_ids.values()), None)
        c.execute(
            "INSERT INTO memory_chunks (meeting_id, contact_id, content, embedding) VALUES (%s, %s, %s, %s)",
            (mid, first_cid, args["summary"], emb),
        )

    return {"saved": True, "meeting_id": str(mid), "contacts": list(contact_ids)}


# ---------- semantic search ----------

def search_memory(query: str, contact_name: str | None = None) -> list:
    emb = vo.embed([query], model=EMBED_MODEL, input_type="query").embeddings[0]
    sql = """
        SELECT mc.content, mc.created_at, ct.name AS contact
        FROM memory_chunks mc
        LEFT JOIN contacts ct ON ct.id = mc.contact_id
        {where}
        ORDER BY mc.embedding <=> %(emb)s::vector
        LIMIT 6
    """
    params = {"emb": emb}
    where = ""
    if contact_name:
        where = "WHERE lower(ct.name) = lower(%(name)s)"
        params["name"] = contact_name
    with conn() as c:
        rows = c.execute(sql.format(where=where), params).fetchall()
    return [
        {"content": r["content"], "date": r["created_at"].date().isoformat(), "contact": r["contact"]}
        for r in rows
    ]


# ---------- leads ----------

def query_leads(due_only: bool, status: str | None) -> list:
    clauses, params = [], []
    if due_only:
        clauses.append("l.next_action_at <= now()")
    if status:
        clauses.append("l.status = %s")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn() as c:
        rows = c.execute(
            f"""SELECT l.id, ct.name, l.description, l.status, l.next_action_at, l.next_action_note
                FROM leads l JOIN contacts ct ON ct.id = l.contact_id
                {where} ORDER BY l.next_action_at NULLS LAST LIMIT 25""",
            params,
        ).fetchall()
    return [
        {**r, "id": str(r["id"]),
         "next_action_at": r["next_action_at"].isoformat() if r["next_action_at"] else None}
        for r in rows
    ]


def update_lead(args: dict) -> dict:
    with conn() as c:
        if args.get("complete_commitment_id"):
            c.execute("UPDATE commitments SET done_at = now() WHERE id = %s", (args["complete_commitment_id"],))
        if args.get("lead_id"):
            sets, params = ["updated_at = now()"], []
            for field in ("status", "next_action_at", "next_action_note"):
                if args.get(field):
                    sets.append(f"{field} = %s")
                    params.append(args[field])
            params.append(args["lead_id"])
            c.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = %s", params)
    return {"updated": True}
