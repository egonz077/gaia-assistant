from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.calendar.tools import (
    cancel_event,
    check_availability,
    confirm_invite,
    create_event,
    list_events_on,
    list_pending_invites,
    propose_invite,
)

CAPABILITY = Capability(
    name="calendar",
    prompt_fragment=(
        "\nYou can check the user's calendar and put things on it. Creating an event invites "
        "nobody — it lands only on their own calendar. To invite people you must call "
        "propose_invite, read the exact addresses back to the user, wait for them to agree, "
        "and only then call confirm_invite.\n"
        "\nNever call confirm_invite without having shown the addresses and heard a yes. An "
        "event you created can be deleted; an invitation that reached a client cannot be "
        "unsent. When you read addresses back, name the people — 'Dalila Serrao and two "
        "others at Arquitectonica' — because seven raw addresses are not checkable at a "
        "glance.\n"
        "\nOnce you have read an invitation back, any affirmative from the user is the "
        "approval — 'yes', 'send it', 'go', 'do it', 'send #4' — so call confirm_invite. Do "
        "not ask for a particular word, and never create another event in response to an "
        "approval: a new event is only for a new request.\n"
        "\nGive dates as a date and a wall-clock time in the user's own day. Never compute a "
        "UTC offset yourself.\n"
        "\nIf a tool says needs_connection, send the user the link it gives you and explain "
        "that Gaia needs access to their Google Calendar once.\n"
        "\nEvent ids and pending ids come only from your own tool results in the current "
        "turn -- create_event, propose_invite, list_pending_invites. Never invent one, and "
        "never rely on remembering one from an earlier turn: your history is prose and the "
        "ids are gone. When the user approves an invitation you proposed in an earlier turn, "
        "call list_pending_invites and confirm the matching pending_id. Never call "
        "propose_invite twice for the same event.\n"
        "\nA row in list_pending_invites has NOT been sent; it is waiting for the user's "
        "yes. To cancel or refer to an event from an earlier turn, call list_events_on for "
        "that day and use the event_id it returns; entries with created_by_gaia are the "
        "ones you made.\n"
    ),
    tools=(
        Tool("check_availability",
             "What the user is already booked for on a given day. Times only, no details.",
             {"type": "object",
              "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
              "required": ["date"]},
             check_availability),
        Tool("list_events_on",
             "The user's own events on a given day, with their event_id, title, times and "
             "attendees. This is where the id for an existing event comes from — to cancel "
             "it, or to refer to it. Entries with created_by_gaia are ones you created.",
             {"type": "object",
              "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
              "required": ["date"]},
             list_events_on),
        Tool("create_event",
             "Put an event on the user's own calendar, optionally with a Google Meet link. "
             "Invites nobody — use propose_invite afterwards to add people.",
             {"type": "object",
              "properties": {
                  "summary": {"type": "string"},
                  "date": {"type": "string", "description": "YYYY-MM-DD"},
                  "start_time": {"type": "string", "description": "HH:MM, the user's local time"},
                  "duration_minutes": {"type": "integer"},
                  "with_meet": {"type": "boolean"},
                  "lead_id": {"type": "string", "description": "Link this event to a lead."},
                  "commitment_id": {"type": "string"},
              },
              "required": ["summary", "date", "start_time"]},
             create_event),
        Tool("propose_invite",
             "Prepare an invitation for an existing event. Sends nothing. Returns the exact "
             "address list to read back to the user before confirming.",
             {"type": "object",
              "properties": {"event_id": {"type": "string"},
                             "emails": {"type": "array", "items": {"type": "string"}}},
              "required": ["event_id", "emails"]},
             propose_invite),
        Tool("confirm_invite",
             "Send the invitation the user just approved. Takes only the pending_id from "
             "propose_invite — the addresses are the ones already shown to the user.",
             {"type": "object",
              "properties": {"pending_id": {"type": "string"}},
              "required": ["pending_id"]},
             confirm_invite),
        Tool("list_pending_invites",
             "The invitations you have proposed that are still waiting for the user's yes, "
             "each with the event it is for. Call this when the user approves something "
             "you proposed in an earlier turn -- the pending_id lives here, not in your "
             "memory.",
             {"type": "object", "properties": {}},
             list_pending_invites),
        Tool("cancel_event",
             "Delete an event from the user's calendar, notifying anyone invited.",
             {"type": "object",
              "properties": {"event_id": {"type": "string"}},
              "required": ["event_id"]},
             cancel_event),
    ),
)
