Ruling: Local tests run against pip-installed `pgserver`, not docker compose.
  — Docker is unavailable and cannot be installed without the user's password;
  — Cost if wrong: the test harness differs from production (PG16 local vs PG17
Ruling: Drop `CREATE EXTENSION pgcrypto` from migrations/001_init.sql.
  — `gen_random_uuid()` has been in core since PG13; the production image is
  — Cost if wrong: nil. Nothing in the plan uses another pgcrypto function.
Ruling: Target Python 3.11 locally; keep `python:3.12-slim` in the Dockerfile.
  — pyenv only has 3.11.11 and 3.12 cannot be built without sudo. Every
  — Cost if wrong: a 3.12-only feature could slip in untested locally. None is
Ruling: Work on branch `feat/gaia-butler-increment-1` in place, not a separate
  — The pip environment is expensive to reproduce (pgserver bundles a Postgres
  — Cost if wrong: no parallel-work isolation. Nothing else is in flight.
Ruling: `tests/conftest.py` sets required env defaults in `os.environ` at the
  — Cost if wrong: none; it only affects test bootstrap.
Ruling: the predicate becomes
  — Cost if wrong: nil; this is strictly a correctness fix. Left alone it would
Ruling: the code is authoritative — `handle_turn(user, batch, wa)`. It opens two
  — Cost if wrong: a reviewer flags the mismatch; the ruling is recorded here.
Ruling: use the `lifespan` async context manager instead. Same behaviour, no
  — Cost if wrong: nil.
Ruling: Dispatch Tasks 1 and 2 as ONE unit ("Foundation").
  — Task 1's test harness is docker-compose based and superseded by the
  — Cost if wrong: a coarser review surface for the foundation. Both tasks are
Ruling: FIX the Critical. `tx()` must ensure the pool is open rather than
  — All three of the plan's entry points (main lifespan, admin CLI, digest job)
  — Cost if wrong: one redundant idempotent call per transaction. Negligible.
Ruling: FIX the Important, but NOT by swapping in pg_advisory_xact_lock alone —
  — Postgres DDL is transactional, so the whole batch becomes atomic: a failure
  — Cost if wrong: one transaction is held for the duration of a migration run.
Ruling: also fix the controller-spotted gap — add `pgserver` to
  — Cost if wrong: nil.
Ruling: fix the Important AND promote one reviewer-minor into the same round —
  — This CLI is the bootstrap: if `add-user` is broken, nobody can onboard and
  — Cost if wrong: one extra optional parameter on an internal function.
Ruling: PARK the get_or_create TOCTOU race (contacts.py:16-23).
  — The spec (§3.5) deliberately chose NOT to put a unique constraint on contact
  — Cost if wrong: occasional duplicate contact rows, cleaned up by merge-contacts.
Ruling: FIX the unused `user` in merge_profile (contacts.py:50-67).
  — This is a WRITE path in a multi-tenant system that accepts an authenticated
  — Cost if wrong: nil. Strictly narrows what the write can touch.
Ruling: KEEP pgserver as the test harness; use Docker only to validate the
  — Six tasks and 57 tests are built on the pgserver conftest; it runs the full
  — What Docker buys that pgserver cannot: proof that docker-compose.yml, the
  — Cost if wrong: the local harness (PG16 via pgserver) differs from production
  — not pgserver. Commit 74ae73f.
  — My earlier reasoning ("switching would churn every test file") was wrong. The
  — pgserver removed from dev deps. ENVIRONMENT.md rewritten so later implementers
  — Cost if wrong: tests now need Docker running. That is the deployment target
Ruling: FIX all three coverage gaps in one round — they are cheap and two of them cover
  — contact_name is a model-supplied argument on search_memory reaching a SQL filter;
  — The dimension gap is the subtle one: fake_embed hardcodes 1024 independently of
  — Cost if wrong: three small tests and one constant.
Ruling: PARALLELISE from here. Tasks 9 and 11 are dispatched concurrently with
  — Verified file-set disjointness before dispatching: Task 8 touched
  — Both implementers were told explicitly not to `git add -A` / `commit -a`, so
  — Remaining shape: 9 ∥ 11 -> 10 -> 13 ∥ 14 -> 12 -> 15 -> 16.
  — Cost if wrong: a git conflict between two implementers, recoverable by
Ruling: FIX the coverage gap, and promote one Minor into the same round.
  — visible_to IS the access-control model. Four untested branches means an and/or
  — Promoted Minor: dispatch returns the raw exception string to the model
  — Cost if wrong: four small tests and a slightly less informative tool-error string.
Ruling: PROMOTE the `send_template` zero-coverage minor to a fix round.
  — send_template is the 24-hour-window fallback: the mechanism that lets the morning
  — _post already has a stubbed-transport harness, so this is cheap.
  — Cost if wrong: one more test.
Ruling: dispatch Tasks 13 and 14 in parallel, but FORBID BOTH from touching
  — Their tests import CAPABILITY and the handler functions directly, so registration
  — Cost if wrong: one extra controller commit, and registration is briefly unexercised
Ruling: FIX items 1 and 2 (untested batching, untested whitespace-only fallback).
  — Batching is CORRECT but every fake response scripts a single ToolUseBlock, so the
  — Cost if wrong: two small tests.
Ruling: FIX item 3 by making run_agent NOT mutate the caller's list.
  — No caller depends on the mutation (Task 12 logs only the final reply; Task 15 does
  — Cost if wrong: one list copy per turn. Negligible at this volume.
Ruling: DEFER the Minor (failed tool results lack `is_error: true`).
  — Setting it requires run_agent to distinguish success from failure, but dispatch
  — Cost if wrong: slightly weaker signal to the model on tool failure.
  — This was a real cost of parallelising: it cost a false alarm and could have caused a
Ruling: add BOTH — the set_meeting_visibility tool AND meeting_id in search_memory's
  — Cost if wrong: one extra field in a tool result and one more tool in the model's
Ruling: also fix the lookup_contact test gap in the same round (my brief's omission) and
Ruling: FIX, and note this is a REGRESSION I introduced. The PROTOTYPE handled it —
  — Two parts: (a) the turn path must tell the user when it fails, converting silence
  — Cost if wrong: an occasional apology message she did not need. Vastly better than
Ruling: DEFER the Minor (_locks grows one asyncio.Lock per user forever). At a
Ruling: PROMOTE the caption-drop minor to a fix; defer the other.
  — When an image fails, only a fixed "[a photo ... could not be downloaded]" note
  — Bundling the second minor (apology not written to `messages`) since it is the same
  — Cost if wrong: a slightly longer content block and one extra logged row.
Ruling: REVERT to the brief's semantics — NULL means OUTSIDE the window, use the template
  — and fix the test fixture instead.
  — Changing product behaviour to satisfy a test is the anti-pattern this whole review
  — Cost if wrong: a newly-onboarded user gets her first digest via template rather than
Ruling: FIX all three Important in one round.
  — (1) is the serious one: `sleep 86400` is not anchored to a wall-clock time, so the
  — (2) PGPASSWORD lacks the `:-devpassword` fallback its three sibling services all have;
  — (3) The runbook sequences Meta webhook verification BEFORE the First Run step that
  — Cost if wrong: trivial in all three cases.
Ruling: DEFER the three Minors (prune-only failure reports "backup failed" though the
  — and (b) it logs "backup failed" EVERY night despite the backup working, which means a
Ruling: FIX with portable epoch arithmetic — `date -u -d @$((now - 30*86400))` works on
  — Cost if wrong: nil; it is strictly more portable than what is there.
Ruling: fix all 3 Criticals plus I1-I9 and I11 in ONE fix wave (the skill's prescribed
  — Cost if wrong: a large single diff to re-review. Justified: per-finding fixers each
Ruling: REVISIT the Task 5 parked TOCTOU race. I parked it citing `admin merge-contacts`
  — Cost if wrong: ~30 lines of CLI. Cheaper than leaving a ruling built on a premise
Ruling: FIX — inject the weekday alongside the date. One line, removes a whole class of
  — Cost if wrong: a few more tokens in a cached block.
Ruling: fix both. NEW-1 by actually preserving the fact privately so the claim becomes
  — Cost if wrong: a slightly larger raw_input, and one extra compose file.
