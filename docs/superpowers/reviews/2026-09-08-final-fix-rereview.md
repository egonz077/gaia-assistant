# Scoped re-review of the final fix wave — gaia-butler increment 1

**Range:** `e83c370..44d3570` (11 commits, 32 files)
**Scope:** verdict C1, C3(+I10), I1–I11, the weekday fix, the dead-code removals.
C2, the full suite, and the live smoke run were verified by the coordinator and
are taken as given here.

**Bottom line:** every finding in the brief is ADDRESSED. One NEW Important was
introduced by the C1 fix — the tool result tells the model something that is not
true. Nothing else regressed.

---

## Criticals

### C1 — private `profile_update` leaking into a shared profile — **ADDRESSED**, with a new defect attached

**The leak is closed, and the skip is complete.** `merge_profile` has exactly one
production caller — `gaia/capabilities/meetings/tools.py:45` — and it is inside
`if visibility == "org"`. Nothing else in `gaia/` writes `contacts.profile`:
`leads.create` (`gaia/core/db/leads.py:28`) calls `get_or_create` but never
merges, and `contacts.merge` (the new I9 command) is `visible()`-scoped and
admin-only. There is no second path.

**The semantics decision is right for increment 1.** The reasoning holds up on
inspection: `get_or_create` (`contacts.py:17-25`) returns rows owned by other
users, and `contacts.profile` is one org-visible `TEXT` column, so a shared
contact has nowhere to put a fact learned in confidence. Option 1 (propagate the
meeting's visibility to contact creation) would have been worse in a way the
report names correctly — `get_or_create` cannot see another user's private row,
so the address book fragments permanently, and the only remedy (`merge-contacts`)
re-leaks the profile through the side door. Option 2 is a schema change and is
correctly parked. Refusing the write is the honest third option. Concur.

**The model is told, on three surfaces**, which is enough to be useful:
`save_meeting` returns `profile_updates_skipped: ["Rivera"]` plus a `note`; the
`profile_update` schema says "Skipped when private is true"; the `private` schema
cross-references it; and the meetings prompt fragment tells the model to say so
plainly. `set_meeting_visibility`'s docstring is honest about what it cannot
retract. Good.

**But: the answer to "is the fact still available to its owner" is no.** The
`note` returned to the model asserts, unconditionally:

> "It is kept in the private meeting note instead."

That is only true if the model happened to put the same fact in `summary` or
`raw_transcription`. `save_meeting` stores nothing from `profile_update` —
`meetings_db.save` persists `summary` and `raw_input` only. The branch's own test
demonstrates the failure: `test_a_private_meetings_profile_update_never_reaches_
the_company` files summary `"Quiet divorce sale"` with
`profile_update="divorcing, must sell by Dec, will take 540 if pushed"`, and after
the call that sentence exists nowhere in the database. The test asserts Ana's
`search_memory` returns *something* (the summary), not that the fact is
retrievable. So the model is handed a false statement and will relay it: "I kept
that off the shared profile and in your private note" — when the fact was
dropped. A silently-dropped fact is the defect the report itself argues against;
this is that defect with a reassuring message on top.

See **NEW-1** below.

### C2 — backup retention — **ADDRESSED** (coordinator-verified; not re-run here)

Code inspected and consistent with the verification: the guard at
`deploy/backup.sh:46` is now an `if`, the listing is captured into `LISTING` and
fed by here-string so the `pruned` counter survives, and the success line names
the count and the cutoff. `deploy/README.md:165-170` documents the line and that
`pruned 0` on an old bucket is itself a signal. One marginal note in
*Out of scope* below.

### C3 (+I10) — a failing tool poisoning the turn — **ADDRESSED**

- **Isolation is genuine, and better than a savepoint.** `Registry.dispatch`
  (`base.py:64-104`) now takes the pool and opens `async with tx(pool)` per tool
  call. A failing handler rolls back only its own transaction and returns a
  fresh connection to the pool; no later statement runs on an `INERROR`
  connection because no later statement shares one.
- **The model still gets its error and continues.** The `except` returns the same
  `"tool {name} failed…"` string, and the loop in `llm.py:90-96` keeps iterating.
- **The test uses a real connection.** `test_a_failing_tool_does_not_destroy_the_
  turns_earlier_work` (`test_registry.py:93-144`) runs on the `migrated` pool,
  with a real SQL error (`SELECT * FROM leads WHERE id = 'not-a-uuid'` — the
  `uuid` cast failure `update_lead` actually produces), work committed before it,
  and a third tool call after it. The `committed_ana` fixture exists precisely
  because the shared `ana` fixture's uncommitted row would be invisible to the
  per-call connections — that is the right reason for the right fixture. The old
  `conn=None` blind spot is gone: every `dispatch`/`run_agent` call site in the
  suite now passes `migrated`.
- **The transaction is no longer held across the Anthropic call.** `handle_turn`
  (`butler.py:224-274`) reads history and builds the prompt in one short `tx`,
  runs `run_agent` holding nothing, sends, and logs the reply in a second `tx`.
  The docstring was rewritten and is now true rather than aspirational. **I10 is
  addressed as a consequence, not documented away.**

---

## Importants

| # | Verdict | Evidence |
|---|---------|----------|
| **I1** | **ADDRESSED** | `docker-compose.yml` has no `ports:` on `db`; all four `${DB_PASSWORD:?…}`; `caddy` gets `DOMAIN` only; `.env.example` ships `CHANGE_ME_openssl_rand_base64_32`. `deploy/compose.dev.yml` publishes `127.0.0.1:5432` and is deliberately *not* `docker-compose.override.yml`, so it cannot auto-load on the droplet. Both sides reconciled and documented (README "Local development", `.env.example` header, `deploy/README.md:20-25`). Two doc nits below. |
| **I2** | **ADDRESSED** | `flatten_for_template` joins on ` · `, collapses `\s+`, marks truncation at 900; applied inside `send_template` so the digest prompt keeps its grouping. `deploy/README.md` step 4 now says register as `en`. |
| **I3** | **ADDRESSED** | Failure path checked, not just the happy one: `send_digest` (`digest.py:130-141`) returns `False` *before* `mark_nudged`, `messages_db.log` and the `last_digest_on` UPDATE, so the next tick retries; `test_a_rejected_send_records_nothing` asserts all four pieces of state untouched. `_post`/`send_text`/`send_template` return `bool`; `send_text` stops after a rejected chunk. `butler.handle_turn` logs the reply only after `delivered`, matching `_apologize`. |
| **I4** | **ADDRESSED** | `butler.receive` does `seen` + `touch_inbound` + `log` in one transaction at the webhook, before `queue.submit` (`main.py:66-78`), which is spec §5 step 4. `_log_inbound` became `_build_blocks` and writes nothing except a photo-failure amendment via `messages.set_content` — correctly an UPDATE, since a re-INSERT would vanish into `ON CONFLICT DO NOTHING`. Two tests, including the mid-turn redelivery shape. **No "recorded but never processed" regression**: the inbound row is committed before the 200 in both the old and new designs, so a crash after the 200 loses the turn either way — the difference is that now the message survives in her history instead of vanishing. One narrow residual noted below. |
| **I5** | **ADDRESSED** | `contacts.roster` now requires ownership via `EXISTS` on `meeting_contacts`/`meetings.user_id` or `leads.user_id`, with `visible()` still applied (narrows, never widens). `test_the_roster_line_is_true` proves a colleague's org contact is `lookup`-able but off the roster. `meeting_contacts` stops being write-only. |
| **I6** | **ADDRESSED** | `build_system_prompt` returns `[stable, volatile]`; `_system_blocks` marks only `parts[0]`. Block 0 = `BASE_PROMPT` + capability fragments. Block 1 = date+weekday, timezone, roster. **Nothing volatile is in block 0** — I checked specifically: the user's *name* is in block 0, and that is correct, not a miss: spec §5.3 says the cached prefix is "stable **per user**", and the name never changes within a user's turns. `test_only_the_stable_prefix_is_cached` asserts placement, not merely that a `cache_control` exists. |
| **I7** | **ADDRESSED** | No gendered pronoun anywhere in the assembled prompt; `test_says_nothing_about_the_users_gender` guards it against regression. Every remaining `she`/`her` in `gaia/` is in a docstring or comment narrating the Ana scenario — none is model-facing (verified by grep across `gaia/`). |
| **I8** | **ADDRESSED — both halves** | Entry: `add-user --tz` is `type=_timezone`, which raises `argparse.ArgumentTypeError` with an actionable message and a non-zero exit (`admin.py:13-30`); the default `America/New_York` passes it. Resilience: `due_users` wraps the whole per-user body in `try/except … continue` (`digest.py:48-62`) *and* `run_once` wraps each user's send (`digest.py:153-158`), which also covers the `ZoneInfo(user.timezone)` inside `send_digest`. `test_one_unusable_user_does_not_cost_everyone_else_their_digest` writes `America/NewYork` directly in SQL and asserts Sofia still gets hers. |
| **I9** | **ADDRESSED** | `contacts.merge` moves `leads`/`commitments`/`memory_chunks`, handles `meeting_contacts` by insert-then-delete (composite key would collide on UPDATE), `COALESCE`s phone/email, concatenates profiles under `PROFILE_CAP`, deletes the source, all `visible()`-scoped. CLI + `deploy/README.md` section with the id-finding query and the lossiness caveat. The `--as <phone>` deviation from spec §7 is well-judged and documented — an unscoped merge is C1 through the admin door. |
| **I10** | **ADDRESSED** | With C3. No transaction is held across any network I/O in `handle_turn`. |
| **I11** | **ADDRESSED** | README rewritten from spec §1–§2: what the product is, the visibility-vs-ownership rule, the real layout (matches the shipped tree, including `set_meeting_visibility` and `merge-contacts`), the dev-overlay test instructions, and a pointer to `deploy/README.md`. No trace of the prototype. |

---

## The weekday fix (`44d3570`) — **correct**

`WEEKDAYS` indexing is right. Python's `datetime.weekday()` is `0 = Monday … 6 =
Sunday`, and the tuple is `("Monday", …, "Sunday")` — index 0 is Monday.
Independently verified against the calendar:

```
2026-09-11 → Friday    2026-09-08 → Tuesday   2026-09-13 → Sunday
2026-01-01 → Thursday  2026-12-31 → Thursday  2026-09-12 → Saturday
```

all matching both the tuple lookup and `strftime("%A")`. No off-by-one.

`today_line(now)` takes the weekday and the date from the *same* `datetime`
(`WEEKDAYS[now.weekday()]` and `now.date()`), and `build_system_prompt` passes
`datetime.now(ZoneInfo(user.timezone))`, so weekday and date cannot disagree and
both follow the agent's local day. The `strftime` avoidance is justified (`%A` is
`LC_TIME`-dependent) and the *test* cross-checks with `strftime`, which is the
right way round — the tuple is not left asserting itself. The parametrized cases
are real calendar facts including 2026-09-11, and the midnight-straddle case
(`2026-09-12T01:30Z` → Friday 2026-09-11 in New York, Saturday 2026-09-12 in UTC)
is the one that actually exercises the tz-awareness. Placement is pinned by
`test_todays_weekday_stays_out_of_the_cached_prefix`, which correctly asserts on
the *rendered* value rather than on the word, so the constant `"Friday, Sept 11"`
example in the cached base prompt does not confuse it.

---

## Dead code

All four removals confirmed, with no remaining references anywhere in `gaia/`,
`tests/` or `evals/`:

- `Registry.all()` — gone; `Registry` now has `register`, `for_user`,
  `tool_defs`, `prompt_fragments`, `dispatch`.
- `User.is_admin` — gone; `User` is `id, name, wa_id, role, timezone, active`.
- `Capability.description` — gone. `Tool.description` remains and is correct: it
  is read by `Tool.to_api`.
- `meetings.recent()` — gone, along with its now-unused `visible` import
  (`meetings.py` imports only `datetime`, `UUID`, `contacts_db`, `User`).

`meeting_contacts` was **correctly kept** and is no longer dead — `contacts.roster`
reads it (`contacts.py:53-55`) and `contacts.merge` maintains it. The only
surviving references to the removed names are in `docs/superpowers/plans/…` and
`docs/superpowers/specs/…:261` (`registry.all()`), which are historical design
documents, not code.

---

## NEW breakage introduced by this fix wave

### NEW-1 (Important) — `save_meeting` tells the model the skipped fact was kept, and it was not

`gaia/capabilities/meetings/tools.py:65-69`

```python
result["note"] = (
    "This meeting is private, so what you learned about "
    f"{', '.join(skipped)} was not added to the shared company profile. "
    "It is kept in the private meeting note instead. Say so plainly."
)
```

The second sentence is asserted unconditionally and is not guaranteed. Nothing in
`save_meeting` copies `profile_update` into the meeting — `meetings_db.save`
persists `summary` and `raw_input` only — so unless the model independently put
the same fact in the summary, that text is stored nowhere. The branch's own C1
test is a live instance of it: `"divorcing, must sell by Dec, will take 540 if
pushed"` is absent from every table after the call, and the test only asserts that
Ana's `search_memory` returns the *summary*, not the fact.

The consequence is exactly the failure the fix report says it was avoiding, one
step further along: the model is told the fact was preserved, tells the user so,
and the user believes a confidential detail is on file when it is not. That is
worse than a visible drop, because it is unfalsifiable from her side.

Either fix is small:

1. **Make the claim true** — append the skipped text to the meeting's
   `raw_input` (or the summary) before saving, so the private meeting really does
   carry it; or
2. **Make the message honest** — "…was not recorded anywhere. If it matters, tell
   me and I will add it to the private meeting note."

(2) is a one-line change and needs no schema thinking. (1) is better product and
still small. Either way the tool must not claim a write it did not make.

### NEW-2 (Nit) — `tests/conftest.py:24-25` still tells you to start the DB the way that no longer works

```python
# Start it with: docker compose up -d db
```

After I1 that command brings up a Postgres with no published port, so the suite
cannot reach it. `README.md`, `.env.example` and `deploy/README.md` were all
updated to the `-f docker-compose.yml -f deploy/compose.dev.yml` form; the comment
sitting directly above the test DSN was not.

### NEW-3 (Nit) — the README quickstart drops `.env.example`'s "use `devpassword` locally" note

`README.md:82` says `cp .env.example .env  # fill in; DB_PASSWORD has no default`,
then `up -d db`, then `pytest`. A developer who follows that literally and
generates a random `DB_PASSWORD` gets a database the suite cannot authenticate
against, because `conftest.py` defaults `TEST_ADMIN_DSN` to `…gaia:devpassword@…`.
`.env.example:16-17` *does* say "LOCAL DEV: any value. `devpassword` is what the
test suite's default DSN expects, so use that" — so the information exists and the
failure is loud (an auth error), not silent. It just is not where the quickstart
is. One clause in the README comment closes it.

---

## Out of scope — for a later loop, not this one

1. **`send_digest` holds a pooled connection across two network calls.**
   `run_once` opens `tx(pool)` and calls `send_digest`, which runs `compose_digest`
   (an Anthropic call) and the Graph send inside it. This is I10's exact shape in
   the digest job. It is pre-existing structure, unchanged by this wave, and the
   digest's concurrency is one user at a time — but the principle C3/I10 just
   established is not applied there.
2. **`_system_blocks` raises `IndexError` on an empty `system` sequence**
   (`llm.py:27-31`, `parts[0]`). Unreachable from `build_system_prompt`.
3. **`flatten_for_template("")` produces an empty template parameter**, which Meta
   rejects. Unreachable today because `send_digest` returns early on empty text.
4. **`contacts.merge` lets an owner fold their *own* private contact into an org
   row**, publishing that profile. The docstring's claim is scoped to a
   *colleague's* private contact and is accurate as written. Unreachable in
   practice: no production path creates a private contact row, since
   `get_or_create` always creates `org`.
5. **`aws s3 ls` exits 1 on a prefix with no keys**, and `LISTING=$(aws … | awk …)`
   under `set -euo pipefail` would abort on it. Unreachable in production because
   the upload precedes the listing, so the prefix always has at least one object.
6. **`receive` returns `True` without checking whether the `ON CONFLICT` insert
   actually inserted.** Two *genuinely concurrent* webhook deliveries of one id
   could therefore both submit. This is a residual of I4, not a regression: the
   old window was the length of a whole in-flight turn (10–30s); the new one is
   the length of one webhook transaction (milliseconds), and two submits inside
   the debounce coalesce into a single batch anyway. Closing it fully means having
   `messages.log` report its rowcount.
7. **M4 and M7**, and **the colleague-attribution gap** (org data returned by
   `memory.search` / `leads.query` with no owner, and nothing in the prompt saying
   org rows belong to colleagues). All three were correctly flagged by the
   implementer as out of brief; the attribution gap remains the largest open
   product gap in the prompt surface.
8. **Spec drift.** Spec §7's `merge-contacts` usage line lacks `--as`, and spec
   §5.2/§261 still references `registry.all()`. The code is right in both cases;
   the spec should be amended when it is next touched.
