# Onboarding interview and per-developer digest time

**Date:** 2026-09-12
**Status:** design approved, not yet implemented
**Supersedes nothing. Defers:** the `leads` → projects pipeline reframe (see §9)

## Problem

The morning digest goes out at 08:00 local for everyone. The hour is a module
constant — `SEND_HOUR = 8` at `jobs/digest.py:23` — and no per-user setting
exists. A developer who starts at 06:30, or who wants the list after the
morning site walk at 15:00, cannot say so.

Separately, nothing in the system ever introduces itself. A user comes into
existence through `python -m gaia.core.admin add-user` and their first text is
handled as an ordinary turn. There are exactly four prompts in the codebase and
none of them onboards anyone.

These are one piece of work: the natural place to ask someone what time they
want their list is the first time they ever text.

## Decisions taken

| Question | Decision |
|---|---|
| What does the interview cover? | Digest time and what to call them. **Not** timezone — see §4. |
| How insistent? | Ask once. Stamp `onboarded_at` when asked, not when answered. Never raise it again. |
| Where does the logic live? | Tool in a new capability; trigger in the **volatile** prompt half. |
| Built for Gaia only, or to generalize? | To generalize. Gaia has three developers in one timezone; the machinery is built anyway. |

## 1 · Why the trigger cannot live in the capability

`registry.prompt_fragments(user)` feeds the **stable, cache-marked** half of
the system prompt (`core/llm.py:_system_blocks`). A fragment that varied per
turn would invalidate the cached prefix — every tool definition plus the base
prompt, rewritten at 1.25× — on every turn for every developer.

That is not hypothetical. The contact roster used to sit in the cached half and
reordered on essentially every `save_meeting`, which is why
`build_system_prompt` returns two parts and only the first is marked. A
conditional onboarding fragment has exactly the same shape as that bug.

So:

- **The tool** goes in the registry. It is permanently available, role-agnostic,
  and a developer must be able to change their time on any day, not only the
  first one.
- **The interview fragment** goes in the volatile half, appended by
  `build_system_prompt` when the trigger condition holds.

A third option — a dedicated first-turn branch in `handle_turn` — was rejected:
a second path through the agent loop means re-implementing the apology and
delivery-before-logging rules in two places.

## 2 · Schema

```sql
-- migrations/003_onboarding.sql
ALTER TABLE users
  ADD COLUMN digest_at      TIME NOT NULL DEFAULT '08:00',
  ADD COLUMN preferred_name TEXT,
  ADD COLUMN onboarded_at   TIMESTAMPTZ;
```

`TIME`, not `TIMETZ`. The value is read in the developer's own timezone, which
`users.timezone` already holds; a zone-carrying time would be two sources of
truth for one question.

`digest_at` is `NOT NULL` with a default so `due_users` never has to handle a
missing value, and so existing rows keep today's behaviour exactly.

`onboarded_at` is nullable, and NULL means "has never been asked".

### Model and repository

`core/models.User` is a frozen dataclass and gains three fields:
`digest_at: time`, `preferred_name: str | None`, `onboarded_at: datetime | None`.

`core/db/users.py::_COLUMNS` must grow to match. It backs `create_user`'s
`RETURNING`, `get_by_wa_id` and `list_users`, so a field added to the dataclass
but missed here is a `KeyError` inside `_row_to_user` on every login path.

## 3 · The trigger and the stamp

`handle_turn` already reads `history` immediately before building the prompt, so
the condition needs no extra query:

```python
system = await build_system_prompt(conn, user, first_turn=not history)
```

`messages_db.recent` excludes the current batch via `exclude_wa_ids`, so an
empty result means *this is their first-ever turn*.

The fragment is appended when `first_turn and user.onboarded_at is None`. Both
halves are checked: an admin may have stamped `onboarded_at` out of band, and a
developer whose history was pruned should not be re-interviewed.

**Do not use `last_inbound_at` as the signal.** It looks right and is wrong:
`receive()` calls `touch_inbound` at the webhook, before the turn is queued, so
it is already stamped by the time `build_system_prompt` runs.

### Stamping

`onboarded_at` is set in the same transaction that logs the delivered assistant
reply, and **only when the reply was actually delivered**. If the send was
rejected, the developer was never asked, so they must not be recorded as asked —
the next turn should ask again.

This is the codebase's existing rule ("nothing is recorded unless the send
actually landed", `butler.py` and `digest.py` both) applied to one more column.

## 4 · Why timezone is deliberately not in the tool

`core/admin.py:13`'s `_timezone` validator documents what a bad
`users.timezone` costs: `ZoneInfo()` raises inside `due_users`, so one typo
meant *nobody* in the company got a digest, ever, with one `digest run failed`
line every fifteen minutes; and it raises inside `build_system_prompt`, so that
developer got the apology text for every message they sent, forever. The
docstring concludes that a typo at the CLI must not be able to do that.

A `set_preferences` tool that accepted a timezone would hand the model a pen for
that column — a new, unvalidated boundary into the exact field with that history.

