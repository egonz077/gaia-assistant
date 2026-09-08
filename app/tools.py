"""Tools Claude can call. Keep them coarse-grained — fewer, richer tools beat many tiny ones."""

import json
import db

TOOL_DEFS = [
    {
        "name": "save_meeting",
        "description": (
            "Save a meeting/notes after extracting structure. Creates the meeting record, "
            "links contacts (creating them if new), stores commitments, updates lead "
            "next_action dates, and indexes the summary into semantic memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "raw_transcription": {"type": "string", "description": "Full transcription if source was a photo"},
                "contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "profile_update": {"type": "string", "description": "New facts learned about this person, to merge into their rolling profile"},
                        },
                        "required": ["name"],
                    },
                },
                "commitments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "due_at": {"type": "string", "description": "ISO 8601, optional"},
                            "contact_name": {"type": "string"},
                        },
                        "required": ["description"],
                    },
                },
            },
            "required": ["summary", "contacts"],
        },
    },
    {
        "name": "search_memory",
        "description": "Semantic search over past meetings and notes. Use for questions about history ('what did X say about...').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "contact_name": {"type": "string", "description": "Optional filter"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_leads",
        "description": "List leads, optionally filtered by status or due follow-ups. Use to answer 'who do I need to follow up with'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "due_only": {"type": "boolean"},
                "status": {"type": "string", "enum": ["new", "active", "under_contract", "closed", "lost", "dormant"]},
            },
        },
    },
    {
        "name": "update_lead",
        "description": "Update a lead's status, next action date/note, or mark a commitment done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string"},
                "status": {"type": "string"},
                "next_action_at": {"type": "string"},
                "next_action_note": {"type": "string"},
                "complete_commitment_id": {"type": "string"},
            },
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    try:
        if name == "save_meeting":
            return json.dumps(db.save_meeting(args))
        if name == "search_memory":
            return json.dumps(db.search_memory(args["query"], args.get("contact_name")))
        if name == "query_leads":
            return json.dumps(db.query_leads(args.get("due_only", False), args.get("status")))
        if name == "update_lead":
            return json.dumps(db.update_lead(args))
        return f"unknown tool {name}"
    except Exception as e:
        return f"tool error: {e}"
