from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.leads.tools import (
    complete_commitments,
    create_lead,
    list_commitments,
    query_leads,
    update_lead,
)

STATUSES = ["new", "active", "under_contract", "closed", "lost", "dormant"]

CAPABILITY = Capability(
    name="leads",
    prompt_fragment=(
        "\nWhen the user mentions someone who might transact, create a lead with a next "
        "action date so it reaches their morning digest. A lead without a next_action_at "
        "will never be followed up.\n"
        "\nCommitment ids are yours to look up, never the user's to supply — they see their "
        "commitments as sentences in a digest, so never ask the user for an id. Call "
        "list_commitments and match what they said against the descriptions yourself.\n"
        "\nBefore closing more than one commitment, say which ones you are about to close and "
        "wait for them to agree. Closing a follow-up is not easily undone: the digest is the "
        "only thing that would have reminded them, and a closed one never appears again.\n"
    ),
    tools=(
        Tool("create_lead",
             "Create a lead for a contact, with the next follow-up date. Creates the contact "
             "if they are new.",
             {"type": "object",
              "properties": {
                  "contact_name": {"type": "string"},
                  "description": {"type": "string",
                                  "description": "e.g. 'buying in Coral Gables, ~600k'"},
                  "status": {"type": "string", "enum": STATUSES},
                  "next_action_at": {"type": "string", "description": "ISO 8601"},
                  "next_action_note": {"type": "string",
                                       "description": "e.g. 'send Friday listing update'"},
                  "private": {"type": "boolean",
                              "description": "Keep off the company record. Defaults to false."},
              },
              "required": ["contact_name", "description"]},
             create_lead),
        Tool("query_leads",
             "List leads, optionally filtered by status or by whether follow-up is due.",
             {"type": "object",
              "properties": {"due_only": {"type": "boolean"},
                             "status": {"type": "string", "enum": STATUSES}}},
             query_leads),
        Tool("update_lead",
             "Update a lead's status, next action date, or note.",
             {"type": "object",
              "properties": {"lead_id": {"type": "string"},
                             "status": {"type": "string", "enum": STATUSES},
                             "next_action_at": {"type": "string"},
                             "next_action_note": {"type": "string"},
                             "description": {"type": "string"}},
              "required": ["lead_id"]},
             update_lead),
        Tool("list_commitments",
             "List everything the user still owes — what they mean by 'my commitments' or "
             "'my follow-ups'. Returns each one's id, which is the only way to get one. "
             "Call this before completing anything.",
             {"type": "object", "properties": {}},
             list_commitments),
        Tool("complete_commitments",
             "Mark one or more commitments done. Ids come from list_commitments; the user "
             "does not have them. Reports how many closed, and how many did not.",
             {"type": "object",
              "properties": {"commitment_ids": {
                  "type": "array", "items": {"type": "string"},
                  "description": "Ids from list_commitments."}},
              "required": ["commitment_ids"]},
             complete_commitments),
    ),
)
