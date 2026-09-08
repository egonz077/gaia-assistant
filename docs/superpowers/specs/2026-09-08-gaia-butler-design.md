# gaia-butler — Design

**Date:** 2026-09-08
**Status:** Approved for planning
**Supersedes:** the single-user `wife-agent` prototype in `app/`

## 1. What this is

A WhatsApp assistant for the agents at Gaia. Each agent texts one number.
They send meeting notes — typed or photographed handwriting — and the butler
transcribes, extracts structure, files it, and answers questions about the
history later. Every morning it sends each agent a digest of what needs
follow-up.

The prototype worked for exactly one person. This design makes it work for the
company, and adds room for capabilities beyond meeting notes without a rewrite.

### Goals

1. **N users.** Any Gaia agent on the roster can use it, from one WhatsApp number.
2. **Public by default, private on request.** Data is visible company-wide
   unless its owner marks it private. A capability is available to everyone
   unless it declares an allowlist.
3. **Pluggable capabilities.** Adding a new skill to the butler is adding one
   directory, not editing a dispatcher.
4. **Actually works.** Every defect from the code review is fixed here, and the
   lead pipeline — which the prototype could never populate — works end to end.

### Delivery increments

**Increment 1 — this spec.** Multi-tenancy, the capability registry, meetings,
leads, the admin CLI, and the deployment. Every review defect fixed. This is what
gets Gaia a working bot.

**Increment 2 — the scheduler.** Google Calendar: propose free times, create
events on confirmation, surface conflicts in the digest. Deliberately split out
rather than deferred — it is the highest-value capability, and it is also the one
whose schedule depends on Google project setup rather than on us. Blocking a
currently-broken bot behind an OAuth consent screen is the wrong risk. It gets
its own spec and lands on a foundation already proven in production.

Gaia runs Google Workspace, so the OAuth app is configured **Internal**: no
Google verification review, no unverified-app warning, no per-agent test-user
list. This is the single biggest simplification available to increment 2 and the
reason it should stay a small project. It does add a web surface — `/oauth/start`
and `/oauth/callback`, since consent cannot happen inside WhatsApp — and refresh
tokens in Postgres, encrypted with a key from `.env`, because a refresh token is
standing access to someone's calendar.

### Non-goals for v1

- **Invite-code enrollment.** Onboarding is the admin CLI. In-band enrollment
  (an admin issues a single-use code, the new agent texts it as their first
  message) is designed for and cheap to add — one table and one branch in the
  handler — but at Gaia's headcount it solves a problem that does not exist yet.
  Note that it never removes the CLI: something has to create the first admin.
- **Web admin UI.** Roster management is a CLI on the droplet.
- **Multiple organizations.** Gaia is the only tenant. Rows carry `user_id`, not
  `org_id`. Adding orgs later means one column and one filter, and there is no
  reason to pay for it now.
- **Voice notes, weekly profile consolidation.** Later capabilities.

## 2. Architecture

```
                        ┌───────────────────────────────┐
  WhatsApp ──webhook──▶ │ main.py — verify sig, 200 OK   │
                        │ then hand off to background    │
                        └───────────────┬────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │ core/butler.py                 │
                        │  resolve user by wa_id         │
                        │  assemble visible capabilities │
                        │  run the tool loop             │
                        └───────┬───────────────┬────────┘
                                ▼               ▼
                     capabilities/*        core/db/*
                     (tools + prompt)      (scoped by user)
                                                │
   jobs/digest.py ── every 15 min ──────────────┘
   sends each user their 8am-local digest
```

Three layers, and the boundaries are the point:

- **`core/`** knows about WhatsApp, Postgres, Claude, users, and visibility. It
  knows nothing about meetings or leads.
- **`capabilities/`** knows about the business domain. Each capability declares
  tools and a prompt fragment. It receives an authenticated `User` and never
  reaches around the scoping helpers.
- **`jobs/`** are scheduled entry points that reuse both.

