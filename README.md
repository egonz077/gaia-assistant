# gaia-butler

A WhatsApp assistant for the developers at Gaia, a real-estate development company. Meeting
notes go in — typed, or photographed handwriting — and structured summaries,
contact profiles, a lead pipeline and a morning follow-up digest come out.

It is **multi-tenant within one company**: every developer on the roster uses the
same WhatsApp number, and the assistant knows who is texting. Data is visible
company-wide by default and private only when its owner says so. It holds a real
client book — names, budgets, and what sellers said in confidence — which is why
that distinction is the thing the codebase is most careful about.

```
WhatsApp ──webhook──▶ FastAPI ──▶ Claude (tools) ──▶ Postgres + pgvector
    ▲                                                       │
    └────── per-user morning digest (jobs/digest.py) ◀───────┘
```

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
    embeddings.py      Voyage embeddings
    admin.py           CLI: add-user, list-users, deactivate, merge-contacts
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
    leads/             create_lead, query_leads, update_lead, complete_commitment
  jobs/digest.py       per-user morning follow-up digest
migrations/001_init.sql
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

The isolation suite is parametrized over `DOMAIN_TABLES` and a second test
introspects `information_schema`, so adding a table with a `visibility` column
and no coverage breaks the build rather than passing quietly.

## Deployment

See **[deploy/README.md](deploy/README.md)** — DigitalOcean droplet, Caddy for
TLS, the Meta setup including the `daily_digest` template, nightly off-box
backups, and the restore drill. Run the restore drill before going live; an
untested backup is not a backup.

## Notes on WhatsApp

The Cloud API has a **24-hour customer service window**: free-form messages are
only accepted within 24 hours of the user's last inbound message. Outside it the
digest falls back to an approved template, which is why the `daily_digest`
template is a deployment prerequisite and not a nice-to-have — a
newly-onboarded developer who has not texted the number yet has no open window at
all, so their first digest has no other path to delivery.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-08-gaia-butler-design.md`
- Plan: `docs/superpowers/plans/2026-09-08-gaia-butler-increment-1.md`

## Next

Increment 2 is the scheduler: Google Calendar, proposing free times, creating
events on confirmation, and surfacing conflicts in the digest. Split out rather
than deferred, because its schedule depends on Google project setup rather than
on us. Also queued: voice notes through the same pipeline, and a consolidation
pass that rewrites `contacts.profile` into clean prose instead of the
append-only accretion it is today.
