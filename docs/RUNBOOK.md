# gaia-butler — operator's runbook

Orientation for whoever is on the hook for this system. What it is made of,
what every credential is for, where it runs, and the handful of commands you
will actually type.

**This file contains no secret values and must never contain any.** It names
credentials and says what they are for; the values live in `.env` on the
droplet (mode `600`) and nowhere else.

Related documents, deliberately not duplicated here:

| Document | Covers |
|---|---|
| [`README.md`](../README.md) | Architecture, the two visibility rules, code layout |
| [`deploy/README.md`](../deploy/README.md) | **First-time** setup: droplet, DNS, Meta onboarding, restore drill |
| `docs/superpowers/specs/` | Design documents for each feature |
| `docs/superpowers/backlog.md` | Decided-but-unbuilt work |

---

## 1. What this is

A WhatsApp assistant for the developers at Gaia Group Development. Meeting
notes go in — typed, or photographed handwriting — and structured summaries,
contact profiles, a lead pipeline and a morning follow-up digest come out.

It is multi-tenant within one company: everyone shares one WhatsApp number and
the assistant knows who is texting. **It holds a real client book** — names,
budgets, and what sellers said in confidence. Treat access to the droplet and
to backups the way you would treat access to that.

---

## 2. The stack

| Layer | What | Why this one |
|---|---|---|
| Messaging | **Meta WhatsApp Cloud API** (Graph `v21.0`) | The users already live in WhatsApp. One business number, many developers. |
| Web | **FastAPI** + **uvicorn**, one container (`app`) | Two routes only: `/health` and `/webhook`. |
| TLS | **Caddy 2** | Automatic Let's Encrypt. The only container publishing ports (80, 443). |
| Model | **Anthropic Claude** via the official `anthropic` SDK | Two models, two jobs — see §4. |
| Embeddings | **Voyage** `voyage-3.5-lite`, 1024 dims | Semantic search over past meetings. |
| Speech-to-text | **Deepgram Nova-3** | Claude accepts no audio input at all, so voice notes are transcribed before the model sees them. Chosen for keyterm prompting — general English is solved, rare client names are not. |
| Vector store | **pgvector** on **Postgres 17** (`pgvector/pgvector:pg17`) | RAG lives in the same database as everything else — one backup, one restore, one transaction. No second datastore to keep consistent. |
| Scheduler | A plain `jobs` container looping every 15 min | Per-user 08:00 local delivery is not one cron line once people have timezones. |
| Backups | Nightly `pg_dump` → **DigitalOcean Spaces**, 30-day retention | DO's own droplet backups are weekly; losing six days of meeting notes is not acceptable. |
| Host | One **DigitalOcean** droplet, Docker Compose | See §5. |

### Containers

| Service | Image | Role |
|---|---|---|
| `app` | built from `Dockerfile` | FastAPI: webhook, signature check, dedup, agent turns |
| `jobs` | same image | `python -m gaia.jobs.digest` — the 15-minute digest loop |
| `db` | `pgvector/pgvector:pg17` | Postgres + pgvector. **Publishes no ports.** |
| `caddy` | `caddy:2` | TLS termination, reverse proxy to `app:8000` |
| `backup` | `postgres:17-alpine` | Sleeps until 03:00 America/New_York, runs `deploy/backup.sh` |

`db` deliberately has no `ports:` block — it is reachable only on the compose
network. The one file that publishes 5432 is `deploy/compose.dev.yml`, for the
test suite, and compose does **not** auto-load it. It is not named
`docker-compose.override.yml` precisely so it cannot follow the repo onto the
droplet by accident.

---

## 3. Secrets — what each one is for

Every value below lives only in `.env` on the droplet. **None are in git**
(`.gitignore`), and none are in the image (`.dockerignore`).

