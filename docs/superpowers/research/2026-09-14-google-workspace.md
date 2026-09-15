# Google Workspace integration — research

**Status:** research. No design, no spec. Two questions settled empirically
against a real mailbox on the Gaia Workspace domain (2026-09-14) — see §1 and
§2; everything else is documentation, not decision.
**Question:** what does it take for Gaia to draft email it may never send,
create Meet-backed calendar invites, tie events to leads and commitments in
both directions, and flag conflicts before they bite?

**Capabilities asked for** (2026-09-14):

1. Create draft emails; prohibited from sending — "it should have capacity to
   write draft but only user can hit send".
2. Schedule meetings with Google Meet.
3. Create Google Calendar invites.
4. Correlate leads and commitments with the calendar, **both ways**.
5. Identify conflicts ahead of time.

Conflict detection reads **only the requesting developer's calendar**. That
decision is already taken and it removes an entire subsystem — see §3.

All scope and API facts below were checked against Google's live documentation
on 2026-09-14, not recalled.

---

## 1. The finding that shapes everything: there is no draft-only Gmail scope

`users.drafts.create` requires one of exactly three scopes. `users.drafts.send`
requires one of exactly the same three.

| Scope | Creates a draft | Sends | Google's verbatim description |
|---|---|---|---|
| `https://mail.google.com/` | yes | yes | "Read, compose, send, and permanently delete all your email from Gmail." |
| `.../auth/gmail.modify` | yes | yes | "Read, compose, and send emails from your Gmail account…" |
| `.../auth/gmail.compose` | yes | yes | "Manage drafts and send emails." |
| `.../auth/gmail.send` | **no** | yes | "Send email on your behalf." |
| `.../auth/gmail.insert` | **no** | no | "Add emails into your Gmail mailbox." |

The narrowest scope that can write a draft is `gmail.compose`, and Google's own
one-line description of it ends in "and send emails". `users.messages.send`
accepts it too.

**Consequence: "only the user can hit send" cannot be enforced by the
credential.** Any token that can put a draft in the mailbox can also post it.
This is not an oversight we can route around — it is how Gmail's scope lattice
is cut.

Note for anyone who re-checks this: a plausible-sounding summary of the
`drafts.create` reference page claims `gmail.compose` "enables draft creation
and editing but does not grant sending authority". That is wrong, and it is
wrong in the direction we would like to be true. The `drafts.send` and
`messages.send` reference pages both list `gmail.compose`. Read the send
endpoints, not the create endpoint.

### The way out: `gmail.insert`, and it works — measured, not inferred

`gmail.insert` grants `users.messages.insert`, which "directly inserts a message
into only this user's mailbox similar to IMAP APPEND, bypassing most scanning
and classification. **Does not send a message.**" It appears on no send
endpoint's scope list.

Whether a message inserted under the `DRAFT` label becomes a *real* draft — one
that opens in Gmail's composer — is documented nowhere: Google specifies neither
the permitted label set on insert nor `DRAFT`'s behaviour there. Gmail models a
draft as its own resource (`users.drafts`) wrapping a message, so inserting the
message without the wrapper could plausibly have produced something that lists
but does not compose.

**It does not. It composes.** Probed 2026-09-14 against a live mailbox with a
token holding exactly one scope, `https://www.googleapis.com/auth/gmail.insert`,
and nothing else:

| Probe | Result |
|---|---|
| Granted scope on the token | `…/auth/gmail.insert` alone — nothing wider crept into the grant |
| `messages.insert`, `labelIds: ["DRAFT"]` | **HTTP 200**, returned `labels=['DRAFT']` — the label is accepted and persists |
| `drafts.create` | **HTTP 403** — "Request had insufficient authentication scopes." |
| `messages.send` | **HTTP 403** — "Request had insufficient authentication scopes." |
| Opening it in Gmail | A **real composer**: To, Subject and body populated, Send button rendered, and Gmail's own "someone@example.com is outside your organization" external-recipient banner — a warning it only raises in a live compose window |

**So the send ban can be a property of the credential rather than a promise the
model keeps.** A token scoped to `gmail.insert` alone can stage mail in a
developer's Drafts folder and cannot, by any call, post it. The human's Send
then happens in Gmail's own UI under their own session, which never involves our
token at all — which is exactly why the token's inability to send costs the
feature nothing.

Two caveats worth carrying into the spec:

- **The Send button was observed, not clicked.** Nothing about Gmail's own
  compose UI is in question here, but no one has watched the message leave.
- **Gaia cannot read back its own drafts.** `drafts.list` needs a read scope
  `gmail.insert` does not grant, so whether the inserted message also appears as
  a `users.drafts` *resource* is untested. Gaia can write and forget; it cannot
  list, revise or delete what it wrote without widening the grant — and widening
  it to `gmail.compose` or `gmail.readonly` re-arms the send path or opens the
  mailbox. If a future feature wants "edit the draft you made me yesterday",
  that is a scope decision, not an implementation detail.

### Reply-all: `gmail.metadata` buys threading without re-arming send

A draft is rarely a new message. Replying — reply-*all*, to a live client thread
— needs three things `gmail.insert` cannot supply, because it grants no reads at
all: the `threadId`, the original `Message-ID` for `In-Reply-To`/`References`,
and the From/To/Cc lists. Google requires all of it or the message is not part
of the thread: "the `References` and `In-Reply-To` headers must be set in
compliance with the RFC 2822 standard", "the `Subject` headers must match", and
the `threadId` must be on the resource.

`gmail.metadata` — "headers, but not the message body or attachments" — appears
on `users.messages.get` and on **no send endpoint**. Probed 2026-09-14 with a
token holding exactly `gmail.insert` + `gmail.metadata`, against a real client
thread:

| Probe | Result |
|---|---|
| `getProfile` | **200** — Gaia can learn its own address, so stripping self from reply-all needs no configuration |
| `messages.list` **with** `q` | **403 — "Metadata scope does not support 'q' parameter."** |
| `messages.list` without `q` | 200 |
| `messages.get?format=METADATA` | 200 — Message-ID, Subject, From, To, Cc, Date |
| Reply-all assembled from headers | 7 recipients + 1 Cc, own address correctly excluded, no body ever read |
| `insert` with `threadId` + RFC 2822 headers | **200**, `threadId` matched — landed inside the thread, not beside it |
| `drafts.create` / `messages.send` | **403 / 403** — the send ban survived the wider grant |

**So the shape is `gmail.insert` + `gmail.metadata`:** Gaia can compose a correct
reply-all to a client thread while holding a credential that can neither send it
nor read a word anyone wrote.

**The `q` refusal is a product constraint, not a detail.** Gaia can enumerate
recent mail; it cannot search it. "Reply to the Highland Park thread" cannot be
Gaia finding the thread — the developer has to pick it, or Gaia has to have been
told the `threadId` earlier by some other means. Any design that assumes search
is available is designing against a 403.

### The guarantee's edge: it stops sending, not staging

Worth stating plainly because the probe made it concrete. The reply-all draft
above landed in a live thread, pre-addressed to eight external parties at four
firms. Our token could not send it. **A human opening Gmail and clicking Send
could, instantly, without reading it.**

The credential guarantee is real and worth having, but it bounds the wrong
actor's mistake. It prevents the *model* from sending; it does nothing about the
model preparing something dangerous for a *person* to send. For a feature whose
entire purpose is putting words in front of someone who will send them, that is
the residual risk, and it is a drafting-conventions problem rather than a scopes
problem: what the first line of a Gaia-written draft says, and whether a draft
into a client thread should arrive pre-addressed at all or with an empty To line
the developer fills in deliberately.

### Where enforcement could actually live

Ordered by what each one genuinely guarantees, not by effort:

| Mechanism | Guarantee if the model is confused, or a photographed note contains an injection |
| --- | --- |
| `gmail.insert` only | **Real, and confirmed above.** The credential cannot send. Nothing the model emits, and no injected instruction in a photographed note, changes that. |
| Two tokens: `gmail.compose` held by a component with no send code path | Partial. No send call exists to reach, but the token in the database can send if any future code calls it. |
| One `gmail.compose` token, no send tool registered | Weak. `registry.dispatch` already re-checks visibility and refuses unknown tool names, so the model cannot invent `send_email`. But the guarantee is "we did not write that function", which every future contributor can undo. |
| `BASE_PROMPT` says never send | None. This is the status of "Never contact third parties" today, and it is a behavioural rule, not a boundary. |

The asked-for capability — Gaia writes it, the human presses Send in Gmail —
means Gaia needs **no send path at all**. The feature never wanted the
capability the scope lattice tries to force on us, and `gmail.insert` is the one
row of the table where we do not have to accept it anyway. Take it.

