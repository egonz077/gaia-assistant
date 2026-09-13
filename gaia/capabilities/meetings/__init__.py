from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.meetings.tools import (
    lookup_contact,
    save_meeting,
    search_memory,
    set_meeting_visibility,
)

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
            "description": "True if the user asks to keep this off the company record. "
                           "Defaults to false: colleagues can see it. A private meeting "
                           "also contributes nothing to shared contact profiles - see "
                           "profile_update.",
        },
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile_update": {
                        "type": "string",
                        "description": "Durable facts about who this person is and why Gaia "
                                       "Group would want to meet them: their organization, "
                                       "role, what they build or buy, what they are looking "
                                       "for, how you know them. Everyone at Gaia Group can "
                                       "read it. Not events, dates or arrangements - 'wants "
                                       "lunch', 'emailed 9/8/26', 'Zoom set up by Dan' belong "
                                       "to the meeting and its commitments, which are dated "
                                       "and searchable; a profile is not. Omit this field when "
                                       "a meeting taught you nothing durable about the person, "
                                       "which is often. Skipped when private is true: nothing "
                                       "learned in a private meeting is written to a shared "
                                       "profile.",
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

SET_VISIBILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_id": {"type": "string"},
        "private": {
            "type": "boolean",
            "description": "True to take this already-filed meeting off the company record; "
                           "false to restore it to org-visible.",
        },
    },
    "required": ["meeting_id", "private"],
}

CAPABILITY = Capability(
    name="meetings",
    prompt_fragment=(
        "\nWhen you are asked to keep something off the company record, pass private: true "
        "to save_meeting. Otherwise colleagues at Gaia Group can see it, which is the default.\n"
        "A private meeting stays private end to end: its notes are excluded from "
        "company-wide search, and nothing learned in it is added to a person's shared "
        "contact profile, even if you pass profile_update. save_meeting tells you when it "
        "skipped one - say so plainly rather than implying it was filed on their profile.\n"
        "A contact profile answers who someone is and why you would take a meeting with "
        "them, not what happened. Leave profile_update out unless the meeting taught you "
        "something that will still be true next year.\n"
        "If an already-filed meeting should later be made private - or put back on the "
        "record - use set_meeting_visibility. This also hides (or restores) that meeting's "
        "notes in company-wide search.\n"
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
        Tool("set_meeting_visibility",
             "Reclassify an already-filed meeting as private or back to org-visible. Also "
             "cascades to that meeting's commitments and its semantic-memory chunk.",
             SET_VISIBILITY_SCHEMA, set_meeting_visibility),
    ),
)
