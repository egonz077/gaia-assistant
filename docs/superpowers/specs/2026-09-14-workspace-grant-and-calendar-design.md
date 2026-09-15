# Workspace increment 2 — the grant and the calendar

**Status:** design, approved in conversation 2026-09-14. No implementation plan
yet.
**Evidence:** every API claim here was measured, not read. See
`research/2026-09-14-google-workspace.md`; section references below point at it.

## What this builds

Capabilities 2–5 of the five asked for, plus the OAuth foundation both halves
need:

- **The grant.** Consent over a browser, bound to a WhatsApp identity; refresh
  tokens stored encrypted.
- **Scheduling** with a Google Meet link.
- **Calendar invites**, behind an approval gate that a confused model cannot
  talk its way past.
- **Correlation** between calendar events and leads/commitments, both
  directions.
- **Conflict detection** surfaced in the morning digest.

**Out of scope, deliberately.** Capability 1 — drafting email — is its own
increment. It is independent of everything here except the grant, and it drags
in two more restricted scopes and a second consent round. One decision is
already taken for it and recorded so it is not re-litigated: a Gaia-written
reply-all draft arrives **pre-addressed, with no special handling**, accepting
the risk that research §1 names.

## Decisions, and what they rest on

| Decision | Why |
|---|---|
| **No mirror of Google state into Postgres** | Conflict logic runs in Python over live API responses. A `calendar_events` table would carry colleagues' meeting titles into a table that must then declare a `visibility` and inherit it from something — a subsystem, not a table, and exactly the trap this project's second rule warns about. Quota is three orders of magnitude away (research §7), so there is nothing to economise on. |
| **Own calendar only** | Removes domain-wide delegation entirely. For a system holding a client book, not possessing a key that impersonates any user in the organisation is worth more than the feature it would buy. |
| **Scopes: `openid`, `email`, `calendar.events.owned`** | Confirmed to permit attendees and a Meet link (research §3). It also grants reads — "**See**, create, change, and delete events on Google calendars you own" — which is what correlation needs. `calendar.freebusy` was considered and rejected: held *alongside* `events.owned` it restricts nothing, because the same token already has full read on the calendar. It is a tidier endpoint, not a boundary, and not worth another line on a consent screen Google already describes badly. |
| **Conflicts = overlaps + no room for what's due** | Chosen over calendar-only overlaps. The second half is what makes the digest worth reading. |
| **Addresses: ask once, then remember** | Explicit and predictable. Harvesting them from `gmail.metadata` envelopes was available and rejected — building a client book out of mail metadata is not something to do quietly. |

## 1. The grant

Consent happens in a browser. Gaia knows people only by `wa_id`. Nothing links
the two, and that is the whole problem this section solves.

### Flow

1. Developer asks for something calendar-shaped with no account connected.
2. Gaia sends `https://{DOMAIN}/oauth/google/start?t=<token>` over WhatsApp. The token
   is signed, bound to one `user_id`, and expires in 10 minutes.
3. `/oauth/google/start` validates the token and 302s to Google, carrying a fresh
   `state` stored against that `user_id`.
4. `/oauth/google/callback` validates `state`, exchanges the code, runs the checks
   below, and stores the refresh token encrypted.

Two new routes in `gaia/main.py` — the first that are neither `/health` nor
`/webhook`. No new infrastructure: the droplet already terminates TLS for the
webhook.

### `/oauth/google/start` must consume nothing

WhatsApp builds link previews by fetching URLs. If `/oauth/google/start` consumed the
one-time token, Meta's fetcher would burn it before the developer ever tapped
the link, and every connect attempt would fail with nothing in the logs to
explain it.

So `/oauth/google/start` validates and redirects, consuming nothing. A preview fetcher
receives a 302 to Google and achieves nothing, because consent needs a human.
The token dies by TTL, or when a callback succeeds.

### Where the consenting address comes from

**Corrected 2026-09-15, after the first whole-branch review.** This section
originally required the equality check below without saying where the address
came from, and the scope list held no identity scope — so the callback could
not learn the address at all and refused every connect. Research §9 carries the
full post-mortem.

The flow requests `openid email` alongside the calendar scope. The token
endpoint then returns an `id_token` carrying `email` and `hd`, so no second HTTP
call is needed, and **the domain check uses the `hd` claim rather than a string
suffix on the address** — Google states that claim can be trusted because it
arrives inside a security token from Google, which a suffix match on an
address never was. Both scopes are non-sensitive and do not affect the Internal
exemption in §2.

