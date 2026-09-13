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

## 3 · The Graph API send has never executed — RESOLVED 2026-09-12

**Status:** resolved. Kept as a record of what first contact actually proved.

A postpaid mobile line was verified as a Cloud API sender (+1 305-790-6417,
`CONNECTED` / `VERIFIED`, TIER_250), and both `send_text` and `send_template`
have now run against Meta for real.

What it took, none of which was the code: the WABA had to be subscribed to the
app (`POST /{WABA_ID}/subscribed_apps`) — configuring the app's webhook callback
is not the same registration, and without the second one Meta accepts inbound
messages and delivers nothing. Zero `POST /webhook` requests ever reached the app
before that call; app mode and business verification were both red herrings.

`tests/live/conftest.py::no_real_whatsapp` stays as it is. The live tier still
must not be able to text a developer by accident.

## 4 · Contact profiles are append-only

**Status:** designed and approved, implementation plan not written yet.
**Spec:** `specs/2026-09-12-profile-consolidation-design.md`

`merge_profile` (`core/db/contacts.py:141`) concatenates with `' | '` and trims
from the front at 4000 characters. Nothing retires a superseded fact, so Diego
Ojeda's profile opens with `At Rilea.` — false since he moved to Related — and
nothing records which meeting contributed which sentence.

`c0d9990` narrowed what gets written (durable identity, not events); this is the
other half, how those facts are combined. Carries a `meeting_contacts.note`
column that keeps the per-contact fact the model already generates, a
`profile_versions` audit table, `contacts.consolidated_at`, and a nightly org-wide
job beside the digest.

The privacy rule is the part to get right: the consolidation query must filter on
`meetings.visibility = 'org'` literally, never `visible()`, which also admits rows
owned by a scope user. The spec asks for that test to be written first.

## 5 · `meetings` has no read path

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

## 6 · Delete a meeting note

**Status:** decided in conversation 2026-09-12, not specified.

Nothing deletes today. The meetings capability has `save_meeting`,
`search_memory`, `lookup_contact` and `set_meeting_visibility`.

Decided: user-initiated only, never on a timer — `RETENTION_DAYS` stays long and
nothing is automatically aged out of the live database. One confirmation,
all-or-nothing, after a preview naming what goes ("the Sept 12 photo: 7
commitments, 2 leads, profile notes on 3 people"). Itemised picking reads well in
a spec and is miserable over WhatsApp, where every choice costs a round trip.

Two things deliberately survive a delete, and the preview has to say so: the
conversation log, because `messages` holds both her inbound text and the
assistant's reply echoing the whole transcription, and purging it would silently
edit a history she can still scroll on her phone; and `contacts.profile`, because
deleting the note that revealed Tyler works at Two Roads should not unlearn that
he works at Two Roads.

Cheaper after §4 lands: `meeting_contacts` already cascades, so a note's
contribution disappears with the meeting and the next consolidation pass
regenerates the profile without it.

**The trap:** `commitments.meeting_id` is `ON DELETE SET NULL` and `leads` never
reference the meeting at all, so a naive `DELETE FROM meetings` leaves both alive
and orphaned — still arriving in her 8am digest with nothing behind them.

## 7 · Increment 2 — the calendar

**Status:** scoped in `specs/2026-09-08-gaia-butler-design.md` §1; refined in
conversation 2026-09-12; no spec of its own yet.

New decisions, none of them in a document yet:

- **Invites with attendees, approval-gated.** `BASE_PROMPT` says "Never contact
  third parties", and a Google Calendar invite emails every attendee. The rule
  becomes "never unprompted", with an explicit approval step showing the exact
  address list before anything sends. A deleted event is recoverable; an
  invitation to a client is not.
- **Google Meet, not Teams.** Meet is one field on the event being created.
  Gaia has no Microsoft 365 tenant, so a Teams link would mean a second OAuth
  subsystem through Microsoft Graph — its own app registration, consent flow and
  encrypted refresh tokens. Parked, not refused.
- **`users` has no email column.** Binding a WhatsApp identity to the Google
  account that authorised is a migration, and without it nothing verifies that the
  account consenting is the person expected.
- **The Internal-OAuth assumption needs confirming.** No Google verification
  review only holds while every user is on Gaia's Workspace domain. One
  contractor on a personal Gmail forces an External app — sensitive-scope review,
  privacy policy, demo video — or a Testing-mode app capped at 100 users whose
  tokens expire every 7 days.

**Also missing:** attendee email addresses. `contacts.email` is nullable and
almost entirely empty; the notes carry names. An invite needs an address, so the
design has to say how Gaia asks for and remembers them without interrogating
someone over every lunch.

## 8 · The model cannot see a profile before it writes to it

**Status:** identified; mitigated rather than fixed by §4.

`save_meeting` never receives the contact's current `profile`, so the model
restates what is already there — `At Two Roads.` beside `Head of acquisitions at
Two Roads`. Consolidation cleans this up nightly, which is why it is a mitigation:
the duplication is still written every time, it is just not permanent.

Fixing it at the source costs a `lookup_contact` round trip per known contact per
save, or a `merge_profile` that compares before appending. Neither is worth doing
before a few weeks of consolidated profiles show whether the duplication still
matters.

## 9 · The typing indicator expires before a slow turn finishes

**Status:** accepted, pending real numbers.

Meta dismisses the indicator on the reply or after 25 seconds, whichever comes
first (`core/whatsapp.py:110`, `mark_read`). A photo turn runs 10-30 seconds
(`butler.py:205`), so the bubble can vanish while the model is still working —
the silence the feature exists to remove, arriving slightly later.

Re-issuing needs a timer running alongside the turn, cancelled on reply. Not
worth it until logged turn durations say how often 25 seconds is actually
exceeded.

## 10 · Meta-side follow-ups

**Status:** operational, not code. Recorded here because nothing else tracks them.

- **`daily_digest` template** was submitted 2026-09-12 and is `PENDING`. Until it
  is approved, any digest to someone who has not texted in 24 hours cannot be
  delivered — `send_digest` falls through to `send_template` and Meta rejects it.
- **The app is in Development mode.** Webhooks work regardless; Live needs a
  Privacy Policy URL, which the app could serve itself from a `/privacy` route.
- **Business verification is not started.** Not a blocker — it raises messaging
  limits beyond TIER_250 and unlocks display-name review.
- **The system user token lacks `business_management`**, so enumerating WABAs
  needs the Graph API Explorer. Only matters for administration.

## 11 · Nothing measures what this costs or whether caching works

**Status:** designed and approved, implementation plan not written yet.
**Spec:** `specs/2026-09-12-observability-design.md`

`response.usage` comes back on every call at `core/llm.py:65` and
`jobs/digest.py:107` and is discarded. There is no record of spend by day, job or
developer, and no measurement of the cache design in `_system_blocks`
(`core/llm.py:15`) — whose docstring makes a specific claim about this workload
("costs more than not caching") that has never been checked against a real
`cache_read_input_tokens` figure.

Carries an `llm_calls` table (counts only, no prompt text), a `record` helper
called from three sites, and a `stats` subcommand that emits a self-contained
HTML page over stdout, so `ssh gaia '...' > report.html` is the whole workflow.

Product counts — meetings filed, contacts per meeting, commitments with a due
date — are deliberately *not* stored: they are already queryable, and a second
copy would drift.