---

## 2. Gmail's scopes are *restricted*, which makes the Internal assumption load-bearing

`backlog.md` §7 flags "the Internal-OAuth assumption needs confirming". The
Gmail capability sharpens that from a nuisance into the deciding constraint.

Google classifies these as **restricted** scopes — the highest tier, above
"sensitive":

- `https://mail.google.com/`
- `gmail.readonly`, `gmail.metadata`, `gmail.modify`
- **`gmail.insert`**
- **`gmail.compose`**
- `gmail.settings.basic`, `gmail.settings.sharing`

Both candidates from §1 are restricted. There is no draft-writing path through a
merely-sensitive scope.

An app requesting restricted scopes must pass verification, and verification for
restricted scopes pulls in the **CASA security assessment** — the App Defense
Alliance's Cloud App Security Assessment framework. Google's words: apps must
"undergo an annual security assessment", it must be "revalidated every year",
and "the required assurance level is dynamic and may increase based on changes
in your user base or data-handling practices". Google's Trust and Safety team
initiates it.

Verification is **not needed** for, among others, apps "used exclusively within
a Google Workspace or Cloud Identity organization" — an Internal app is "not
subject to the unverified app screen or the 100-user cap".

**The inference:** the assessment is a requirement *of* the verification
process, and Internal apps are exempt *from* verification, so an Internal app
requesting a restricted scope should face neither. Google's security-assessment
page does not say this in so many words — it simply never discusses internal
apps, and the exemption page never discusses the assessment.

**Tested, and it holds.** A project on the Gaia Workspace org, consent screen
set to Internal, requesting the restricted scope `gmail.insert`: consent
completed and the token issued. No unverified-app interstitial, no verification
prompt, no assessment — verification is never entered, so the thing that would
trigger CASA never fires. An Internal app can hold a restricted Gmail scope
today, on this domain.

One observation worth keeping, because it is a wart the real rollout inherits:
the consent screen's wording was **"gaia-probe is trying to access something"**.
Google has no friendly description for `gmail.insert`, so it falls back to a
string that tells the consenting developer nothing at all. Whatever Gaia shows
people before sending them to that screen has to carry the explanation itself,
because Google's will not.

### What External would actually cost

The backlog's scenario — one contractor on a personal Gmail — is now much more
expensive than "sensitive-scope review, privacy policy, demo video":

| Path | What happens |
|---|---|
| Internal | No verification, no assessment, no unverified-app screen, no user cap. Every user must be on Gaia's Workspace domain. |
| External, Testing | Capped at 100 test users, added by hand. **Refresh tokens expire 7 days from consent.** Every developer re-authorises weekly, forever. |
| External, In Production, restricted scopes | Verification *plus* annual CASA revalidation, at a dynamic assurance level. Refresh tokens become long-lived. |

The 7-day expiry confirms the backlog's claim and is worth stating plainly: it
applies to External apps in Testing unless the only scopes requested are a
subset of name, email and profile. Ours are not.

**Observed in the console, 2026-09-15:** Google classifies
`calendar.events.owned` as **sensitive**, and `gmail.insert` as **restricted** —
the console groups them under exactly those headings. That refines the paragraph
below rather than contradicting it, and the refinement matters: *sensitive* is
not *free*. An External app needs a verification review for a sensitive scope
too; what it avoids, relative to a restricted one, is the annual CASA
assessment.

So if a contractor on a personal Gmail ever forces External, the calendar half
needs a verification review as well. It is the cheaper half, not the exempt one.

**The calendar capabilities survive External far better than the email one
does.** Calendar's scopes are sensitive rather than restricted. If the contractor case ever
materialises, the cheap answer is likely to be that drafting email stays
Internal-only — a per-user capability, which `Capability.allowed_user_ids`
already expresses — rather than dragging the whole app through CASA.

---

## 3. Calendar: what each capability actually needs

Own-calendar-only is the decision, and it is the one that keeps this small. The
alternative — free/busy across the team — would have meant either every
developer authorising separately anyway, or a service account with domain-wide
delegation: an admin-console grant that lets a key impersonate any user in the
organisation without their consent. For a system holding a client book, not
having that key is worth more than the feature it would buy.

Narrowest scope per capability:

| Capability | Scope | Google's description |
|---|---|---|
| Create/modify events on the user's own calendars | `calendar.events.owned` | "See, create, change, and delete events on Google calendars you own." |
| Same, on any calendar they can access | `calendar.events` | "View and edit events on all your calendars." |
| Conflict detection (busy blocks only) | `calendar.freebusy` | "View your availability in your calendars." |
| Reading event detail to correlate | `calendar.events.readonly` or the writable `.owned` above | — |

`freebusy.query` also accepts `calendar.readonly`, `calendar`, and
`calendar.events.freebusy`, but `calendar.freebusy` is the narrowest and is the
only one whose consent string says nothing about reading content.

A note on `calendar.app.created` — "Make secondary Google calendars, and see,
create, change, and delete events" — which confines the app to a calendar it
created. Tempting for isolation, useless here: conflicts live on the primary
calendar, and an invite on a side calendar is not the invite anyone wanted.

The "primary" calendar is owned by the user, so `calendar.events.owned` covers
creating invites on it. **It also permits an `attendees` array — confirmed
2026-09-14**, with a token scoped to `calendar.events.owned` alone creating an
event with an external attendee and a Meet link. That was not obvious: the
consent string says "create, change, and delete events", not "and invite
people", and Google's create-events guide only ever demonstrates attendees
against the broader `calendar.events`. The narrow scope is sufficient; take it.

---

## 4. Meet and invites — and the trap in the approval gate

Creating a Meet link is one field, as the backlog expected. Set the
`conferenceDataVersion` request parameter to `1` — without it the API silently
ignores conference data — and supply a `conferenceData.createRequest` with
`conferenceSolutionKey.type` of `hangoutsMeet`. The `createRequest` carries a
caller-supplied `requestId`, which is the idempotency handle: the same id does
not mint a second conference.

Attendees are an array of `{"email": ...}` on the event.

`sendUpdates` controls the invitation emails: `all`, `externalOnly`, or the
default, which notifies nobody.

### The trap — confirmed on a real external account

`sendUpdates` looks like exactly the lever the approval gate needs: create the
event silently, show the developer the address list, then flip to `all` on
approval. It is not. Google's create-events guide says "the event you create
appears on all the primary Google Calendars of the attendees you included with
the same event ID", and that is precisely what happens.

Measured 2026-09-14. An event created with one **external** attendee (a personal
Gmail account, outside the Workspace domain), `conferenceData` for Meet, and
`sendUpdates` omitted entirely:

| Phase | What the attendee saw |
|---|---|
| Created, `sendUpdates` omitted | **The event and its Meet link, already on their calendar. No email.** |
| Patched, `sendUpdates: "all"` | The invitation email arrives |

**`sendUpdates` suppresses the notification, not the intrusion.** A client
watches a meeting with Gaia Group materialise on their calendar, with a Meet
link, before anyone has approved anything — and the absence of an email makes it
*more* disconcerting, not less, because nothing explains it.

This is easy to misread as a timing artifact — "the email was just slow" — and
it is worth naming the misreading, because the whole finding hides behind it.
Phase 1 was never going to send an email; the default is to notify nobody. The
finding is what happened *without* one.

The backlog's reasoning was "a deleted event is recoverable; an invitation to a
client is not". An event that appeared on a client's calendar and then vanished
is in between, and it is not a good place to be.

**Consequence for the design: the approval gate sits before the attendees
exist.** Create the event with the Meet link and no `attendees` — it is then
purely the developer's own calendar entry, invisible to anyone else. Show the
exact address list over WhatsApp. On approval, `patch` the attendees in with
`sendUpdates: "all"`. One event, one Meet link, and exactly one moment where
anything reaches a third party — downstream of a human saying yes.


---

## 5. Conflict detection ahead of time

"Ahead of time" is doing the work in that sentence, and it splits into two
questions that have different answers.

**Is this slot free?** — asked while scheduling. `freebusy.query` takes
`timeMin`, `timeMax` and an `items` list of calendars, and returns busy
intervals as start/end pairs. Times only, no titles, no attendees. That is
precisely the right shape: the narrowest scope, and a response that cannot leak
a colleague's client name even by accident. `calendarExpansionMax` caps at 50
and `groupExpansionMax` at 100; neither binds for a single developer.

**Does tomorrow already contain a problem?** — asked unprompted. Two mechanisms:

*Pull.* `events.list` with `timeMin`/`timeMax`, `singleEvents=true` (which
expands recurrences into instances — without it a weekly standup is one row that
answers nothing about Tuesday) and `orderBy=startTime`. For repeat checks,
`nextSyncToken` from the last page gives incremental sync, returning only what
changed. **`gaia/jobs/digest.py` already runs each morning and already asks a
per-user question**; this is the cheapest possible home for conflict detection
and it needs no new infrastructure at all.