### 2.1 Repo layout

```
gaia/
  core/
    config.py          settings from env, validated at import
    models.py          User, Visibility, Capability dataclasses
    db/
      pool.py          psycopg connection pool
      scope.py         visible_to() — the one place filtering lives
      users.py         roster lookup, wa_id → User
      contacts.py
      meetings.py
      leads.py
      commitments.py
      memory.py        Voyage embeddings + pgvector search
      messages.py      per-user conversation log
    whatsapp.py        Cloud API: parse, send_text, send_template, media
    images.py          downscale to the model's resolution ceiling
    llm.py             Anthropic client, agent loop, caching, stop-reason handling
    registry.py        capability registration + per-user assembly
    admin.py           CLI: add-user, list-users, deactivate, merge-contacts
  capabilities/
    base.py            Capability, Tool, Visibility protocol
    meetings/          save_meeting, search_memory
    leads/             create_lead, query_leads, update_lead, complete_commitment
  jobs/
    digest.py          per-user morning follow-up digest
  main.py              FastAPI app
migrations/
  001_init.sql
tests/
```

The prototype's `app/` is replaced wholesale. Nothing in it is worth carrying
forward unchanged, and it holds no production data — deployment has not happened
yet.

## 3. Data model

### 3.1 Users and visibility

```sql
CREATE TYPE visibility AS ENUM ('org', 'private');

CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    wa_id      TEXT NOT NULL UNIQUE,     -- country code, no '+'
    role       TEXT NOT NULL DEFAULT 'agent',   -- 'agent' | 'admin'
    timezone   TEXT NOT NULL DEFAULT 'America/New_York',
    active     BOOLEAN NOT NULL DEFAULT true,
    last_inbound_at TIMESTAMPTZ,          -- drives the 24h window check
    last_digest_on  DATE,                 -- digest idempotency, in the user's tz
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Every domain table — `contacts`, `leads`, `meetings`, `commitments`,
`memory_chunks` — gains two columns:

```sql
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    visibility visibility NOT NULL DEFAULT 'org',
```

`user_id` is the owner: whoever filed it. `visibility` is who else can read it.
`ON DELETE RESTRICT` because deleting a user must not silently destroy the
company's record of their deals; deactivate instead.

**`messages` is different.** A user's WhatsApp thread with the butler is a chat
log, not business data. It carries `user_id NOT NULL` and has no `visibility`
column — it is always owner-only. Sofia sharing Ana's lead pipeline must not
mean reading Ana's conversation.

### 3.2 The one filtering rule

```python
# core/db/scope.py
VISIBLE = "({t}.visibility = 'org' OR {t}.user_id = %(scope_user_id)s)"
```

Every read against a domain table composes this fragment. Every repository
function — reader or writer — takes `user: User` as its first positional
argument. This is enforced by a test that reflects over `gaia.core.db.*` and
fails if any public function's first parameter is not named `user`. Writers need
it as much as readers: an insert has to stamp the correct owner, and an update
has to prove the caller may touch the row. A forgotten filter is a data leak, so
it gets a guardrail rather than a code-review convention.

Because the default is `org`, the filter only ever excludes deliberately-private
rows. That keeps the blast radius of a mistake small and is why application-level
scoping is sufficient here; Postgres row-level security is the escalation if
Gaia ever takes on a second organization.

### 3.3 Fixes folded into the schema

- **`leads` can finally be created.** The prototype read and updated the table
  but never inserted into it, so `query_leads` always returned empty and the
  morning digest could only ever see commitments. A `create_lead` tool now
  exists, and `save_meeting` links leads it learns about.
- **`meetings.happened_at` is supplied by the model,** defaulting to now. Notes
  photographed the next morning file under the day the meeting happened.
- **Re-nagging.** `leads` and `commitments` gain `nudge_count INT NOT NULL
  DEFAULT 0` and `last_nudged_at TIMESTAMPTZ`. The digest passes both to the
  model, so day three reads "still open from Tuesday" and day four offers to
  snooze or close it, instead of repeating an identical line until she mutes the
  thread.
- **Contact lookup** gets `CREATE INDEX ON contacts (lower(name))`. Deliberately
  not unique: two different people share a name often enough that a unique
  constraint would force bad data. `admin merge-contacts` handles duplicates.
- **`memory_chunks` carries `user_id` and `visibility`,** and the vector search
  applies the scope fragment. Semantic search over a private note must not
  surface it to another agent.

## 4. Capabilities

```python
@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    tools: list[Tool]
    prompt_fragment: str = ""
    visibility: CapVisibility = PUBLIC     # PUBLIC | Allowlist(users=[], roles=[])

    def visible_to(self, user: User) -> bool: ...