| Variable | Used by | What it does | If it leaks |
|---|---|---|---|
| `DB_PASSWORD` | `db`, `app`, `jobs`, `backup` | Postgres password for the `gaia` role. Compose has no default — a missing value makes `docker compose` fail loudly rather than quietly standing up a client book behind a password published in this repo. | Full client book, if the attacker also reaches the compose network. Postgres publishes no port, so this is not directly internet-reachable. |
| `ANTHROPIC_API_KEY` | `app`, `jobs` | Every model call: agent turns and the digest composer. | Billable API usage on your account. Rotate in the Anthropic console. |
| `VOYAGE_API_KEY` | `app` | Embedding meeting text for semantic search. | Billable usage. Rotate in the Voyage console. |
| `DEEPGRAM_API_KEY` | `app` | Transcribes voice notes. Claude accepts no audio input, so this is what turns a dictated note into text the model can read. Nova-3, English, with the sender's contact roster sent as keyterms; every request sets `mip_opt_out=true`. | Billable usage, and someone could transcribe their own audio on your account. Rotate in the Deepgram console. |
| `WA_ACCESS_TOKEN` | `app`, `jobs` | Permanent System User token. Sends messages, marks read, downloads media. | **Someone can message your clients as you.** Rotate immediately in Meta Business Settings → System Users. |
| `WA_APP_SECRET` | `app` | Verifies the `X-Hub-Signature-256` on every inbound webhook. This is what stops anyone who finds the URL from injecting fake messages. | Forged inbound messages. Rotate in the Meta app dashboard. |
| `WA_VERIFY_TOKEN` | `app` | A string you invent. Meta echoes it once when you first subscribe the webhook. | Low. Only useful during webhook setup. |
| `WA_PHONE_NUMBER_ID` | `app`, `jobs` | Which WhatsApp business number to send from. Not a secret, but lives with them. | Not sensitive. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `backup` | DigitalOcean Spaces key pair. The `aws` CLI reads these exact names natively. | **Every nightly database dump.** This is the highest-value pair here — a backup is the whole client book in one file. Rotate in the DO control panel. |
| `SPACES_BUCKET` / `SPACES_ENDPOINT` | `backup` | Where dumps go. Currently `gaia-backups` at `nyc3`. | Not sensitive on their own. |
| `DOMAIN` | `caddy` **only** | The hostname Caddy provisions TLS for. | Not sensitive — it is public DNS. |

Two more variables exist in `.env` and are not credentials in their own right,
but the first embeds one and is a reliable source of confusion:

- **`DATABASE_URL`** — contains `DB_PASSWORD`. The value in `.env` is for a
  process on *your host* talking to the port `deploy/compose.dev.yml` publishes
  locally. In production it is **overridden** by `docker-compose.yml`'s
  `environment:` block (which beats `env_file:`) to
  `postgresql://gaia:...@db:5432/gaia`, the compose network name. Editing it in
  `.env` on the droplet therefore changes nothing; edit `DB_PASSWORD` instead.
- **`TEST_ADMIN_DSN`** — where the test suite creates and drops its
  per-process database. Local only; `conftest.py` defaults to exactly this, so
  you need it only if you differ.

**`caddy` gets `DOMAIN` and nothing else.** It is deliberately not given
`env_file: .env`: the public-facing TLS terminator has no use for the
Anthropic key, the WhatsApp token or the database password, and handing them
to it widens the blast radius of a Caddy compromise for no benefit.

### Rotating a credential

1. Generate the new value in the relevant console.
2. Edit `/opt/gaia-assistant/.env` on the droplet (keep mode `600`).
3. `docker compose up -d` — recreates the containers that read it.
4. Revoke the old value at the provider.

`DB_PASSWORD` is the exception: changing it means changing the password *in*
Postgres too (`ALTER ROLE gaia PASSWORD ...`), or the app cannot connect.

---

## 4. Models

Two settings, because there are two workloads with nothing in common.

| Setting | Model | Job |
|---|---|---|
| `MODEL` | `claude-opus-5` | The butler: the agent loop. Transcribes photographed handwriting, picks tools, does date arithmetic. |
| `DIGEST_MODEL` | `claude-sonnet-5` | The morning composer: one paragraph from a ~300-token JSON payload. No tools, no images. |

