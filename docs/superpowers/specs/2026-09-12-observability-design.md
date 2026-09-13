# Observability — Design

**Status:** designed, not implemented. Implementation plan not written yet.
**Depends on:** nothing. The consolidation job (`2026-09-12-profile-consolidation-design.md`)
adds a third call site when it lands; this spec does not wait for it.

---

## 1. What is missing, and what only looks missing

Two different problems get filed under "observability" here, and only one needs
storage.

**Already in the database.** Meetings filed, contacts per meeting, commitments per
meeting, leads opened, digests delivered, active users — every one of these is a
`SELECT` over `meetings`, `meeting_contacts`, `commitments`, `leads` and
`users.last_digest_on`. Counting them into a metrics table would duplicate the
source of truth and drift from it the first time a row is deleted or merged.
These need a *read path*, not a writer.

**Genuinely discarded.** Token usage. `client.messages.create` returns
`response.usage` at `core/llm.py:65` and `jobs/digest.py:107`, and nothing reads
it. There is no record anywhere of what the product costs to run, per day, per
job, or per developer.

The second one is worth capturing for a reason beyond the bill. `_system_blocks`
(`core/llm.py:15`) places the cache breakpoint after the stable prefix, and its
docstring explains the choice: marking the whole prompt put the contact roster —
reordered by essentially every `save_meeting` — inside the cached prefix, so
every reorder was a full miss on the system prompt *and* the tool definitions,
rewritten at 1.25x, "which on a workload where most turns file a meeting costs
more than not caching". That is a specific, load-bearing claim about this
workload, and it is currently unmeasured. `cache_read_input_tokens` versus
`cache_creation_input_tokens` settles it.

### Goals

- Know what the product costs to run, broken down by job and by developer.
- Measure the cache design rather than believing it.
- One command produces a page you can read, from a laptop, with no new service.

### Non-goals

- **A live dashboard endpoint.** Considered and rejected for now: every route
  today is `/health` or HMAC-verified, and an authenticated human-facing page on
  the box holding the client book is a real surface to defend. Caddy `basic_auth`
  in front of a `/dashboard` route is the upgrade path when a weekly file stops
  being enough.
- **Alerting.** Nothing pages anyone. A weekly look is the intended cadence.
- **Request/response logging.** This stores counts, never prompt or completion
  text — see §2.

---

## 2. `llm_calls`

```sql
CREATE TABLE llm_calls (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job          TEXT NOT NULL,          -- 'turn' | 'digest' | 'consolidation'
    user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
    model        TEXT NOT NULL,
    input_tokens                INT NOT NULL,
    output_tokens               INT NOT NULL,
    cache_creation_input_tokens INT NOT NULL DEFAULT 0,
    cache_read_input_tokens     INT NOT NULL DEFAULT 0,
    stop_reason  TEXT,
    duration_ms  INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_calls_day ON llm_calls (created_at DESC);
```

**One row per model call, not per turn.** A single turn makes up to
`MAX_ITERATIONS` (8) calls (`core/llm.py:64`), and the per-iteration breakdown is
where cache behaviour actually shows: the first call in a turn pays for cache
creation, later ones should read. Aggregating to the turn before storage would
throw away the only view that answers the §1 question.

**No visibility column, and no text.** This table holds counts. It is the one
table in the schema that can be read by anyone without exposing a client, which
is also what makes the report safe to open on a phone or send to someone.

`user_id` is nullable because consolidation runs for no user, and
`ON DELETE SET NULL` rather than the `RESTRICT` the domain tables use: telemetry
should outlive a roster change, and it carries nothing worth protecting.

---

## 3. Recording

`gaia/core/usage.py`:

```python
async def record(pool, *, job: str, user: User | None, model: str,
                 usage, stop_reason: str | None, duration_ms: int) -> None
```

Opens its own short transaction and swallows every exception. **Telemetry must
never cost a reply** — the same rule the typing indicator follows in
`butler.py`, for the same reason: a metrics insert failing during a turn must
degrade to a missing row, not to an apology.

Three call sites:

| Job | Site | User |
|---|---|---|
| `turn` | `core/llm.py:65`, inside the loop, once per iteration | the turn's user |
| `digest` | `jobs/digest.py:107` | the recipient |
| `consolidation` | the job in the consolidation spec | `NULL` |

