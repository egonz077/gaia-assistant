# Backlog

Work that is decided or identified but not built. Living document — unlike
`reviews/2026-09-08-known-issues.md`, which is a closed record of increment 1.

Each entry says what it is, why it is not done, and where the thinking lives, so
none of it depends on someone opening the right spec by chance.

---

## 1 · Onboarding interview and per-developer digest time

**Status:** designed and approved, implementation plan not written yet.
**Spec:** `specs/2026-09-12-onboarding-interview-design.md`

The digest hour is a module constant (`jobs/digest.py:23`), so nobody can ask for
their list at 06:30 or after the morning site walk. And nothing in the system
ever introduces itself — a developer is created by CLI and their first text is
handled as an ordinary turn.

Carries five concrete changes: a `digest_at` column, `due_users` comparing
against it, a `set_preferences` tool in a new capability, the first-turn
interview fragment in the volatile prompt half, and the tick dropping from 900s
to 300s so a chosen 09:15 is not served at 09:29.

**Breaks on landing:** `tests/live/test_live_digest.py` imports `SEND_HOUR`,
which this replaces with a column default. `_zone_past_send_hour` and
`test_selection_is_timezone_aware_at_a_fixed_instant` must be updated in the same
change.

## 2 · `leads` is shaped for brokers, not developers

**Status:** identified, not designed.
**Noted in:** `specs/2026-09-12-onboarding-interview-design.md` §9

Gaia Group Development's users are developers. The vocabulary was corrected in
`migrations/002_developer_role.sql` and the prompt copy, but the domain model was
deliberately left alone, because it is structural rather than cosmetic:

- `lead_status` is `new/active/under_contract/closed/lost/dormant` — a
  buyer/seller pipeline. A developer moves parcels and projects through
  evaluating → under contract on land → entitlement → permitted → under
  construction → delivered.
- The leads prompt fragment says "someone who might transact".
- **`leads.contact_id` is `NOT NULL`**, which encodes "a lead is a person". A
  site under evaluation may have no person attached at all.

Renaming the statuses without reframing the table would leave the model with a
contradictory vocabulary, which is why this is its own piece of work rather than
part of the vocabulary fix.

`contacts`, `meetings`, `commitments` and `memory_chunks` all generalise fine —
developers meet people and make promises like anyone.

## 3 · The Graph API send has never executed

**Status:** blocked on a verified WhatsApp sender.

Every other edge in the system now has coverage against the real service: the
model and its tool schemas, Postgres, Voyage embeddings, the private-visibility
boundary, and the digest's selection and retry behaviour — 17 tests under
`pytest -m live`.

`wa.send_text` and `wa.send_template` against Meta are the exception, by design:
`tests/live/conftest.py::no_real_whatsapp` makes constructing a real client raise,
so the live tier cannot text a developer by accident.

First contact therefore validates `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` and the
24-hour-window rules all at once. Worth one deliberate smoke send to a seeded
number before a developer's real message is the first thing through.

**Prerequisite:** a phone number verified as a Cloud API sender. Twilio is a dead
end for this — Meta will not deliver OTP short codes to VoIP numbers, and voice
verification stalled on an unroutable number. A postpaid mobile line on the
company account is the path.

## 4 · `meetings` has no read path

**Status:** accepted for now, deliberately.
**Recorded in:** `gaia/core/db/meetings.py:14`

Nothing in `gaia/` selects from `meetings`, and `raw_input` in particular has no
reader anywhere outside a test assertion. Only the summary is indexed into
`memory_chunks`, so anything the summary dropped is preserved in `raw_input` and
simultaneously unfindable by `search_memory`.

Kept because it is the only copy of what was actually filed — the photo is not
stored, so a discarded transcription cannot be re-derived. A reader can be added
the day a meetings list, or "show me my original notes", exists.

Indexing `raw_input` as a second chunk inheriting the parent's visibility would
make it searchable; `memory_chunks` already supports several rows per
`meeting_id`. Costs roughly double the Voyage volume and puts OCR noise into the
ranking, so it is a product call rather than an obvious improvement.
