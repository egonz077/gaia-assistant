from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.meetings.tools import lookup_contact, save_meeting, search_memory

SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "raw_transcription": {
            "type": "string",
            "description": "Full transcription when the source was a photo",
        },
        "happened_at": {
            "type": "string",
            "description": "ISO 8601. Use when the meeting was not today — notes "
                           "photographed the next morning belong to the day they happened.",
        },
        "private": {
            "type": "boolean",
            "description": "True if she asks to keep this off the company record. "
                           "Defaults to false: colleagues can see it.",
        },
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile_update": {
                        "type": "string",
                        "description": "New facts about this person to merge into their profile",
                    },
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
}

CAPABILITY = Capability(
    name="meetings",
    description="Filing and recalling meeting notes",
    prompt_fragment=(
        "\nWhen she asks you to keep something off the company record, pass private: true "
        "to save_meeting. Otherwise colleagues at Gaia can see it, which is the default.\n"
    ),
    tools=(
        Tool("save_meeting",
             "Save a meeting after extracting structure: creates the meeting, links contacts "
             "(creating them if new), stores commitments, and indexes the summary into "
             "semantic memory.",
             SAVE_SCHEMA, save_meeting),
        Tool("search_memory",
             "Semantic search over past meetings and notes. Use for questions about history.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "contact_name": {"type": "string", "description": "Optional filter"}},
              "required": ["query"]},
             search_memory),
        Tool("lookup_contact",
             "Fetch what is known about one person: profile, phone, email.",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             lookup_contact),
    ),
)