### Who is allowed to finish the flow

The link is a bearer credential for ten minutes, so the callback refuses
everything that is not exactly right:

- **`users.email` must already be set** — by the admin CLI, at user creation.
  The consenting Google account must equal it.
- **The `hd` claim must equal `gaiagroupdevelopment.com`** — Google's signed
  assertion of the account's Workspace domain, which is also precisely what
  keeps the Internal exemption true (research §2).

The precondition matters more than it looks. If the callback *populated*
`users.email` on first connect, then whoever used the link first would define
the answer — and a colleague with a forwarded link passes the domain check. The
consequence is not abstract: Gaia would then create that developer's events on
the attacker's calendar, carrying client names and attendees outward. Requiring
the address up front makes the check a pure equality test with no first-use
window.

**The precondition needs a mechanism, and currently has none.** `admin.py`
takes `add-user --name --phone --role --tz`; there is no email anywhere. So this
increment also adds `--email` to `add-user` and a `set-email` subcommand — and
the second is not optional, because every user who already exists predates the
column. Without a backfill path, nobody can connect a calendar after this
deploys. The deploy note says so explicitly: **migrate, then set an address for
each existing developer, then announce the feature.** In that order, or the
first thing anyone tries fails.

### Storage

`google_accounts`, keyed by `user_id`: the Google email, the encrypted refresh
token, the scopes actually granted, `granted_at`, `revoked_at`.

**No `visibility` column**, for the same reason `messages` has none — the
migration says it there: a user's thread is always owner-only. A token is
stronger still: nobody may read a colleague's at any visibility. Omitting the
column keeps the table out of `DOMAIN_TABLES` and out of `test_isolation.py`
without an exemption, because `test_migrate.py` asserts those sets match
exactly. Its reads go in `OWNERSHIP_SCOPED`, and the reason is the honest one:
a token answers *whose account is this*, which is ownership, never who may see
it.

Recording granted scopes is not bookkeeping. The email increment adds two more
scopes and a second consent round; a tool needs to know whether the grant it
holds actually covers what it is about to attempt, rather than finding out from
a 403.

### Encryption

New dependency: `cryptography`. Fernet, key in `.env` as `GOOGLE_TOKEN_KEY`.

Worth being honest about what this buys. Against someone who owns the droplet:
nothing, since they hold both secrets. It is aimed at the realistic leak — a
database dump or backup leaving the box — where the key is not in the dump.
Access tokens are never stored; they are refreshed on demand.

### Revocation

A refresh that returns `invalid_grant` sets `revoked_at` and nothing else. The
next calendar tool call reports that the connection lapsed and offers a fresh
link. A silent retry loop against a revoked grant is how an integration becomes
invisible noise in the logs.

## 2. The calendar capability

A new `gaia/capabilities/calendar/`, registered like `leads` and `meetings`.

| Tool | Reaches a third party? |
|---|---|
| `check_availability(from, to)` | no |
| `create_event(summary, start, end, with_meet, lead_id?, commitment_id?)` | **no — created bare, no attendees** |
| `propose_invite(event_id, emails[])` | no — writes a pending row, returns the list to read back |
| `confirm_invite(pending_id)` | **yes, and only this one** |
| `cancel_event(event_id)` | only to remove |

### What the tools take and return

**`create_event` takes local wall-clock times and applies `user.timezone`
itself.** It never accepts a bare timestamp and hopes. This is the single most
likely place for a real bug: `today_line()` exists because the model called
2026-09-11 "Friday" in one turn and "Thu" in the next, and a model that
unreliable about weekdays should not be trusted to attach a UTC offset across a
DST boundary. The timezone comes from the database, not from the model.

**`check_availability` returns intervals only** — its handler strips summaries
and attendees before returning. Not a security boundary (the token can read it
all either way), but there is no reason to spend the model's context on data the
question does not need, and "busy 10:00–11:00" is easier to reason about than a
wall of event objects.

### Why the event is created bare

`sendUpdates` looks like the approval gate and is not. Measured on a real
external account (research §4): an event created with an attendee and
`sendUpdates` omitted was **already on their calendar, Meet link and all,
before any email existed**. It suppresses the notification, not the intrusion —
and the absence of an email makes it more disconcerting, not less, because
nothing explains it.

So the event is created with no `attendees` at all. It is then purely the
developer's own calendar entry, invisible to anyone else, and attendees are
patched in only after a human agrees.