Instead the interview **states** the timezone rather than offering to change it:

> "Your morning list is set for 08:00, America/New_York — want a different hour?"

A wrong zone is therefore surfaced to the person who would notice, and fixed by
an admin through the validated CLI path. Generalising to firms with developers
in several cities does not change this: `add-user --tz` already handles it, and
the interview still reads the value back for confirmation.

## 5 · The tool

New `gaia/capabilities/onboarding/` — a directory, a `Capability`, one import in
`capabilities/__init__.py`, matching the existing pattern exactly.

```
set_preferences(digest_at?: "HH:MM", preferred_name?: str) -> dict
```

- `digest_at` parsed strictly as 24-hour `HH:MM`; anything else is rejected with
  a message the model can act on, not a raised exception.
- `preferred_name` trimmed and length-capped.
- Returns **what actually changed**, so the model confirms accurately instead of
  claiming a change it did not make. This follows `save_meeting`, whose result
  exists so the reply can say what was skipped: "Told, not silently dropped —
  and told accurately. Every clause here is a claim about where the data is, so
  each one has to be true."

Prompt fragment (stable half, always present):

> The developer can change what time their morning list arrives, or what you
> call them, at any point — use `set_preferences`. Times are in their own
> timezone. Confirm back the time you actually set, not the time they asked for,
> in case it was adjusted.

## 6 · Interview copy (volatile half, first turn only)

The hard requirement is that it **must not hijack the first message**. If the
first thing a developer ever sends is a photo of site notes, those notes get
filed and answered first; the question comes after, in the same reply.

> This is the first message {name} has ever sent you. Deal with what they sent
> first — file it, answer it, exactly as you normally would. Then, at the end of
> the same reply, introduce yourself in one line and ask what time they would
> like their morning follow-up list, mentioning that it is currently set for
> {digest_at} in their timezone ({tz}). Ask once. If they do not answer, do not
> raise it again.

### On naming the timezone in copy

The fragment passes the raw IANA name, matching `CONTEXT_PROMPT`, which already
tells the model "Today is {today} in {name}'s timezone ({tz})". For a Miami firm
that renders as "America/New_York", which is correct but reads oddly. Deriving a
friendlier city name is a separate concern and would need a mapping table no
other part of the system wants; if it becomes worth doing, it belongs in one
helper used by both prompts, not invented here.

## 7 · Tick interval

`jobs/digest.py::main` sleeps 900s from whenever the container started, so ticks
do not land on quarter-hours. A developer who picks 09:15 would be served at the
first tick at or after 09:15 — up to ~14 minutes late.

Drop the sleep to **300s**. The tick interval has no bearing on correctness —
`last_digest_on` makes the loop idempotent, which is the whole reason a poll was
chosen over cron — so this is purely a latency knob. Worst case becomes ~5
minutes late. Cost is one cheap `due_users` SELECT, 288 times a day instead of
96.

`due_users` also calls `list_users()` and then a separate
`SELECT last_digest_on` per user, an N+1 that one query would collapse. Trivial
at this scale either way; fold it in while the file is open.

### Breaking change to the live tests

`tests/live/test_live_digest.py` imports `SEND_HOUR`, which this work replaces
with a column default. `_zone_past_send_hour` and
`test_selection_is_timezone_aware_at_a_fixed_instant` must be updated in the
same change, not afterwards.

## 8 · Testing

Hermetic:

- `due_users` honours `digest_at` — selected at the minute, excluded before it.
- The migration applies to a database that already has rows, and those rows keep
  08:00.
- `set_preferences` rejects a malformed time and accepts `HH:MM`.
- The trigger fires on empty history and does not fire on a second turn.
- `onboarded_at` is stamped on a delivered reply and **not** stamped when the
  send was rejected.
- `_COLUMNS` round-trips all three new fields through `create_user`,
  `get_by_wa_id` and `list_users`.

Live (`-m live`), in order of value:

1. **The real model files the notes and asks the question in the same turn.**
   This is the "doesn't hijack" guarantee and only a real model can be caught
   getting it wrong.
2. The real model calls `set_preferences` correctly from "make it 9:15", and
   confirms back the time it actually set.
3. A digest goes out at a non-default `digest_at`.

## 9 · Explicitly out of scope

**The `leads` → projects reframe.** Gaia's users are developers, not brokers.
`lead_status` is `new/active/under_contract/closed/lost/dormant` and the leads
fragment says "someone who might transact" — a buyer/seller pipeline. A
developer tracks parcels and projects through stages (evaluating, under contract
on land, entitlement, permitted, under construction, delivered), and
`leads.contact_id` is `NOT NULL`, which encodes "a lead is a person". A site
under evaluation may have no person attached at all.

That is a structural mismatch, not vocabulary, and it is larger than this piece
of work. Filed here so it is not mistaken for an oversight.

The developer-facing **vocabulary** fix (prompt copy and the `role` default) was
done separately in `migrations/002_developer_role.sql` and is not part of this
spec.
