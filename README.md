# gaia-butler

A WhatsApp assistant for the developers at Gaia Group Development. Meeting
notes go in — typed, photographed handwriting, or dictated on the walk to the
car — and structured summaries, contact profiles, a lead pipeline and a morning
follow-up digest come out.

It is **multi-tenant within one company**: every developer on the roster uses the
same WhatsApp number, and the assistant knows who is texting. Data is visible
company-wide by default and private only when its owner says so. It holds a real
client book — names, budgets, and what sellers said in confidence — which is why
that distinction is the thing the codebase is most careful about.

```
                    photo ──▶ downscale ─┐
WhatsApp ──webhook──▶ FastAPI            ├──▶ Claude (tools) ──▶ Postgres + pgvector
    ▲               voice ──▶ Deepgram ──┘         │                    │
    │                                              └──▶ Voyage ─────────┤
    └────── per-user morning digest (jobs/digest.py) ◀───────────────────┘
```

Claude accepts no audio input, so a voice note is transcribed before the model
sees it — with the sender's contact roster passed as keyterms, because general
English is solved and rare client names are not.

## The two rules everything follows

- **`visibility` governs reads.** Who may see a row. `org` by default,
  `private` on request. The one place read-filtering lives is
  `gaia/core/db/scope.py`; every read composes that fragment.
- **`user_id` governs responsibility.** Whose job this is. Anything that tells
  a person what to do — the digest, open commitments — filters on ownership,
  never on visibility. Otherwise Ana gets nagged every morning about Sofia's
  follow-ups, which are readable and emphatically not her work.

Conflating the two produces bugs in both directions, so `leads.due_for` and
`leads.query` sit next to each other querying one table with different filters
and a docstring on each explaining which question it answers.

Derived rows inherit their parent's visibility on both paths: repositories copy
it at insert time, and a Postgres trigger cascades a later change. A private
meeting whose memory chunk defaults to `org` is hidden from the meetings list
and fully searchable by the whole company — worse than no privacy, because it
looks private.

## Layout

```
gaia/
  main.py              FastAPI app: webhook, signature check, dedup, /health
  butler.py            one turn per user — inbound log, prompt, agent loop, reply
  core/
    config.py          settings from the environment, validated at import
    models.py          User
    whatsapp.py        Cloud API: parse, send_text, send_template, media
    images.py          downscale photos to the model's resolution ceiling
    llm.py             agent loop, prompt caching, stop-reason handling
    turns.py           per-user turn queue with a 3s debounce
    embeddings.py      Voyage embeddings
    transcription.py   Deepgram: voice notes to text, roster as keyterms
    usage.py           one model_calls row per model call; never raises
    stats.py           cost priced on read, product counts derived on read
    stats_html.py      the same numbers as one self-contained page
    admin.py           CLI: add-user, list-users, deactivate, merge-contacts,
                       stats
    db/
      pool.py          psycopg async pool, tx() context manager
      scope.py         visible() — the one place read-filtering lives
      users.py contacts.py meetings.py leads.py commitments.py
      memory.py        pgvector semantic search
      messages.py      per-user conversation log
      migrate.py       applies migrations/ at startup
  capabilities/
    base.py            Capability, Tool, Registry
    meetings/          save_meeting, search_memory, lookup_contact,
                       set_meeting_visibility
    leads/             create_lead, query_leads, update_lead, list_commitments,
                       complete_commitments, update_commitment
  jobs/digest.py       per-user morning follow-up digest
migrations/             001_init, 002_developer_role, 003_llm_calls,
                        004_model_calls, 005_voice_note_source
deploy/                backup.sh, restore.sh, compose.dev.yml, runbook
evals/smoke.py         live end-to-end check against the real Anthropic API
tests/
```

Adding a skill is adding one directory under `capabilities/`, not editing a
dispatcher: a `Capability` declares its tools and an optional prompt fragment,
registers itself, and the registry assembles per user. A capability is available
to everyone unless it declares an allowlist.

## Local development

Requires Docker and Python 3.11+.

```bash
cp .env.example .env      # fill in; DB_PASSWORD has no default

# The dev overlay publishes 5432 on loopback for the test suite. Production
# deliberately publishes no database port, so this file is not auto-loaded.
docker compose -f docker-compose.yml -f deploy/compose.dev.yml up -d db

python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Tests run against `pgvector/pgvector:pg17`, the exact production image, and drop
and recreate a per-process database each test. pgvector behaviour is
load-bearing here and is not worth faking.

There is a second tier that real services back, deselected by default because
it is billable:

```bash
.venv/bin/python -m pytest -m live -q    # real Anthropic, Voyage and Deepgram
```

Run it before deploying anything that touches a model call. It has been left
silently red for a day before, because plain `pytest` stayed green.

Several tests exist to fail on carelessness rather than to check a feature.
The isolation suite is parametrized over `DOMAIN_TABLES`; another introspects
`information_schema`, so a new table with a `visibility` column and no coverage
breaks the build; a third requires every read in `gaia/core/db/` to compose
`visible()` or say in writing why it does not. That last one is the guard that
matters — SQL injection is not a risk here, since psycopg parameterises every
value, but a new read that quietly returns a colleague's client is.

## Running it

**[docs/RUNBOOK.md](docs/RUNBOOK.md)** is the operator's document: the stack,
what every credential is for and what it costs if it leaks, the droplet, the
admin CLI, and the traps that have already cost someone a day.

**[deploy/README.md](deploy/README.md)** covers first-time setup — DigitalOcean
droplet, Caddy for TLS, the Meta onboarding including the `daily_digest`
template, nightly off-box backups, and the restore drill. Run the restore drill
before going live; an untested backup is not a backup.

What the product costs to run, and what it produced for the money:

```bash
docker compose exec -T app python -m gaia.core.admin stats --days 7
```

Every model call writes a row. Cost is computed when the report is read, from a
rate card in `gaia/core/stats.py`, so editing that dict reprices history rather
than leaving old rows quietly wrong.

## Notes on WhatsApp

The Cloud API has a **24-hour customer service window**: free-form messages are
only accepted within 24 hours of the user's last inbound message. Outside it the
digest falls back to an approved template, which is why the `daily_digest`
template is a deployment prerequisite and not a nice-to-have — a
newly-onboarded developer who has not texted the number yet has no open window at
all, so their first digest has no other path to delivery.

## Design documents

One design document per feature, carrying the reasoning and the options that
were rejected. Read the relevant one before changing that feature.

| | |
|---|---|
| `CLAUDE.md` | Orientation: the invariants, the tripwires, where everything lives. |
| `docs/superpowers/specs/` | Designs — the original increment, observability, voice notes, and two not yet built. |
| `docs/superpowers/plans/` | Implementation plans, task by task. |
| `docs/superpowers/research/` | Findings behind a spec, including why Claude cannot take audio. |
| `docs/superpowers/backlog.md` | Decided but unbuilt, with the reasoning. |

The git history is part of this. Commit messages carry what broke, what it
cost, and why this fix rather than the obvious one — more than once they have
been the fastest route to a diagnosis.

## Next

Increment 2 is the scheduler: Google Calendar, proposing free times, creating
events on confirmation, and surfacing conflicts in the digest. Split out rather
than deferred, because its schedule depends on Google project setup rather than
on us.

Also specified and unbuilt: a consolidation pass that rewrites
`contacts.profile` into clean prose instead of the append-only accretion it is
today, and an onboarding interview for new developers. Both have design
documents.