*Push.* `events.watch` opens a notification channel against an HTTPS endpoint.
Two things make this a second increment rather than a first. The payload
"[does] not contain specific information about updated resources" — it is
headers only (`X-Goog-Channel-ID`, `X-Goog-Resource-State`), so every
notification costs an API call anyway. And channels expire with, in Google's
words, "no automatic way to renew a notification channel… you must replace it
with a new one by calling the `watch` method" — a renewal cron, a channels
table, and a silent-failure mode where notifications simply stop. The droplet
already terminates TLS for the WhatsApp webhook, so the endpoint requirement
(valid certificate, 2xx response) is already met; the lifecycle is the cost.

**Undefined, and the spec must define it:** what *is* a conflict? At least
three different things are hiding under the word — two events overlapping; a
commitment whose `due_at` falls on a day with no free time to do it; a lead
whose `next_action_at` collides with something. Only the first is a calendar
fact. The other two are the interesting ones and they are the reason §6 exists.

---

## 6. Correlating both ways

**Database → Calendar** is easy: the event id comes back from `events.insert`
and a column on `leads` / `commitments` remembers it.

**Calendar → Database** is the half that needs a mechanism, because Gaia has to
recognise its own events among everything else on a developer's calendar — and
better, find them without reading everything else.

`events.list` accepts `privateExtendedProperty`: "Extended properties constraint
specified as propertyName=value. Matches only private properties. This parameter
might be repeated multiple times to return events that match all given
constraints." So writing `extendedProperties.private.gaia_lead_id` at creation
makes the reverse lookup a server-side filter rather than a scan, and makes the
link unambiguous — no fuzzy matching of titles against contact names.

Two traps:

- **Extended properties are per-copy.** Each attendee holds their own copy of
  the event. "Private" means private to one calendar's copy. The developer who
  created it carries the property; a colleague added as an attendee does not.
  Given own-calendar-only, this is fine — but it means the property is not a
  shared identifier, and nothing should be built as though it were.
- **The calendar is not a database.** Users delete events. The durable link is
  the column in Postgres; the extended property is an index into the calendar,
  not a store. A lead whose event was deleted is still a lead.

### What this touches in our schema

- **`users` has no email column**, as the backlog notes. Binding a WhatsApp
  identity to the Google account that consented needs one, and without it
  nothing verifies that the account granting access belongs to the person we
  think is asking.
- **`contacts.email` is nullable and almost entirely empty.** The notes carry
  names. An invite needs an address. Still unsolved, still the thing most likely
  to make the feature feel stupid in practice, and still not a research question
  — it is a conversation-design question about how Gaia asks without
  interrogating someone over every lunch.
- **A `google_accounts` table** holding the refresh token, the granted scopes
  and the Google email. **It should deliberately have no `visibility` column**,
  like `model_calls`: it is infrastructure, not client data, and nobody may read
  a colleague's token at any visibility. `tests/test_scope.py` asserts that the
  set of tables carrying a `visibility` column equals `DOMAIN_TABLES` exactly,
  so omitting the column keeps it out of `DOMAIN_TABLES` and out of
  `test_isolation.py`'s parametrisation without an exemption.
- **Every read in `gaia/core/db/google_accounts.py` must go in
  `OWNERSHIP_SCOPED` with a reason**, since none of them will compose
  `visible()`. The reason writes itself and is the correct one: a token answers
  "whose account is this", which is ownership, never "who may see it".
- **Event-id columns on `leads` and `commitments`** are free — those rows are
  already visibility-scoped and inherit correctly.
- **`/oauth/google/start` and `/oauth/google/callback`** would be the first routes in
  `gaia/main.py` that are neither `/health` nor `/webhook`. Consent cannot
  happen inside WhatsApp, as the design spec anticipated.

---

## 7. Cost, at the volume we actually have

Calendar API: 1,000,000 requests per day per project, 10,000 per minute per
project, 600 per minute per user. "All standard use of the Google Calendar API
is available at no additional cost", with the caveat — worth a diary note — that
Google states charges for exceeding quota are planned to arrive later in 2026.

A handful of developers, a morning digest, and a few events a day sits three
orders of magnitude under the daily ceiling. Even a push-notification design
that re-reads on every change does not get close.