The butler stays on Opus — reading handwriting is the hardest thing this
product does and the reason it exists, so it is the last place to trade
quality for price.

`claude-haiku-4-5` is cheaper again for the digest and is **not** used: it
rejects `output_config.effort` with a `400` (which `compose_digest` sends), and
on a realistic payload it broke the prompt's plain-text rule with ALL-CAPS
headers. If you ever move it there, remove the `effort` parameter first.

Rate card for reporting lives in `gaia/core/stats.py`. Editing that dict
reprices every historical row, because cost is computed on read and never
stored.

---

## 5. The droplet

| | |
|---|---|
| Provider | DigitalOcean, NYC1 |
| Host | `ubuntu-s-1vcpu-2gb-nyc1` |
| Address | `137.184.192.175` — ssh alias `gaia` (`~/.ssh/config`, key `~/.ssh/gaia_droplet`) |
| OS | Ubuntu 24.04 LTS |
| Size | 1 vCPU, 2 GB RAM, 48 GB disk, **2 GB swap** |
| Repo path | `/opt/gaia-assistant` |
| Domain | `agents.gaiagroupdevelopment.com` |
| SSH user | `deploy` |

The 2 GB swap file is not optional — Postgres, Python and Caddy on 2 GB of RAM
have no headroom without it.

**Firewall:** inbound 80 and 443 from anywhere, 22 from your IP only. Postgres
is not exposed at all.

```bash
ssh gaia                                   # you are `deploy`
cd /opt/gaia-assistant
docker compose ps                          # what is running
docker compose logs app --since 15m        # recent app logs
docker compose logs jobs --since 1h        # digest loop
```

---

## 6. Custom utilities

One CLI, five subcommands, run inside the `app` container.

```bash
docker compose exec -T app python -m gaia.core.admin <subcommand>
```

### `add-user` — put someone on the roster

```bash
... admin add-user --name "Ana Ruiz" --phone 13055550001 --role developer \
                   --tz America/New_York
```

This is the bootstrap: **until one row exists in `users`, every inbound number
is ignored.** There is no in-band signup, by design — an assistant holding the
client book does not let strangers enrol themselves.

`--phone` is the country code with no `+`. `--tz` is validated against the IANA
database before the row is written; a typo used to mean that developer got an
error reply to every message forever, and nobody in the company got a digest.

### `list-users`

```bash
... admin list-users
```

### `deactivate` — take someone off the roster

```bash
... admin deactivate --phone 13055550001
```

Soft: the row and their data stay, they simply stop being served and stop
receiving digests.

### `merge-contacts` — fold one duplicate contact into another

```bash
... admin merge-contacts --from <contact-id> --into <contact-id> --as 13055550001
```

Two rows for the same person happen; the index on `lower(name)` is
deliberately non-unique because different people share names.

`--as` is required and is a safety property, not paperwork: the merge is scoped
to what that developer can see. Without it, anyone with a shell could fold a
private contact into an org-visible row and publish its profile to the whole
company — the exact failure the visibility system exists to prevent.

### `stats` — what it costs to run, and what it produced

```bash
... admin stats                 # last 30 days
... admin stats --days 7
```

For a page you can open on a laptop or a phone, pull it down over the ssh
session you already have — no scp step, no new port, nothing listening:

```bash
ssh gaia 'cd /opt/gaia-assistant && docker compose exec -T app \
    python -m gaia.core.admin stats --html' > report.html
```

The report carries **aggregates only** — no contact names, no meeting text — so
it is safe to screenshot and send. `model_calls` is the one table in the schema
with nowhere to put a client in it.

The number worth watching first is the **cache hit rate**. `core/llm.py`'s
`_system_blocks` puts the prompt-cache breakpoint after the stable prefix, on
the argument that the contact roster reorders on nearly every `save_meeting`.
That was a belief in a docstring until this report existed.

---

## 7. Routine operations

### Deploy a change

```bash
ssh gaia
cd /opt/gaia-assistant
git pull --ff-only origin main
docker compose up -d --build
```