```

At message time the butler assembles the request for that specific user:

```python
caps   = [c for c in registry.all() if c.visible_to(user)]
tools  = [t for c in caps for t in c.tools]
system = BASE_PROMPT + "".join(c.prompt_fragment for c in caps)
```

A private capability is not merely blocked — it is absent from the tool list, so
the model never mentions a capability the user cannot access.

**Dispatch re-checks.** `registry.dispatch(user, tool_name, args)` looks up the
owning capability and re-verifies `visible_to(user)` before calling the handler.
Filtering the tool list is presentation; the check at dispatch is enforcement,
and a hallucinated tool name from a private capability must not execute.

Every handler has the signature `handler(user: User, args: dict) -> dict` — the
user is not optional and not inferred from ambient state.

**Adding a capability** is: a directory under `capabilities/`, a `Capability`
instance, an import in `capabilities/__init__.py`. No dispatcher edit, no router.
When a future bot genuinely needs its own personality and thread, this registry
is most of what an agent router needs — it becomes an additive change.

## 5. Message flow

1. `POST /webhook` verifies the Meta signature and **returns 200 immediately**,
   handing work to `BackgroundTasks`. The prototype ran the full agent loop
   inline — 10-30s on a photo — which Meta retries and eventually disables the
   subscription over.
2. Resolve `wa_id` → `User`. Unknown or inactive numbers are logged and dropped.
3. Dedup on `wa_msg_id`; WhatsApp redelivers.
4. Build content blocks. Images are downloaded, downscaled to 2576px on the long
   edge, re-encoded JPEG.
5. Log the inbound message, then fetch history with
   `recent_messages(user, exclude_wa_id=msg.id)`. Logging first is crash-safe for
   dedup; excluding by id is what stops the prototype's bug where the current
   message appeared twice in the request — once as history text without its
   image, once as the real content.
6. Run the tool loop, capped at 8 iterations.
7. Log and send the reply.

### 5.1 Model configuration

`claude-opus-5`, `max_tokens=8000`, `output_config={"effort": "low"}` for chat
turns. Notes:

- Thinking is on by default on current models and thinking tokens count against
  `max_tokens` — the prototype's `max_tokens=1500` would truncate replies
  mid-thought.
- Prompt caching on the tools + base system prefix, which is stable per user.
  Contact profiles go after the breakpoint since they change often.
- `stop_reason` is handled explicitly: `tool_use` loops, `max_tokens` logs and
  returns what it has, `refusal` sends a neutral fallback. An empty text response
  never reaches WhatsApp, which rejects an empty body.

**Cost, so the model choice is yours to make deliberately.** At roughly 4k input
/ 600 output per message and ~40 messages a day across the team: Opus 5 lands
near $42/mo, Sonnet 5 near $17/mo, before caching knocks a chunk off the input
side. Both handle this workload; Opus is the better transcriber of bad
handwriting, which is the hardest thing this app does. It is one env var —
`MODEL` — so it can be changed after seeing real transcription quality.

### 5.2 The 24-hour window

WhatsApp only permits free-form messages within 24h of the user's last inbound
message. The morning digest breaks this the first day someone goes quiet — the
prototype flagged this in its README and did nothing about it, and worse, never
checked the send response, so the failure was silent.

`users.last_inbound_at` drives the decision. Inside the window, `send_text`.
Outside it, `send_template` with a registered utility template. Every send checks
the response status and logs failures. Registering the template in Meta Business
Manager is a deployment prerequisite, not an afterthought.

## 6. Jobs

`jobs/digest.py` runs every 15 minutes as its own compose service and sends to
each active user whose *local* time has just crossed 08:00. Per-user timezones
mean this is not expressible as a single cron line.

Sending sets `users.last_digest_on` to the user's local date, and a user whose
`last_digest_on` already equals today is skipped. Without that, a container
restart inside the 15-minute window sends the digest twice.

In-container cron is gone. Cron jobs run with a near-empty environment and do not
inherit the container's — the prototype's digest would have died on its first
`os.environ[...]` lookup, every day, silently.

Nothing to say means nothing sent. A digest that fires on empty days trains
people to ignore it.

## 7. Admin CLI

```
python -m gaia.admin add-user --name "Ana" --phone 13055551234 [--role admin] [--tz America/New_York]
python -m gaia.admin list-users
python -m gaia.admin deactivate --phone 13055551234
python -m gaia.admin merge-contacts --from <id> --into <id>
```

Deactivation is reversible and preserves history; there is no user deletion.

## 8. Deployment (DigitalOcean droplet)

**Droplet:** Ubuntu 24.04 LTS, 2GB / 1 vCPU / 50GB, NYC region (nearest to
Miami), plus a 2GB swap file. Postgres with pgvector, Python, and Caddy do not
fit comfortably in 1GB and will OOM during index builds. The sizing is for those
three coexisting, not for data volume — a few thousand meetings and their
embeddings is megabytes.

**Services:** `db` (healthchecked), `app` (FastAPI, runs migrations at startup),
`jobs` (the 15-minute digest scheduler), `caddy` (TLS).

### 8.1 Persistence

Three layers, and only the third survives losing the droplet:

1. **Container filesystem** — dies with the container. Never data.
2. **Docker named volume** (`pgdata`) — on the droplet's root disk. Survives
   `compose down`, rebuilds, reboots, image upgrades. This is the existing setup
   and it is correct.
3. **Off-box** — nightly `pg_dump -Fc` to DigitalOcean Spaces, 30-day retention.

Layer 2 is exactly as durable as the droplet: a deleted droplet, a corrupted
disk, an errant `docker volume rm`, or a botched Postgres major-version upgrade
takes the company's entire client book. **Perform one real restore before going
live** — an untested backup is not a backup. Enable DO's droplet backups as a
cheap floor, but they run weekly, and losing six days of meeting notes is not an
acceptable worst case.

**Pin `pgvector/pgvector:pg17` and leave it pinned.** A major-version bump makes
the existing data directory unreadable; Postgres refuses to start and the fix is
`pg_upgrade` or dump/restore. A floating tag turns that into a surprise.

### 8.2 Migrations, not initdb

The prototype mounts `schema.sql` into `/docker-entrypoint-initdb.d/`, which runs
**only when the data directory is empty** — first boot and never again. Every
later schema change silently does not happen, and the symptom is a query
referencing a column that was never created.

Replaced by numbered files in `migrations/`, a `schema_migrations` table, and a
runner invoked at app startup. An initdb mount is fine for a throwaway and a
liability for anything that will change.

### 8.3 Everything else

- **Caddy gets `env_file: .env`.** Without it `{$DOMAIN}` expands to nothing,
  Caddy fails to start, and Meta's webhook verification hits a host with no TLS.
- **Postgres healthcheck** with `depends_on: condition: service_healthy`.
- **`TZ=America/New_York`** on every container; per-user timezones handle the
  rest. The prototype computed "today" in UTC, so after 8pm Miami it filed
  commitments under tomorrow.
- **Firewall**: 80/443 open to the world, 22 to a known IP. Postgres publishes no
  ports and is reachable only on the compose network — an exposed 5432 is how
  these get owned.
- **`/health`** endpoint for uptime monitoring.
- **`.gitignore` and `.dockerignore`** before the first commit. The repo has
  neither, nothing stops `.env` being committed, and `__pycache__` is currently
  copied into the image.

## 9. Testing

There are currently zero tests. This is rebuilt test-first.

- **pytest + pytest-asyncio**, real Postgres as a `db-test` compose service.
  pgvector behavior is load-bearing and not worth faking.
- **`FakeWhatsApp`** records sends; **`FakeAnthropic`** returns scripted turns,
  including a tool_use-then-text sequence.
- Fixtures `user_ana` and `user_sofia` exist for every isolation test.

Tests that must exist before the code they cover:

| Area | Test |
|---|---|
| Isolation | Sofia cannot read Ana's private lead, contact, meeting, or memory chunk |
| Isolation | Sofia never sees Ana's message thread, even for `org` data |
| Isolation | Semantic search excludes another user's private chunks |
| Access | A private capability's tools are absent from that user's tool list |
| Access | Dispatching a private capability's tool by name is refused |
| Scoping | Every public reader in `core/db` takes `user` first (reflection test) |
| Webhook | Bad signature → 403; unknown sender → dropped; duplicate id → no-op |
| Webhook | Handler returns 200 before the agent loop completes |
| Leads | A lead created via the tool appears in the owner's digest |
| Digest | Silent on an empty day; escalates wording as `nudge_count` rises |
| Window | Outside 24h the digest uses a template, not free-form text |
| Agent loop | Empty text is never sent; iteration cap terminates |

## 10. Review defects, and where each is handled

| # | Defect | Section |
|---|---|---|
| 1 | Leads never created | 3.3 |
| 2 | Cron has no environment | 6 |
| 3 | Caddy never sees `DOMAIN` | 8 |
| 4 | Webhook blocks on the agent loop | 5 |
| 5 | Current message sent twice | 5 |
| 6 | Everything computed in UTC | 3.1, 8 |
| 7 | `happened_at` always now | 3.3 |
| 8 | Send failures silent | 5.2 |
| 9 | Digest re-nags identically | 3.3, 6 |
| 10 | Empty reply sent to WhatsApp | 5.1 |
| 11 | Obsolete model id | 5.1 |
| 12 | `max_tokens` too low for thinking | 5.1 |
| 13 | No prompt caching | 5.1 |
| 14 | Full-resolution images | 5 |

## 11. Settled decisions

- **Repo directory stays `gaia-assistant`.** The product is gaia-butler; the
  Python package is `gaia`. Not worth a rename.
- **Capabilities, not routed agents** (§4). A router becomes an additive change
  on top of the registry if a future bot ever needs its own thread and voice.
- **Public by default, private opt-in** — applied to both data and capabilities.
- **Application-level scoping, not Postgres RLS** (§3.2). Revisit if Gaia ever
  onboards a second organization.
- **Admin CLI ships in increment 1.** It is the bootstrap: until one row exists
  in `users`, every number is ignored.
- **Scheduler is increment 2**, not deferred.

## 12. Prerequisites and open items

- **WhatsApp message template** for the out-of-window digest needs registering in
  Meta Business Manager and one-time approval. Utility templates usually clear
  quickly, but the digest is unreliable without it — start this early, it is the
  only increment-1 dependency outside our control.
- **Git identity** is unset in this repo and must be configured before the first
  commit.
- **DigitalOcean Spaces bucket** and credentials for backups.
- **Model choice** (§5.1) is specced as `claude-opus-5` and is one env var. Worth
  revisiting after seeing real handwriting transcription quality against the
  roughly 2.5x cost difference versus Sonnet 5.