**Cost is not a constraint on this feature, and no architecture choice here
should be made to economise on API calls.** The real currencies are consent
(§2), scope classification (§1), and the blast radius of a mistake that reaches
a client (§4).

Gmail's quota model was not checked; at drafting volume it cannot plausibly
bind, but that is an assumption, not a verified number.

---

## 8. Google's hosted MCP servers — and why they are not the shortcut

Google now ships remote MCP servers for Workspace, enableable from the Cloud
console and easy to mistake for a way to skip all of the above. They are worth
the paragraph it takes to rule most of them out.

| Server | Tools | Scopes it demands |
|---|---|---|
| Gmail (`gmailmcp.googleapis.com/mcp/v1`) | `create_draft`, `list_drafts`, `get_thread`, `get_message`, `search_threads`, label management — **no send tool** | `gmail.compose` **and `gmail.readonly`** |
| Calendar (`calendarmcp.googleapis.com/mcp/v1`) | `create_event`, `update_event`, `delete_event`, `get_event`, `list_events`, `list_calendars`, `respond_to_event`, `suggest_time` | documented as `calendarlist.readonly`, `events.freebusy`, `events.readonly` |
| Universal Search (`workspacemcp.googleapis.com/mcp/v1`) | `search_corpus`, one tool | `gmail.readonly`, `drive.readonly`, `calendar.readonly`, `chat.messages.readonly` |

The Gmail server is the tempting one: Google has already built "compose but
never send", and maintains that boundary so we do not have to. But it is a
restriction on the **tool surface**, not on the **credential** — the grant still
contains `gmail.compose`, which §1 establishes can send. It is the same class of
guarantee as "we did not write a send function", merely maintained by a better
janitor. And it charges `gmail.readonly` for the privilege: full standing read
access to every developer's mailbox, bought for a feature whose entire ask was
to write a draft.

Which inverts the intuition. Ranked by what the credential can do:

| | Can the credential send? | Does it read the mailbox? | Who maintains the boundary |
|---|---|---|---|
| `gmail.insert` alone (§1) | **no** | **no** | Google's scope lattice |
| `gmail.compose`, our own tools | yes | no | us |
| Google's Gmail MCP | yes | **all of it** | Google |

**Building it ourselves is the narrower grant.** That is the opposite of how
hosted integrations usually go, and it is decisive for a system holding what
sellers said in confidence.

The Calendar server fails for a different and firmer reason. §4 establishes that
the approval gate must sit *between* the model and the call that adds attendees.
A tool the model invokes directly on Google's server leaves us nowhere to stand:
no WhatsApp confirmation, and no `registry.dispatch` either — so no `visible()`,
no ownership scoping, and nothing recorded in `model_calls`, which is the
observability this project has just finished building.

Universal Search is not a superset of anything. "Cross-corpus" means it searches
*across* products; it is one read-only tool, and it asks for four `.readonly`
scopes spanning mail, Drive, Calendar and Chat to provide it. It performs none
of the five capabilities.

**Also flagged:** Google's Calendar MCP page lists three read-only scopes while
advertising `create_event`, `update_event` and `delete_event`. Those tools
cannot work with those scopes. The documentation is wrong or incomplete, and
anyone reaching for that server should resolve it before trusting the rest of
the page.

The pattern across all three: **every hosted convenience here is paid for in
read scope.** Worth saying out loud, because the next such offer will look just
as much like a shortcut.

## 9. What this research does not settle

Everything that was blocking is now answered, each against live Google APIs on
the Gaia Workspace domain rather than from documentation:

| Settled | Where | How |
|---|---|---|
| The send ban can be structural | §1 | `gmail.insert` alone: draft composes, `drafts.create` and `messages.send` both 403 |
| ~~The scope set is complete~~ | — | **Never probed, and false.** `userinfo` needs an identity scope; see the correction in §9. Nothing in this table was checked against the *absence* of a capability. |
| Reply-all is possible without re-arming send or reading bodies | §1 | `+ gmail.metadata`: threaded reply-all built from headers, both send endpoints still 403 |
| Internal exempts a restricted scope from verification and CASA | §2 | Consent completed, no interstitial, verification never entered |
| `calendar.events.owned` permits attendees | §3 | External attendee accepted on the narrow scope |
| Meet links come from `conferenceData` | §4 | Created and visible to the attendee |
| `sendUpdates` is not the approval gate | §4 | Event reached an external calendar with no email sent |