Migrations apply themselves at startup (`gaia/core/db/migrate.py`, in filename
order, under an advisory lock, all-or-nothing). Then verify:

```bash
docker compose ps                                   # 0 restarts, db healthy
docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read())"
docker compose logs app jobs --since 3m | grep -iE "error|traceback"
```

### Back up and restore

Nightly at 03:00 America/New_York, automatically, to Spaces with 30-day
retention. On demand:

```bash
docker compose exec backup /deploy/backup.sh
docker compose exec backup /deploy/restore.sh /tmp/gaia-<stamp>.dump
```

**Run the restore drill before you need it.** An untested backup is not a
backup. Row counts must match.

### The database

```bash
docker compose exec -T db psql -U gaia -d gaia
```

Tables: `users`, `contacts`, `meetings`, `meeting_contacts`, `commitments`,
`leads`, `memory_chunks`, `messages`, `model_calls`.

---

## 8. What the assistant can actually do

Capabilities are directories under `gaia/capabilities/`. Adding a skill is
adding a directory, not editing a dispatcher.

| Capability | Tools |
|---|---|
| `meetings` | `save_meeting`, `search_memory`, `lookup_contact`, `set_meeting_visibility` |
| `leads` | `create_lead`, `query_leads`, `update_lead`, `list_commitments`, `complete_commitments` |

A capability is available to everyone unless it declares an allowlist.
`Registry.dispatch` re-checks visibility at call time — filtering the tool list
is presentation, that check is enforcement, so a hallucinated tool name cannot
execute.

---

## 9. Things that will bite you

Each of these cost someone a real debugging session. They are in the code as
comments too.

**WhatsApp's 24-hour window.** Free-form messages are only accepted within 24
hours of the user's last inbound message. Outside it the digest falls back to
the approved `daily_digest` template. A newly-onboarded developer who has never
texted the number has **no open window at all**, so their first digest has no
path to delivery except that template — which is why it is a deployment
prerequisite and not a nice-to-have. Register it as language `en`, not `en_US`:
Meta matches the tag exactly and `gaia/core/whatsapp.py` sends `en`.

**`visibility` governs reads, `user_id` governs responsibility.** Who may *see*
a row versus whose *job* it is. The digest and open commitments filter on
ownership, never on visibility — otherwise one developer gets nagged every
morning about a colleague's follow-ups. Conflating them produces bugs in both
directions.

**Derived rows inherit their parent's visibility.** A private meeting whose
memory chunk defaults to `org` is hidden from the meetings list and fully
searchable by the whole company — worse than no privacy, because it looks
private.

**Inbound messages are logged before the reply is attempted**, in a separate
transaction. If the model call fails afterwards, the user gets an apology and
their note is still on record. Rolling both back together would erase the only
evidence they ever wrote in.

**A user added mid-day is not due a digest until the next morning.** Their
08:00 has not come round yet. Rows from previous days keep the catch-up, so a
send that failed at 08:00 still goes out later.

**Nothing is recorded unless a send actually landed.** Not nudge counts, not
`last_digest_on`, not the message log. Recording an undelivered digest means
tomorrow's says "still open from yesterday" about something the user was never
told.

**The DB uses `journal_mode = DELETE`, not WAL**, on the local orchestration
side — WAL is unreliable on WSL2 NTFS mounts (`/mnt/c/`).

**The live test tier is billable and deselected by default.** `pytest` runs the
hermetic suite; `pytest -m live` hits the real Anthropic and Voyage APIs. A
change once left that tier red for a day because plain `pytest` stayed green.
Run it before deploying anything that touches a model call.

---

## 10. Local development

Requires Docker and Python 3.11+.

```bash
cp .env.example .env      # fill in; DB_PASSWORD has no default
docker compose -f docker-compose.yml -f deploy/compose.dev.yml up -d db
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # hermetic, ~240 tests
.venv/bin/python -m pytest -m live -q  # real APIs, billable
```

Tests run against `pgvector/pgvector:pg17` — the exact production image — and
drop and recreate a per-process database each run. pgvector behaviour is
load-bearing here and is not worth faking.
