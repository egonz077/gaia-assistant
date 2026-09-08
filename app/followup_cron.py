"""
Daily follow-up nudge. Run via cron in the app container:
    0 13 * * *  python followup_cron.py     # 8am Miami (13:00 UTC, adjust for DST)

Queries due leads + open commitments, has Claude write one natural morning
message, sends it into the same WhatsApp thread.
"""

import os
import asyncio

import anthropic

import db
import whatsapp

OWNER_WA_ID = os.environ["OWNER_WA_ID"]
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

claude = anthropic.Anthropic()


async def main():
    due_leads = db.query_leads(due_only=True, status=None)

    with db.conn() as c:
        open_commitments = c.execute(
            """SELECT cm.description, cm.due_at, ct.name
               FROM commitments cm LEFT JOIN contacts ct ON ct.id = cm.contact_id
               WHERE cm.done_at IS NULL AND cm.due_at <= now() + interval '2 days'
               ORDER BY cm.due_at"""
        ).fetchall()

    if not due_leads and not open_commitments:
        return  # quiet day — don't send noise

    context = {
        "due_leads": due_leads,
        "commitments": [
            {"description": r["description"],
             "due": r["due_at"].isoformat() if r["due_at"] else None,
             "contact": r["name"]}
            for r in open_commitments
        ],
    }

    response = claude.messages.create(
        model=MODEL,
        max_tokens=600,
        system=(
            "Write a short, warm morning WhatsApp message for a real-estate agent "
            "listing what needs follow-up today. Group by person. Plain text, no markdown. "
            "End by offering to draft any of the follow-up texts."
        ),
        messages=[{"role": "user", "content": str(context)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")

    db.log_message("assistant", text, None)
    await whatsapp.send_text(OWNER_WA_ID, text)


if __name__ == "__main__":
    asyncio.run(main())
