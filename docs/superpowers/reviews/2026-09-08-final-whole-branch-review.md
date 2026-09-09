# Final whole-branch review — gaia-butler increment 1

**Range:** `5e5c287..e83c370` (38 commits, 77 files, +4352/−666)
**Spec:** `docs/superpowers/specs/2026-09-08-gaia-butler-design.md`
**Reviewer scope:** cross-cutting correctness, consistency, aggregate dead code,
deferred-list triage, the prompt surface, production risk. Test suite and
container stack not re-run (already verified by the controller).

---

## Verdict

**Ship with fixes.**

The architecture is sound and the spec was followed closely. The three-layer
boundary holds, `visible()` is genuinely the only place read-filtering lives, and
the ownership-vs-visibility distinction is applied correctly and deliberately in
every repository I read — `leads.due_for` vs `leads.query` on the same table is
exactly the discrimination §3.3 asked for, and the docstrings prove it was
understood rather than copied. The derived-visibility mechanism works on both
paths (insert-time copy and the trigger). This is better than most code that
ships.

But there are three defects that must not reach a real brokerage's client book,
and each is only visible from above the task boundary:

1. Private meeting content is written verbatim into an **org-visible** contact
   profile, readable by the whole company through `lookup_contact`.
2. The nightly off-box backup script aborts on every run after the first,
   never prunes, and logs `backup failed` every night — permanently destroying
   the only signal that the only off-box copy of the client book is working.
3. One failing tool poisons the turn's Postgres transaction, silently rolling
   back everything the turn had already saved.

None of these could have been caught by a scoped per-task review. All three are
small fixes.

---

## Critical

### C1 — A private meeting's content leaks company-wide through the contact profile

`gaia/capabilities/meetings/tools.py:8,29,32` · `gaia/core/db/contacts.py:9,25`
· `gaia/capabilities/meetings/__init__.py:33-39`

The path:

```
save_meeting(private=True, contacts=[{name: "Rivera",
                                      profile_update: "divorcing, must sell by Dec,
                                                       will take 540 if pushed"}])
  → meetings_db.save(..., visibility="private")        # meeting: private ✅
      → contacts_db.get_or_create(conn, user, "Rivera")
          → create_contact(..., visibility="org")      # ← hardcoded default
  → contacts_db.merge_profile(conn, user, cid, "divorcing, must sell by Dec, …")
```

`contacts.create_contact` defaults `visibility="org"` (`contacts.py:9`) and
`get_or_create` never passes anything else (`contacts.py:25`). So the confidential
sentence lands in an org-visible `contacts.profile`, and any colleague gets it
back verbatim from `lookup_contact`:

```python
async def lookup(conn, user, name):   # contacts.py:40
    ... WHERE lower(t.name) = lower(%(name)s) AND (t.visibility='org' OR ...)
```

The row passes `visible()` because the row *is* org. `visible()` is not bypassed;
the wrong visibility was written in the first place.

This is precisely the failure §3.4 exists to prevent, one table over: "the private
note is hidden from the meetings list and *fully searchable by everyone*". Here
it is hidden from the meetings list and *fully readable by everyone*. The
meetings capability is even the one that markets the guarantee — the schema tells
the model `private: "True if she asks to keep this off the company record"` while
`profile_update: "New facts about this person to merge into their profile"` carries
no visibility notion at all. A model handed a discreet divorce-sale note will
write the discreet part into the profile. The product invites the leak.

`tests/test_capability_meetings.py:22` (`test_save_meeting_respects_private`)
asserts the *memory chunk* is hidden and stops there. `tests/test_contacts.py`
exercises private contacts, but nothing in the suite ever creates one through a
production path, because no production path can.

**Also note (lower severity, same root):** a private meeting creates an
org-visible contact *row*. Sofia sees "Rivera" appear in her roster line. That
is a name, not content, and is arguably acceptable for a shared company contact
book — but it should be a decision, not an accident.

**Fix (increment 1, minimal).** Do not merge private facts into a shared profile:

```python
# meetings/tools.py, in the contact loop
if entry.get("profile_update") and visibility == "org":
    await contacts_db.merge_profile(conn, user, contact_id, entry["profile_update"])
```

and return `{"profile_updates_skipped": True}` in the tool result plus one prompt
line, so the model can say "I kept that off her profile" rather than silently
dropping it. Add a test: Ana files a private meeting with a `profile_update`;
Sofia's `lookup_contact` must not contain the string.

The full fix — per-entry profile visibility — needs a `profile_entries` table and
belongs with the deferred consolidation pass. Do not build it now.

### C2 — `backup.sh` aborts on every run after the first; retention never prunes

`deploy/backup.sh:5,28` · `deploy/README.md:120-123`

```bash
set -euo pipefail
...
  | while read -r key; do
      d=$(echo "$key" | sed -n 's/gaia-\([0-9]\{8\}\)T.*/\1/p')
      [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ] && aws s3 rm ...
    done
echo "backed up ${FILE##*/}"
```