Awaited rather than fired-and-forgotten. It is a single small insert against a
pooled connection, next to a model call that just took seconds; making it a
background task buys nothing and makes tests non-deterministic.

---

## 4. Cost is computed when read, never stored

Rates live in one dict in the reporting module, keyed by model:

```python
# USD per million tokens. Anthropic first-party rates.
PRICES = {
    "claude-opus-5":   {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
}
CACHE_WRITE_MULTIPLIER = 1.25   # cache_creation_input_tokens
CACHE_READ_MULTIPLIER  = 0.10   # cache_read_input_tokens
```

Cost for a row is

```
(input * in + cache_creation * in * 1.25 + cache_read * in * 0.10 + output * out) / 1e6
```

Prices change. Storing a computed cost means either rewriting history when they
do, or reporting numbers that quietly stop being true; storing tokens and pricing
at read time means an old row reprices correctly. An unknown model id reports
tokens with no cost rather than guessing, and says so in the output.

---

## 5. The report

`python -m gaia.core.admin stats [--days 30] [--html]`, a fifth subcommand
alongside `add-user`, `list-users`, `deactivate` and `merge-contacts`
(`core/admin.py:33`). Text by default; `--html` writes a self-contained page to
stdout.

```
ssh gaia 'cd /opt/gaia-assistant && docker compose exec -T app \
    python -m gaia.core.admin stats --html' > report.html
```

The file lands on the laptop through the existing ssh session. No scp step, no
new port, nothing listening.

### 5.1 What it shows

**Product** (derived, no new storage):

- meetings filed per day, and per developer
- contacts per meeting, mean and max
- commitments per meeting, and the share with a due date — this is the number
  that tells you whether the date guard (`4acefe8`) over-corrected
- leads opened, and how many have a `next_action_at`
- digests delivered, versus users due one
- photo versus text meetings (`meetings.source`)

**Model** (from `llm_calls`):

- tokens and cost by day, by job, and by developer
- calls per turn — the distribution, since 8 is the cap and a turn hitting it
  returns `FALLBACK_TEXT`
- **cache hit rate**: `cache_read / (cache_read + cache_creation + input)`
- stop reasons, so `max_tokens` and `refusal` stop being invisible

### 5.2 Per-developer cost

Included, as agreed. Worth stating the tradeoff where it will be read: at two
people this is curiosity, and beyond a handful it is a number that can quietly
become a performance metric. A developer who files careful photo notes every day
costs several times one who texts occasionally, and that is the product working,
not waste. If it ever reads that way, the fix is to drop the per-user column from
the report — the data stays, the framing goes.

### 5.3 HTML

One file, inline CSS, inline SVG for the daily bars, no CDN and no JavaScript.
It has to open from a `file://` URL on a laptop and a phone.

**Aggregates only. No contact names, no meeting text, ever.** The report is the
one artefact of this system designed to be screenshotted and shared; the moment
it lists Tyler Flanzer it becomes another copy of the client book with worse
access control than the database it came from.

---

## 6. Testing

- One model call writes exactly one row, with the right `job` and `user_id`.
- A turn that makes three iterations writes three rows.
- `record` swallowing a database failure leaves the turn's reply unchanged —
  the same test shape as `test_a_failing_typing_indicator_does_not_cost_the_reply`.
- Cost maths against a fixed row: a known token mix produces a known dollar
  figure, including the cache multipliers.
- An unknown model reports tokens and no cost, without raising.
- Aggregation over seeded rows: by day, by job, by user.
- `--html` output contains the computed figures and no `<script>` tag.

---

## 7. Open questions

- **Retention.** Unbounded for now. At a few hundred rows a day these are
  kilobytes; revisit only if the nightly dump notices.
- **Live capture of cache diagnostics.** The API has a beta cache-diagnosis mode
  that explains *why* a prefix missed. Out of scope here — this spec measures
  whether caching works, not why it doesn't.
- **A `/dashboard` route** behind Caddy `basic_auth` is the obvious next step if
  the file-per-week rhythm annoys. Deliberately deferred, §1 non-goals.