What remains, none of it blocking:

1. **What counts as a conflict?** §5. At least three things share the word —
   two events overlapping, a commitment whose `due_at` lands on a day with no
   room to do it, a lead whose `next_action_at` collides. Only the first is a
   calendar fact, and the other two are the interesting ones. The spec has to
   pick.
2. **How does Gaia learn attendee email addresses?** §6. `contacts.email` is
   nullable and near-empty; the notes carry names. Unchanged from the backlog,
   not a research question, and still the likeliest source of day-one friction.
3. **What does a Gaia-written draft say, and does it arrive pre-addressed?**
   §1's edge case. The credential cannot send, but it can stage a fully-addressed
   reply-all in a live client thread that a person sends with one careless
   click. A drafting convention, not a scope.
4. **Where do refresh tokens get encrypted, and with what key?** Untouched here.
   §1 caps the Gmail blast radius usefully — an exfiltrated `gmail.insert` +
   `gmail.metadata` token can plant mail in a Drafts folder and read envelopes,
   but cannot send as the developer and cannot read anyone's prose. The calendar
   token has no such ceiling: `calendar.events.owned` can create and delete
   events on the developer's calendar.
5. **Thread selection, given no search.** §1: metadata scope refuses `q`. Any
   design that assumed Gaia could find a thread by subject or sender needs
   another mechanism — the developer picking one, or a `threadId` remembered
   from earlier.

**Recommended scope set, on the evidence above:**

```
openid                                                  # identity -- see the correction below
email                                                   # the consenting address, from the id_token
https://www.googleapis.com/auth/gmail.insert            # write drafts, cannot send
https://www.googleapis.com/auth/gmail.metadata          # headers only, for threading
https://www.googleapis.com/auth/calendar.events.owned   # events incl. attendees + Meet
```

### Correction, 2026-09-15: this list was wrong, and the omission was load-bearing

The first four rows above were the whole list when this document was written,
and a design built on it **could not complete a single consent**.

Section 1 requires the callback to compare the consenting Google account against
`users.email`. It never asked where that address comes from. The obvious answer —
`https://www.googleapis.com/oauth2/v2/userinfo` — is served only to tokens
holding `openid`, `email`/`userinfo.email` or `profile`. A token scoped to
`calendar.events.owned` alone receives **403, insufficient authentication
scopes**. The address then reads as the empty string, the domain check refuses
it, and every developer who tries to connect is told "that account cannot be
connected" — a message that sends whoever is debugging it to audit
`users.email` and `GOOGLE_DOMAIN`, neither of which is the problem.

**Why this survived thirteen implementation tasks and thirteen reviews:** the
token exchange is mocked in every route test. A mock returns whatever shape the
test author expected, so **no mock can falsify a scope requirement**. Every
other finding in this document was measured against a live API; this one was
assumed, and the assumption was never probed. The "what this research does not
settle" list did not include it, which is the more useful lesson — the danger
was not an open question left open, it was a question nobody thought to ask.

**The repair is better than the omission.** Requesting `openid email` makes the
token endpoint return an `id_token` carrying `email` and `hd`, so the second
HTTP call disappears — and `hd` is the authoritative one. Google's words:

> Unlike the request parameter, the ID token `hd` claim is contained within a
> security token from Google, so the value can be trusted.

So the domain check becomes an assertion signed by Google rather than
`address.endswith("@" + domain)` string matching. `openid` and `email` are
**non-sensitive** — unlike the Gmail scopes in section 2 — so nothing here
disturbs the Internal exemption reasoning.

A token taken straight from Google's token endpoint over HTTPS, in response to
our own client-authenticated request, does not require signature validation;
Google documents that exemption, and it is exactly this code path. It would not
hold for an `id_token` arriving by any other route.

Three, not four. `calendar.freebusy` was in an earlier draft of this list and
was dropped while designing: correlation needs event *detail*, which `freebusy`
does not return, so the digest reads `events.list` — and `calendar.events.owned`
already grants that ("**See**, create, change, and delete events on Google
calendars you own"). Under own-calendar-only, `freebusy` buys no capability and
only lengthens a consent screen that already explains itself poorly.

Two of the three are restricted (`gmail.insert`, `gmail.metadata`), which is
affordable only while the app stays Internal (§2) — and that constraint, not any
of the API mechanics, is the thing most likely to change out from under this
design.
