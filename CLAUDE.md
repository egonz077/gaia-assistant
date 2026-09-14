# gaia-butler

A WhatsApp assistant for the developers at Gaia Group Development. Meeting notes
go in — typed, or photographed handwriting — and structured summaries, contact
profiles, a lead pipeline and a morning digest come out.

**It holds a real client book**: names, budgets, and what sellers said in
confidence. That is why the two rules below exist and why they are worth reading
before touching anything.

> The `CLAUDE.md` one directory up describes a **different project** — a
> multi-agent orchestrator. It does not apply here.

## The two rules everything follows

- **`visibility` governs reads.** Who may *see* a row. `org` by default,
  `private` on request. The one place read-filtering lives is
  `gaia/core/db/scope.py`; every read composes `visible()`.
- **`user_id` governs responsibility.** Whose *job* this is. Anything that tells
  a person what to do — the digest, open commitments — filters on ownership,
  never on visibility. Otherwise one developer is nagged every morning about a
  colleague's follow-ups.

Conflating them produces bugs in both directions. `leads.due_for` and
`leads.query` sit next to each other querying one table with different filters,
with a docstring on each saying which question it answers. `commitments.open_for`
and `commitments.query` are the same pair.

Derived rows inherit their parent's visibility on both paths — repositories copy
it at insert, a trigger cascades a later change. A private meeting whose memory
chunk defaults to `org` is worse than no privacy, because it looks private.

## Where things are

| | |
|---|---|
| `docs/RUNBOOK.md` | **Start here for operations.** Stack, what every credential is for, droplet, the admin CLI, and the traps that have already cost someone a day. |
| `deploy/README.md` | First-time setup: droplet, DNS, Meta onboarding, restore drill. |
| `docs/superpowers/specs/` | One design doc per feature. Read the relevant one before changing that feature. |
| `docs/superpowers/plans/` | Implementation plans, task-by-task. |
| `docs/superpowers/research/` | Findings that informed a spec. |
| `docs/superpowers/backlog.md` | Decided but unbuilt, with the reasoning. |
| `README.md` | Architecture and code layout. |

**The git history is documentation here.** Commit messages carry the *why* and
have repeatedly been the fastest route to a diagnosis. Keep writing them that
way: what broke, what it cost, why this fix and not the obvious one.

## Testing

```bash
.venv/bin/python -m pytest -q          # hermetic, ~240 tests, always run this
.venv/bin/python -m pytest -m live -q  # real Anthropic/Voyage APIs — BILLABLE
```

The live tier is deselected by default. It has been left silently red for a day
before (`eff08ce`), because plain `pytest` stayed green. **Run it before
deploying anything that touches a model call.**

Tests need the dev database:

```bash
docker compose -f docker-compose.yml -f deploy/compose.dev.yml up -d db
```

### Tripwires that will fail the build, by design

- `tests/test_scope.py` — a new table with a `visibility` column must be in
  `DOMAIN_TABLES`. `model_calls` deliberately has neither.
- `tests/test_db_signatures.py` — everything in `gaia/core/db/` takes
  `(conn, user, ...)` unless explicitly exempted.
- `tests/test_scope.py` — every read in `gaia/core/db/` either composes
  `visible()` or is listed in `OWNERSHIP_SCOPED` **with a reason**. Injection
  is not the risk here (psycopg parameterises every value); a new read that
  quietly returns a colleague's client is. Adding a read forces the decision.
- `tests/test_isolation.py` — parametrized over `DOMAIN_TABLES`; a new domain
  table with no coverage breaks the build rather than passing quietly.

### Two traps that have bitten before

- **The `ana` fixture is uncommitted.** Anything opening its own transaction on
  another pooled connection — `usage.record`, every capability tool — cannot see
  it, and the insert dies on a foreign key. Commit the user first (see
  `tests/test_usage.py`).
- **`tx()` sets `row_factory` on a pooled connection**, and that rides back into
  the pool. A bare `pool.connection()` yields tuples or dicts depending on which
  connection you get. Set it explicitly in tests.

## Conventions

- **TDD.** Write the test, watch it fail for the right reason, then implement.
  If a test passes the moment you write it, say so — it is a contract test, not
  a regression guard.
- **Comments carry reasoning, not description.** The house style explains why a
  line exists and what went wrong without it. Match it.
- **Never commit a secret.** `.env` is gitignored and excluded from the image.
  `docs/RUNBOOK.md` names credentials and their purpose, never values.
- **No test-aware production code.** No `if TESTING:`, no env var that only
  tests set, no parameter defaulting to off whose only caller is a test. Pass a
  seam instead — `usage.record` takes a required `pool` for exactly this reason.

## Deploying

Production is a DigitalOcean droplet, ssh alias `gaia`, repo at
`/opt/gaia-assistant`. **Ask before writing to it.** Reads are fine.

```bash
ssh gaia 'cd /opt/gaia-assistant && git pull --ff-only origin main && docker compose up -d --build'
```

Migrations apply themselves at startup. Verify afterwards: containers running
with 0 restarts, `/health` returning ok, logs clean.