### The gate must not be a prompt rule

The weak version: the model reads back the addresses, the human says yes, the
model calls `add_attendees(event_id, emails)`. That trusts the model to pass
the same list it read back — the exact class of guarantee that made
`gmail.compose` unacceptable in research §1.

The durable version removes the possibility. `propose_invite` writes a
`pending_invites` row holding the event and **the exact address list**.
`confirm_invite` takes only a `pending_id` and sends **what was stored**. The
model cannot smuggle a different recipient into the confirmation, because
confirmation accepts no recipients. Single-use, and expiring — an approval from
Tuesday must not fire on Friday.

`pending_invites` follows `messages`: no `visibility` column, reads in
`OWNERSHIP_SCOPED`, because a pending invite is one person's workflow rather
than org content.

**What the gate does not do.** It makes it impossible for the model to confirm a
*different* list than the human saw. The addresses still originate with the
model, so a *wrong* list that the human approves carelessly reaches the client
exactly as intended. That is inherent — a human approving a list they did not
read is not something a schema can prevent — but it is the reason the read-back
should name people, not just addresses: "Dalila Serrao and two others at
Arquitectonica" is checkable at a glance in a way that seven raw addresses is
not.

**When a pending invite expires**, the bare event stays on the developer's own
calendar with no attendees. Nothing cleans it up, and nothing should: it is
their event, it reached nobody, and deleting something a person can see without
being asked is worse than leaving it.

### Details that bite if unplanned

- **Meet idempotency.** `conferenceData.createRequest` carries a `requestId`;
  derive it from the event so a retry does not mint a second conference.
- **`conferenceDataVersion=1`** or the conference block is silently ignored —
  no error, just no Meet link.
- **A pooled connection now spans a Google call.** `registry.dispatch` gives
  each handler its own transaction, and the HTTP request happens inside it. The
  docstring there is explicit that no connection should be held across the
  model's round-trips; this is adjacent rather than identical, but a slow Google
  call ties up a pooled connection. Stated rather than deferred: the pool is
  `max_size=8`, concurrency is bounded by how many developers are mid-turn at
  once — realistically one to three — and a Calendar call is a few hundred
  milliseconds. Eight is ample. This is written down so that whoever first sees
  pool exhaustion knows the assumption that was made, rather than re-deriving it.
- **No account connected is not a hidden capability.** The tools stay visible
  and return "connect your calendar first". Hiding them via
  `Capability.visible_to` would make Gaia claim it cannot do calendars at all,
  which is false.

## 3. Correlation, both ways

**DB → Calendar:** a nullable `calendar_event_id TEXT` on `leads` and
`commitments`. One column, not a join table, and deliberately meaning *the
event for the current next action* rather than a history — history of what
already happened lives in `meetings`, and a second home for the same truth is
how the two drift apart.

Both tables are already in `DOMAIN_TABLES`, so the column inherits `visibility`
correctly on both paths with nothing new to cascade.

**Calendar → DB:** `extendedProperties.private` carries `gaia_lead_id` /
`gaia_commitment_id`, and `events.list` filters on them directly via
`privateExtendedProperty`. The reverse lookup is therefore a server-side filter
and the link is exact — no fuzzy-matching event titles against contact names,
which would eventually pair the wrong client with the wrong deal.

**Two traps.** `extendedProperties.private` is per-*copy*: the developer's copy
carries it, an attendee's does not. Harmless under own-calendar-only, but it is
not a shared identifier and nothing may treat it as one. And the calendar is not
a database — people delete events. A `calendar_event_id` pointing at a 404 is
normal, not a failure: the read clears it and moves on.

## 4. Conflicts, in the digest

`gaia/jobs/digest.py` already runs each morning and already asks a per-user
question. One `events.list` per connected user per morning. No new scheduler, no
`watch` channels, no renewal cron — push notifications carry no payload anyway
and have no automatic renewal, so they cost a channels table and a silent
failure mode to save an API call this system has no reason to save.

**Computed in Python, never asked of the model.** This codebase already carries
the scar: `today_line()` exists because the model called 2026-09-11 "Friday" in
one turn and "Thu" in the next. Interval arithmetic over a day of events is
harder than naming a weekday. The job computes; `digest_model` writes the
sentence and nothing else.

- **Overlaps:** intersecting intervals, with `singleEvents=true` so a weekly
  standup expands into instances rather than being one row that says nothing
  about Tuesday.