Line 28 is a bare `&&`-list used as a statement. Under `set -e`, when the guard
is false the whole list exits non-zero and the shell dies. The `while` runs in the
last stage of a pipeline (a subshell), so it dies, `pipefail` propagates, and the
script exits 1 — before `echo "backed up …"`.

Reproduced exactly:

```
$ bash t2.sh          # one in-retention key, same guard, same pipeline
exit=1                # "backed up OK" never printed
```

Every backup after the very first one has at least one object newer than the
30-day cutoff, so **this fires every night, forever**. Consequences:

- `docker compose logs backup` prints `backup failed` every single night.
- Retention never prunes — Spaces grows without bound, and 30-day retention is
  documented as a property of the system that is not true.
- Worst: `deploy/README.md:120` tells the operator "If a night's backup fails,
  `backup failed` is logged … check `docker compose logs backup` if you suspect a
  night was missed." The alarm is stuck on. Within a week the operator learns to
  ignore it, and a *genuine* failure is indistinguishable from the normal case.

The dump and upload themselves happen before the failing block, so backups are
still being taken — this destroys the monitoring signal and the retention policy,
not the data. But this is the only off-box copy of a real brokerage's client book,
and the spec's own words are "an untested backup is not a backup." The
controller's own verification note says the `aws s3` legs were never exercised,
which is exactly why this survived.

The irony is that `backup.sh:17-22` carries a long comment about a `set -e` abort
in this exact block, fixed for `date` and reintroduced six lines later.

**Fix.** Make the guard a conditional, not a statement:

```bash
      if [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ]; then
        aws s3 rm "s3://${SPACES_BUCKET}/backups/${key}" --endpoint-url "${SPACES_ENDPOINT}"
      fi
```

and, since this is the client book, have the loop count deletions and `echo` a
summary so a silent no-op is visible.

### C3 — One failing tool poisons the turn's transaction and rolls back the whole turn

`gaia/capabilities/base.py:77-88` · `gaia/butler.py:163-172` · `gaia/core/llm.py:66`

`handle_turn` runs the entire agent loop inside a single `tx()`:

```python
async with tx() as conn:                       # butler.py:163
    ...
    reply = await run_agent(client, conn, user, messages, system, tool_defs)
    await messages_db.log(conn, user, "assistant", reply)
```

`Registry.dispatch` catches every handler exception and returns a friendly string
to the model (`base.py:84-88`). For a *database* error that is not recovery — it
is a trap. psycopg3 leaves the connection in `INERROR` after a failed statement
inside `conn.transaction()`; there is no savepoint, so every subsequent statement
on `conn` raises `InFailedSqlTransaction`. Concretely:

1. Model calls `save_meeting` — succeeds, meeting + commitments + chunk written.
2. Model calls `create_lead` with `next_action_at: "next Friday"`, or
   `update_lead` with a hallucinated `lead_id` that is not a UUID.
3. Postgres: `invalid input syntax for type timestamp` / `for type uuid`.
   Transaction aborted.
4. `dispatch` swallows it and returns "tool create_lead failed. Tell the user…".
5. Model writes a reply. `messages_db.log(conn, …)` → `InFailedSqlTransaction`.
6. Outer `except` → `_apologize` → "Sorry, something went wrong on my end."
7. `tx()` rolls back — **including the meeting from step 1.**

