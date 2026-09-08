from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.leads.tools import (
    complete_commitment,
    create_lead,
    query_leads,
    update_lead,
)

STATUSES = ["new", "active", "under_contract", "closed", "lost", "dormant"]

CAPABILITY = Capability(
    name="leads",
    description="Tracking deals and follow-ups",
    prompt_fragment=(
        "\nWhen the user mentions someone who might transact, create a lead with a next "
        "action date so it reaches their morning digest. A lead without a next_action_at "
        "will never be followed up.\n"
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
        Tool("complete_commitment",
             "Mark a commitment done.",
             {"type": "object", "properties": {"commitment_id": {"type": "string"}},
              "required": ["commitment_id"]},
             complete_commitment),
    ),
)