- **No room for what's due:** Gaia does not know how long "send comps to
  Marcel" takes, and inventing forty minutes would make it confidently wrong. It
  reports the two facts it has — free time in the working day after subtracting
  busy blocks, and the count of obligations due. *"You owe three things today
  and have 40 minutes clear between 9 and 6."* The human judges.

Working hours are hardcoded 9–18 in `user.timezone` with a comment saying so. A
`users.working_hours` column is the obvious next step and nothing needs it yet.

**Ownership, not visibility** — the digest filters on `user_id`, exactly like
`commitments.open_for` and `leads.due_for`. Otherwise one developer is nagged
about a colleague's day.

**The calendar must never break the digest.** Google erroring, or a revoked
grant, drops the conflict section and logs it; the digest still sends. The
digest is this product's daily heartbeat, and a calendar outage silencing it
would be a worse bug than the one this feature fixes.

**A revoked grant is said out loud, once.** The digest is usually what discovers
`invalid_grant`, running unattended — and telling nobody means the developer's
digest is quietly less useful for however long it takes them to next ask Gaia
something calendar-shaped, which could be a week. So the first digest after
`revoked_at` is set carries one line offering a fresh link, and subsequent ones
do not. A daily nag about an integration someone may have revoked deliberately
is its own failure.

## 5. Schema — migration 006

```
users              + email TEXT UNIQUE          -- precondition for connecting
leads              + calendar_event_id TEXT
commitments        + calendar_event_id TEXT

google_accounts    user_id PK -> users, google_email, refresh_token_enc,
                   scopes TEXT, granted_at, revoked_at        -- no visibility
pending_invites    id, user_id -> users, event_id, emails TEXT[],
                   created_at, expires_at, confirmed_at       -- no visibility
```

Both new tables carry a comment saying why they have no `visibility` column, in
the style of the one on `messages`.

**Also in scope, and easy to miss because it is not schema:** `admin.py` gains
`--email` on `add-user` and a `set-email` subcommand. §1 depends on it, and
without it the migration lands a precondition nothing can satisfy.

## 6. Testing

TDD throughout. **Write this one first**, because it is the finding that cost a
probe and a real event on someone's calendar to learn:

> Every outbound Calendar **create** payload contains no `attendees` key.

A tripwire in the house style, so a future contributor helpfully adding
attendees to `create_event` meets a test that explains why they cannot, rather
than discovering it on a client's calendar.

**`confirm_invite`'s patch is exempt, and the exemption is the point** — that
call is the one place attendees are supposed to appear, downstream of a human
who saw the list. The test must therefore assert on the create path
specifically. Generalised to "no Calendar call may send attendees" it would
block the feature it exists to protect, which is a plausible enough mistake to
be worth naming in the test's own docstring.

Then: A's token cannot attach B's account; the domain check; `users.email` as a
hard precondition; `state` mismatch refused; `/oauth/google/start` idempotent under a
link-preview fetch; encryption round-trip plus an assertion the stored column is
not plaintext; `pending_invites` single-use and expiry; `confirm_invite` using
stored addresses — mostly a schema assertion, since it accepts none; overlap
detection and free-time arithmetic; digest survives a Google failure.

**Seams, not switches.** The event fetch is a required parameter, the way
`usage.record` takes `pool`. No `if TESTING`, no env var only tests set.

**Two known traps apply directly.** Capability tools get their own transaction
via `registry.dispatch`, so the `ana` fixture must be committed before any
calendar tool test — the same foreign-key death `usage.record` has. And `tx()`
leaves `row_factory` on the pooled connection, so these tests set it explicitly.

Google calls go in the `-m live` tier: not billable, but they need credentials
CI does not have, and the hermetic tier stays hermetic.

## 7. What this does not do

- **Drafting email.** Separate increment, separate spec, second consent round.
- **Team-wide free/busy.** Own calendar only, by decision.
- **Anything push-based.** The digest is the only proactive surface.
- **Duration estimates.** Reports free time and obligation counts; never guesses
  how long work takes.
- **Address harvesting.** `gmail.metadata` makes it possible and it was
  rejected.

The constraint most likely to change under this design is not technical: the
Internal OAuth configuration holds only while every user is on the Workspace
domain, and one contractor on a personal Gmail forces External — where these
scopes are merely sensitive, but the email increment's are restricted and pull
in an annual CASA assessment.