The user photographs her notes, is told something went wrong, and her meeting is
gone. That is the exact failure `handle_turn`'s own docstring says it split the
transaction to prevent ("the rollback would erase the only record that she ever
wrote in"), reintroduced one step later.

Nothing in the suite catches it: `test_dispatch_does_not_leak_exception_internals`
(`test_registry.py:62`) passes `conn=None` and raises a `RuntimeError`, never a
real SQL error on a real connection.

This also silently upgrades deferred item 13 ("tool schemas type ID fields as
plain `string`… cosmetic; the model can still recover"). It is not cosmetic while
C3 stands: a malformed id is a `uuid` cast error, which aborts the turn rather
than degrading to "belongs to someone else."

**Fix.** Wrap each handler call in a nested transaction — psycopg3 turns that into
a savepoint, so a failed tool rolls back only its own writes and the outer
transaction stays usable:

```python
try:
    async with conn.transaction():                 # savepoint
        result = await tool.handler(conn, user, args)
    return json.dumps(result, default=str)
except Exception:
    log.exception("tool %s failed", name)
    return f"tool {name} failed. ..."
```

`dispatch` is called with `conn=None` in tests, so guard for that or give the
tests a real connection. Add a regression test: a tool that executes deliberately
invalid SQL, followed by a second tool that must still succeed.

---

## Important

### I1 — Production Postgres publishes a port, with `devpassword` as the default

`docker-compose.yml:8,10-11,29,43,54` · `deploy/README.md:20` · `.env.example:9`

`docker-compose.yml` is the production file (there is no separate dev compose) and
it publishes `127.0.0.1:5432:5432`, while `deploy/README.md:20` tells the operator
"Postgres publishes no ports and is reachable only on the compose network" and
spec §8.3 says the same. The runbook asserts a security property the compose file
contradicts.

Compounding it: `POSTGRES_PASSWORD: ${DB_PASSWORD:-devpassword}` appears four
times, and `.env.example:9` ships `DB_PASSWORD=devpassword`. The first-run
instruction is `cp .env.example .env # fill in`. Every other secret in that file
is an obvious placeholder (`sk-ant-...`, `EAAG...`) that demands attention;
`DB_PASSWORD=devpassword` looks already filled in. The realistic outcome is a
droplet running the company's client book on loopback:5432 with a password that
is in a public git repo.

**Fix.** Drop the `ports:` block from `db` (tests set `TEST_ADMIN_DSN` and can
publish it from a `compose.override.yml`), drop every `:-devpassword` default so
a missing `DB_PASSWORD` fails loudly, and change `.env.example` to
`DB_PASSWORD=CHANGE_ME_generate_with_openssl_rand_base64_32`.

Related, smaller: `caddy` gets `env_file: .env` (`docker-compose.yml:83`), so the
public-facing TLS terminator holds the Anthropic key, the WhatsApp token and the
DB password in its environment. It needs `DOMAIN`. Pass only that.

### I2 — The out-of-window digest will very likely be rejected by Meta

`gaia/core/whatsapp.py:69-83` · `gaia/jobs/digest.py:26-33,111`

`send_template` puts the whole digest into one template body parameter. The
WhatsApp Cloud API rejects template parameters containing newlines, tabs, or 4+
consecutive spaces (error 132000, "parameter format does not match"). The digest
system prompt at `digest.py:27` asks for exactly the opposite:

> "listing what needs follow-up today. **Group by person.** Plain text, no
> markdown, no bullet characters."

A grouped multi-item follow-up list is a multi-line message. The template path is
used for exactly one case — an agent who has not texted in 24h, including every
newly-onboarded agent's *first* digest (`deploy/README.md:56-61` calls this out as
the reason the template exists). So the highest-stakes send is the one most likely
to be rejected. It is also silently rejected: see I3.

Secondary: `body[:1000]` (`whatsapp.py:80`) truncates mid-sentence with no marker.

This is unverifiable without a real approved template, and the smoke test never
exercised `send_template` against Meta — which is why it survived.

**Fix.** In `send_template`, flatten before sending, and say so:

```python
flat = " · ".join(line.strip() for line in body.splitlines() if line.strip())
flat = re.sub(r"\s{4,}", " ", flat)
if len(flat) > 900:
    flat = flat[:900].rsplit(" ", 1)[0] + "… (reply here for the rest)"
```

Add to `deploy/README.md` step 4: the template's language must be registered as
`en`, not `en_US` — `whatsapp.py:78` sends `{"code": "en"}` and Meta matches
exactly.

### I3 — Sends never report success, so the digest records an undelivered message as sent

`gaia/core/whatsapp.py:49-58` · `gaia/jobs/digest.py:107-119` · `gaia/butler.py:172-174`

`_post` logs a 4xx and returns `None`. `send_text`/`send_template` return `None`.
So `send_digest` proceeds regardless:

```python
if await _within_window(conn, user):  await wa.send_text(...)
else:                                  await wa.send_template(...)
await leads_db.mark_nudged(...)        # nudge_count += 1
await commitments_db.mark_nudged(...)
await messages_db.log(conn, user, "assistant", text)
await conn.execute("UPDATE users SET last_digest_on = %s ...")
```

A rejected template (I2), an expired access token, or a rate limit produces:
nudge counts incremented, the digest logged into her thread as though she read it,
and `last_digest_on` set — so it will not be retried today. The agent gets
nothing, the system believes it delivered, and tomorrow's message says "still open
from yesterday" about something she was never told.

Spec defect #8 was "Send failures silent," and §5.4 says "Every send checks the
response status and logs failures." The letter is satisfied; the intent is not —
a log line no one reads is the same silence one layer up.

The butler has the milder version of the same shape (`butler.py:172-174`): the
reply is logged *inside* the transaction and sent *after* it commits, so a failed
send leaves a reply in history the user never received. Note that `_apologize`
(`butler.py:62-72`) gets this exactly right — it logs only after the send
succeeds, with a docstring explaining why. Two functions in the same file,
opposite conventions.

**Fix.** Have `_post`/`send_text`/`send_template` return `bool`. In `send_digest`,
return `False` without marking nudged or setting `last_digest_on` when the send
failed, so the next 15-minute tick retries. In the butler, log the reply after a
successful send.

### I4 — Webhook dedup can double-process a redelivered message

`gaia/main.py:72` · `gaia/butler.py:105,113,116` · `gaia/core/turns.py:26-49`

The webhook checks `messages_db.seen(conn, message["id"])` at receipt, but the
message is only *written* to `messages` inside `handle_turn`, after the 3-second
debounce and after the per-user lock is acquired. Spec §5 step 4 is explicit:
"Log the inbound message and take the user's turn lock… Logging before reading is
crash-safe for dedup."

The window is not 3 seconds. If a turn is already in flight, the queued message
sits unlogged for the whole of the previous turn — 10-30s on a photo — which is
squarely inside Meta's retry window. A redelivery then passes `seen`, gets
submitted as a fresh burst, and runs as a second turn: the model sees the same
message twice and the user gets two replies to one message. `messages.log`'s
`ON CONFLICT (wa_msg_id) DO NOTHING` protects the table but not the behaviour.

Spec §9.2 lists "Webhook | … duplicate id → no-op" as a required test.
`tests/test_webhook.py` has bad-signature, missing-signature, verify-challenge,
wrong-token, unknown-sender, health, and ordering — **no duplicate-id test.** It
is the one line of that row that was not implemented and the one that has a bug.

**Fix.** Log the inbound row in the webhook handler, inside the same transaction
as the `seen` check, before `queue.submit`. `handle_turn` then builds blocks
without re-logging (it still needs its own logging for the failed-photo note; make
that an `UPDATE` or keep the `ON CONFLICT`). Add the missing test.

### I5 — The system prompt tells the model the company's contact book is the user's

`gaia/butler.py:29` · `gaia/core/db/contacts.py:28-37`

```
People she has worked with recently: {roster}
```

but `contacts.roster` is scoped by `visible()`, not by ownership:

```sql
SELECT t.name FROM contacts t
WHERE (t.visibility = 'org' OR t.user_id = %(scope_user_id)s)
ORDER BY t.updated_at DESC LIMIT 40
```

That is *the company's* 40 most recently touched contacts. Ana's prompt will
assert, as fact, that she recently worked with Sofia's clients. The model will act
on it — "how did it go with Rivera?" about someone Ana has never met, in a
brokerage where agents guard their books.

This is the ownership-vs-visibility rule inverted in the one place it is stated
in natural language rather than SQL. Spec §5.2 says "the current user's
recently-touched contacts, capped"; the implementation is the org's.

**Fix.** The honest scoping already has a table waiting for it — `meeting_contacts`
(see D3, currently write-only dead code):

```sql
SELECT DISTINCT ct.name FROM contacts ct
JOIN meeting_contacts mc ON mc.contact_id = ct.id
JOIN meetings m ON m.id = mc.meeting_id
WHERE m.user_id = %(scope_user_id)s
ORDER BY ... LIMIT 40
```

That makes the sentence true and retires the dead table in one change. If you
prefer the org-wide roster (defensible — it does help the model spell colleagues'
clients correctly), then relabel the line: "People in Gaia's contact book:".
Either is fine; the current pairing is not.

### I6 — The prompt-cache breakpoint sits after the volatile content

`gaia/core/llm.py:36` · `gaia/butler.py:34-45`

```python
system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
```

`system` is `BASE_PROMPT.format(name, today, tz, roster) + prompt_fragments`. The
roster is inside the cached prefix, and it reorders on essentially every
`save_meeting` (it is `ORDER BY updated_at DESC`). Every reorder is a full cache
miss on system *and* tools, and cache writes bill at 1.25×. On a workload where
most turns file a meeting, this can cost more than not caching at all.

Spec §5.3 specified the split precisely: "Prompt caching on the tools + base
system prefix, which is stable per user. Contact profiles go after the breakpoint
since they change often."

`tests/test_llm.py:75` asserts `req["system"][0]["cache_control"]` exists — which
is why this passed review; the test checks the mechanism, not the placement.

**Fix.** Two blocks. Stable first, marked; volatile second, unmarked:

```python
system_blocks = [
    {"type": "text", "text": stable,   "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": volatile},
]
```

where `stable` = BASE minus the last three lines, plus the capability fragments,
and `volatile` = the `Today is …` / roster / `Use lookup_contact` lines.
Tighten the test to assert the roster is *not* in `system[0]`.

### I7 — Every prompt hardcodes "she" / "her"

`gaia/butler.py:18-30` · `gaia/capabilities/meetings/__init__.py:74-78` ·
`gaia/capabilities/leads/__init__.py:15-17` · `gaia/jobs/digest.py:26-33` (clean)

Eight occurrences across the base prompt and both capability fragments: "When
**she** sends meeting notes", "ask **her** to confirm", "**her** timezone",
"People **she** has worked with", "When **she** asks you to keep something off
the company record", "When **she** mentions someone who might transact", "reaches
**her** morning digest".

This is a direct carry-over from the single-user `wife-agent` prototype into a
product whose first stated goal is "**N users.** Any Gaia agent on the roster."
The model is handed the user's real name and then told, repeatedly, that the
user is a woman. It will misgender male agents in its replies, and it will do so
in the very first sentence of a product being sold to a company.

The digest prompt (`digest.py:26`) gets it right — "a real-estate agent" — which
shows the convention was available and just not applied.

**Fix.** `{name}` where a referent is needed, "they" otherwise. Ten minutes.
Cheapest high-visibility fix on this list.

### I8 — An unvalidated `--tz` typo permanently breaks the digest for the whole company

`gaia/core/admin.py:19` · `gaia/jobs/digest.py:42` · `gaia/butler.py:39`

`add-user --tz` takes any string with no validation, and `users.timezone` is plain
`TEXT`. `ZoneInfo("America/NewYork")` raises `ZoneInfoNotFoundError`. That call
sits inside the `for user in ...` loop of `due_users` (`digest.py:42`), which is
inside `run_once`, whose exception is caught only at the top of `main`
(`digest.py:151`). So one bad row means **no user gets a digest, ever**, with a
single `digest run failed` line every 15 minutes.

The same value breaks that user's every turn: `build_system_prompt`
(`butler.py:39`) raises, and she gets the apology text for every message she
sends, forever.

**Fix.** Validate in the CLI (`ZoneInfo(args.tz)` before the insert, exit with a
clear message) *and* make `due_users` resilient — wrap the per-user body in
`try/except`, log the offending user, continue. Both: the CLI check prevents it,
the loop guard means a bad row can never take the whole company's digest down.

### I9 — `merge-contacts` does not exist

`gaia/core/admin.py:11-26` · spec §7, §3.5 · deferred item 5

Spec §7 lists four subcommands; `build_parser` implements three. There is no
`merge-contacts`, and `deploy/README.md` does not mention it.

This matters beyond a missing feature: it is the stated remedy in two places.
Spec §3.5 justifies the deliberately non-unique index on `lower(name)` with
"`admin merge-contacts` handles duplicates," and the Task 5 ruling parked the
`get_or_create` TOCTOU race on the grounds that "merge-contacts is the documented
remedy." The remedy was never built, so the parked race currently has no remedy at
all — just an acceptance whose premise is false.

**Fix.** Either build it (≈30 lines: reassign `leads.contact_id`,
`commitments.contact_id`, `memory_chunks.contact_id`, `meeting_contacts`,
concatenate profiles under `PROFILE_CAP`, delete the source, all inside one `tx`),
or amend the spec and `deploy/README.md` to say duplicates are resolved by hand
in SQL. Do not leave the branch claiming a command it does not have.

### I10 — The agent loop holds a pooled connection open, in a transaction, across every model call

`gaia/butler.py:163-172`

`handle_turn`'s docstring:

> "this function's own sends to WhatsApp happen outside any transaction so a slow
> Graph API call never holds a pooled connection."

The Graph call is ~200ms. The Anthropic loop is 10-30s on a photo, up to 8
iterations — and it runs *inside* the transaction, holding one of eight pooled
connections idle-in-transaction the whole time. The stated principle is right and
was applied to the cheaper of the two calls.

At Gaia's volume (a handful of agents) this will not exhaust `max_size=8`, and
Postgres will not vacuum-stall on a 30s transaction. It is not urgent. But it is
the mechanism behind C3 — the transaction is long-lived *and* spans arbitrary tool
code — and it means one wedged Anthropic request pins a connection until the httpx
timeout.

**Fix (with C3, or after).** Let each tool own its transaction: pass the pool
rather than a connection into `run_agent`, and have `dispatch` open `async with
tx(pool)` per call. That gets savepoint-free isolation, releases the connection
between iterations, and makes a partially-successful turn keep its successful
writes — which is the behaviour the docstring already argues for.

### I11 — `README.md` is still the deleted prototype's README

`README.md` (untouched across all 38 commits — `git log 5e5c287..e83c370 -- README.md` is empty)

It opens with "Personal agent … **Single-user by design**", documents `app/main.py`,
`app/tools.py`, `app/db.py`, `app/followup_cron.py` and `schema.sql` — every one
of which this branch deletes — tells the reader to `cd wife-agent`, deploys to
Hetzner rather than a DigitalOcean droplet, says "Text the number from her phone",
and lists the 24-hour window as an unfixed known issue with a "Fix:" that this
branch already implemented.

`deploy/README.md` is excellent and current. The repo root README is the first
thing anyone opens and it describes a different, deleted application.

**Fix.** Rewrite it from the spec's §1 and §2 and point deployment at
`deploy/README.md`.

---

## Consistency across the sixteen tasks

Genuinely coherent: logger naming (`gaia.<module>`), the `(conn, user, ...)`
signature convention and its reflection tripwire, docstrings that explain *why*
rather than restate the code, the `visible()` fragment used identically
everywhere it appears, and the deliberate ownership-vs-visibility split with the
reasoning written down at each site. This does not read as sixteen dialects.
The divergences below are real but few.

**M1 — Two conventions for constructing the Anthropic client.**
`gaia/butler.py:156-162` builds `AsyncAnthropic()` zero-arg with a four-line
comment: "**Never pass `api_key=`** — that would turn workload identity
federation into a code change later instead of a config-only one."
`gaia/jobs/digest.py:143` does `AsyncAnthropic(api_key=settings.anthropic_api_key)`.
One module states a rule; the other, written later, breaks it. Make digest
zero-arg.

**M2 — `jobs/digest.py` reaches around the repository layer.** Three raw queries
against `users` live in the job: `SELECT last_digest_on` (`digest.py:45`),
`SELECT last_inbound_at ...` (`digest.py:61`), `UPDATE users SET last_digest_on`
(`digest.py:116`). Every other mutation of `users` in the codebase goes through
`gaia/core/db/users.py`. Spec §2 puts `jobs/` above both other layers as a
consumer, not a peer of `core/db`. It also means `test_db_signatures.py`, which
walks `gaia.core.db` only, is structurally blind to them. Move them to
`users_db.mark_digest_sent(conn, user, local_date)` and
`users_db.within_window(conn, user)`.

**M3 — Write scoping differs by module, correctly, but undocumented.**
`contacts.merge_profile` scopes writes by `visible()` — a colleague may append to
an org contact (necessary: `get_or_create` hands back rows owned by others, and
`test_contacts.py:65` asserts the behaviour deliberately). Every other writer —
`leads.update`, `leads.mark_nudged`, `commitments.complete`, `meetings.set_visibility`
— scopes by `user_id`. Both are right for their table. Nothing says so; spec §3.2
says only "an update has to prove the caller may touch the row," which admits
both readings. Add a sentence to `scope.py`'s module docstring: writes to shared
entities (contacts) are visibility-scoped; writes to owned work items are
ownership-scoped.

**M4 — Two different meanings of "due".** `leads.due_for` (`leads.py:47-48`)
requires `next_action_at IS NOT NULL AND <= now() AND status IN ('new','active')`.
`leads.query(due_only=True)` (`leads.py:62`) requires only `next_action_at <= now()`
— no status filter, so it returns closed and lost leads as "due". The model sees
one word and two behaviours: the digest nags about active work, `query_leads`
answers with dead deals mixed in. Align the status filter.

**M5 — Three shapes for "that didn't apply to you".** `set_meeting_visibility`
returns `{"changed": False, "note": "no such meeting, or it belongs to someone else"}`;
`update_lead` returns the same shape; `complete_commitment`
(`capabilities/leads/tools.py:42-44`) returns a bare `{"completed": False}` with
no note. The model gets no way to distinguish "already done", "does not exist" and
"someone else's" on the one tool of the three that most needs it. Add the note.

**M6 — `_within_window` interpolates into SQL by f-string** (`digest.py:63`)
where every other query parameterises. `WINDOW_HOURS` is a module int so it is
safe, but it is the only place in the codebase doing it and it will be copied.
Use `make_interval(hours => %(h)s)`.

**M7 — Digest fires at any hour ≥ 08:00 local, not "just crossed 08:00".**
`digest.py:43` is `if local.hour < SEND_HOUR: continue`, with no upper bound; the
only other gate is `last_digest_on`. On a day that starts empty, `send_digest`
returns `False` without setting `last_digest_on`, so the job keeps retrying every
15 minutes all day — and the moment a lead falls due at 16:45 the agent receives a
message that opens "Morning!" (per `digest.py:26`, "Write a short, warm **morning**
… message"). Spec §6: "whose *local* time has just crossed 08:00." Bound it:
`if not (SEND_HOUR <= local.hour < SEND_HOUR + 2): continue`.

**M8 — `evals/smoke.py:42-58` duplicates `BASE_PROMPT` verbatim** instead of
calling `gaia.butler.build_system_prompt`, and never appends
`registry.prompt_fragments`. The smoke test's own docstring claims it proves
"what the assistant actually SAYS — the system prompt has never been read by a
model before this." It read a *copy* of two thirds of it. The two capability
prompt fragments — a third of the product surface, and the part that governs
privacy vocabulary — have still never been in front of a model. Import the real
builder.

---

## Aggregate YAGNI / dead code

- **D1 — `meetings.recent()`** (`gaia/core/db/meetings.py:68-74`). No consumer, no
  test. Known. **Delete**; it is eight lines and trivially re-added.
- **D2 — `Registry.all()`** (`gaia/capabilities/base.py:55-56`). Called from
  nowhere in `gaia/`, `tests/` or `evals/`. It is the spec §4 pseudocode's API,
  superseded by `for_user`. Delete.
- **D3 — the `meeting_contacts` table** (`migrations/001_init.sql:65-69`). Written
  by `meetings.save` (`meetings.py:38-42`); read by nothing, in code or tests.
  Do **not** delete — it is the correct backing for the I5 roster fix. Use it, and
  it stops being dead.
- **D4 — `User.is_admin`** (`gaia/core/models.py:14-16`). Never referenced.
  Capability allowlists compare `user.role` directly. Delete or use it in
  `Capability.visible_to`.
- **D5 — `Capability.description`** (`base.py:33`). Set on both capabilities, read
  nowhere — only `Tool.description` reaches the API. Harmless, but it is declared
  public API that does nothing; either surface it or drop it.
- **D6 — `contacts.create_contact`'s `visibility` parameter** (`contacts.py:9`).
  Only ever non-default from tests; no production path creates a private contact.
  Not dead code so much as a load-bearing fact — it is the entire reachability
  argument for deferred item 9. If you keep relying on that, assert it: a test
  that fails if any production call site passes `visibility != "org"`.
- **D7 — leftover artifacts.** `app/__pycache__/*.pyc` remains on disk from the
  deleted prototype (gitignored, harmless — `rm -rf app`), and
  `tests/__pycache__/test_trigger_manual_scratch.*.pyc` is the compiled remnant of
  a scratch test file that no longer exists.

Nothing else in the branch is unused. For 4352 added lines that is a good result.

---

## The prompts, read as a user

**Assembled system prompt** = `BASE_PROMPT` (butler.py:15-31) + meetings fragment
(meetings/`__init__`.py:73-79) + leads fragment (leads/`__init__`.py:14-18), in
registration order.

**What works.** It is short, it is concrete, and the register is right for
WhatsApp: "brief and warm, like a text message. No markdown headers or bullet
lists" is exactly the instruction that keeps a model from producing a bulleted
report in a chat bubble. "Echo back what you understood and ask her to confirm
anything ambiguous: names, numbers, dates" is the single most valuable line in the
file — for a product whose hardest job is reading bad handwriting, forcing
confirmation on names, numbers and dates is the difference between useful and
dangerous. The `today` injection is right and fixes prototype defect #6. The
meetings fragment explains *both* directions of `set_meeting_visibility` and says
what it cascades to, which is the kind of detail that stops a model from claiming
it did something it did not. The leads fragment states the one non-obvious causal
fact the model could not infer — "A lead without a `next_action_at` will never be
followed up" — which is precisely what a prompt fragment is for. The two fragments
do not contradict each other or the base, and they concatenate into readable
prose rather than a wall of rules.

**What will misfire.**

1. **The gendering (I7).** Read as a user, this is the first thing you notice and
   the hardest to un-notice.
2. **The roster line is false (I5).** "People she has worked with recently:"
   followed by the company's contact book. Everything downstream of a false
   premise in a system prompt is confidently wrong.
3. **Nothing tells the model that org data belongs to colleagues.** `search_memory`
   returns another agent's meeting notes with a `contact` and a `date` and no
   owner. The model, told it is "the assistant for Ana," will report Sofia's
   meeting back as Ana's: "you told Rivera you'd send comps Friday." In a
   brokerage where agents guard their books, that is worse than not answering.
   Two changes: return the owner's name from `memory.search` and `leads.query`,
   and add one line — "Some notes and leads belong to Ana's colleagues at Gaia.
   When you use one, say whose it is." This is the largest *product* gap in the
   prompt surface and it is two lines of work.
4. **"Never contact third parties."** There is no tool that could contact anyone,
   so this defends against nothing and spends a sentence telling the model about a
   capability it does not have — which occasionally makes models ask about it.
   Drop it, or replace it with the colleague-attribution line above, which is the
   real constraint.
5. **The privacy vocabulary is incomplete (C1).** The fragment promises "keep
   something off the company record" while `profile_update` quietly puts it on the
   record. Whatever you do about C1 in code, the prompt must stop making a
   promise the tools do not keep.
6. **Digest prompt (`digest.py:26-33`)** is good on its own — the nudge_count
   escalation ladder is well specified and "Never repeat yesterday's phrasing
   verbatim" is the right instruction for defect #9. But "Group by person"
   collides with the template channel (I2), and "End by offering to draft any of
   the follow-up texts" sets up a follow-on turn the butler handles fine — as long
   as someone has checked the reply lands inside the 24h window, which by
   construction it may not.

**Verdict on the prompts.** Coherent and better than most; three genuine defects
(gender, false roster premise, missing colleague attribution) and one promise the
tools break. All four are text edits.

---

## Deferred-list triage

| # | Item | Verdict | Note |
|---|---|---|---|
| 1 | `get_pool()` singleton has no lock | **Accept** | No `await` between check and assign; single event loop. Not reachable. |
| 2 | `pyproject.toml` has no `[build-system]` | **Closed** | Present at `pyproject.toml:1-3`; image builds. |
| 3 | `IS DISTINCT FROM` guard is defensive dead code | **Accept — keep it** | Zero cost, and it keeps the trigger from firing two UPDATEs on every unrelated `meetings` update. Currently dead only because nothing else updates `meetings`. |
| 4 | `list-users` e2e asserts stdout only | **Accept** | stdout is that command's only observable output. |
| 5 | `get_or_create` TOCTOU race | **Should-fix soon** | The race itself is fine (one duplicate row, cross-user only, rare). But the ruling's premise is false — `merge-contacts` does not exist (I9). Build it, or amend the ruling honestly. |
| 6 | `merge_profile`'s two UPDATEs not in an explicit tx | **Accept** | Every caller runs inside `tx()`; the second UPDATE is idempotent. |
| 7 | `test_save_creates_contacts_and_commitments` asserts only `description` | **Should-fix (cheap)** | `meeting_contacts` and the resolved `contact_id` are asserted by nothing anywhere. Worth doing *with* the I5 roster change, which makes that link load-bearing. |
| 8 | `meetings.recent()` — no test, no consumer | **Delete** | D1. Confirmed dead. |
| 9 | `memory.search()` returns `ct.name` via an unscoped LEFT JOIN | **Should-fix — and it is wider than recorded** | The same unscoped contact join is in `leads.query`'s `_SELECT` (`leads.py:7-9`), which the note missed. Reachability analysis still holds (nothing creates a private contact), but it now rests on an unenforced fact — see D6. Fix both joins (`CASE WHEN <visible(ct)> THEN ct.name END`) or add the assertion. Cheap either way; do it before any capability can create a private contact. |
| 10 | Offender-injection tripwire is ad-hoc, not committed | **Accept** | The parametrized isolation suite is the real guarantee and is mutation-verified. A guard on the guard is not worth the line. |
| 11 | `download_media` does not check the metadata GET status | **Accept** | The missing-url guard degrades safely and `_log_inbound` catches per photo, preserving the caption. Correct call. |
| 12 | Task 10 tested only against `FakeAnthropic` | **Mostly closed — one gap** | The live smoke test closed the model-id/request-shape/tool-schema gap. It did **not** close the prompt gap: `evals/smoke.py` uses a duplicated `BASE_PROMPT` and never assembles the capability fragments (M8). One-line fix. |
| 13 | Tool schemas type ID fields as plain `string` | **Accept — conditional on fixing C3** | Not cosmetic while C3 stands: a hallucinated id is a `uuid` cast error, which aborts the whole turn. Fix C3 (savepoints) and this reverts to genuinely cosmetic. |
| 14 | Rows that committed during the live smoke test vanished | **Accept as unexplained — but fix C2 first** | No production risk identified; dev data, dev database, no reproduction. Recording it rather than inventing a cause was right. The correct response is not more investigation of an unreproducible event, it is making sure a *repeat* is detectable and recoverable — which is exactly what C2 currently prevents. Fix C2, then this is properly accepted debt. |

---

## What is good, said plainly

Worth recording because a findings list is not a fair picture of the branch:

- The ownership-vs-visibility rule is applied correctly and *knowingly* in every
  repository. `leads.due_for` and `leads.query` sit next to each other querying
  the same table with different filters and a docstring on each explaining why.
  That is the hardest idea in the spec and it landed.
- Derived visibility works on both paths, and the trigger is the right mechanism
  for the update path — application code would have forgotten it.
- `messages.recent`'s NULL-guard comment (`messages.py:21-29`) explains a
  three-valued-logic trap that would have silently deleted every assistant turn
  from history on every real turn. That is a first-class bug caught in advance.
- The failure design in `butler.py` — commit inbound first, apologise rather than
  vanish, log the apology only after it sends, keep one bad photo's caption — is
  thoughtful in a way that only shows up when something goes wrong at 11pm.
- `deploy/README.md` is the best artefact in the branch. It says what is verified
  and what is not (the Caddy/ACME note), it explains *why* the template is a
  prerequisite, and it frames `deactivate` correctly as incident response for a
  system where a phone number is the credential.
- The parametrized isolation suite plus the `information_schema` introspection
  test (`tests/test_scope.py:14-19`) do fail closed as §9.1 designed. Its limit —
  that it tests the `visible()` predicate rather than each public reader — is
  exactly where C1 and deferred item 9 slipped through, which is worth knowing
  for increment 2.

---

## Suggested order of work

1. C2 (`backup.sh` — one `if`), I1 (compose ports + password default). Both are
   minutes and both are about the client book's safety at rest.
2. C1 (gate `merge_profile` on `visibility == "org"`, plus the test).
3. C3 (savepoint in `dispatch`, plus the regression test). This also resolves
   deferred item 13.
4. I7 (pronouns), I5 (roster), I2/I3 (template flattening + boolean sends),
   I8 (timezone validation).
5. I4 (log at the webhook), I6 (cache split), I9 (merge-contacts or amend),
   I11 (README).
6. M1-M8 and D1-D7 as cleanup.

Items 1-3 are the ones I would not put in front of a real client book without.
