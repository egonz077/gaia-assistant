# Profile consolidation — Design

**Status:** designed, not implemented. Implementation plan not written yet.
**Depends on:** nothing. Lands on increment 1 as deployed.

---

## 1. The problem

`contacts.profile` is one append-only string per person. `merge_profile`
(`core/db/contacts.py:141`) concatenates with `' | '` and, past `PROFILE_CAP`
(4000 characters), trims from the *front* with `right()`. Nothing records which
meeting contributed which sentence.

One day of real use produced this:

```
Tyler Flanzer | At Two Roads. | Head of acquisitions at Two Roads; focused on
                                ground-up multifamily in Broward County.
Diego Ojeda   | At Rilea.     | Land acquisition at Related (previously Rilea).
Andrea        | With Aria | Aria
```

Three distinct failures:

1. **Stale facts are never retired.** Diego's profile opens with `At Rilea.`,
   which is false. `lookup_contact` hands the model the whole string; today it
   usually resolves the contradiction from word order, because appends happen in
   chronological order. That reasoning breaks the moment `merge()`
   (`core/db/contacts.py:74`) folds two contacts — it concatenates
   `dst || ' | ' || src`, interleaving two chronologies into one line where
   position no longer means time. Its docstring already admits this is lossy.
2. **Paraphrase duplication.** The model cannot see the existing profile when it
   calls `save_meeting` — the current value is not passed in — so it restates
   what is already there. `Rilea | Rilea` is the exact-match case; `At Two Roads.`
   beside `Head of acquisitions at Two Roads` is the general one.
3. **Eviction hits the wrong end.** Durable facts are learned early, so they sit
   at the front and are the first thing `right()` amputates. Episodic noise is
   newest and survives.

Commit `c0d9990` narrowed what gets written — profiles now take durable identity
facts rather than events. It did not change how those facts are *combined*, which
is what this spec addresses.

### Goals

- A profile states who someone is now, in prose, with superseded facts either
  retired or explicitly marked as prior.
- The combining step is reversible and auditable: a bad rewrite is recoverable
  and visible after the fact.
- No private meeting content can reach an org-readable profile.

### Non-goals

- **Feeding the digest.** The digest reads leads and commitments, never profiles.
  Cross-cutting observations from the nightly pass ("this lead has no next
  action") are the obvious follow-on and are deliberately out of scope here.
- **Contact deduplication.** Suggesting `merge-contacts` candidates is a natural
  second job in the same runner. Not now.
- **`delete_meeting`.** Discussed alongside this and separately designed. It
  becomes much simpler once §3.1 exists.
- **Private contacts.** Rows with `visibility = 'private'` are skipped in v1.
  Their profile is owner-only and their meetings are typically private too, so
  there is nothing org-visible to consolidate from.

---

## 2. Shape

```
INGEST              synchronous, the user is waiting
  webhook -> debounce -> agent turn -> save_meeting
  writes meetings, meeting_contacts(+note), commitments, leads, memory_chunks
                          |
DREAMING            nightly, org-wide, nobody waiting
  reads dated per-contact notes from org-visible meetings
  writes contacts.profile, profile_versions, contacts.consolidated_at
                          |
DIGEST              08:00 local, per user  (unchanged)
```

The digest is per-user and timezone-sensitive because it is a message to a
person. Consolidation is **org-wide and runs once**, because its unit is the
contact, and contacts are shared across Gaia — Tyler Flanzer has one profile, not
one per developer. A single nightly pass at 03:00 in the container's timezone
(`TZ: America/New_York`, already set on the `jobs` service) leaves five hours of
headroom before the earliest digest.

---

## 3. Data model

### 3.1 `meeting_contacts.note` — keep the fact that is already generated

`save_meeting` receives `profile_update` per contact per meeting, merges it into
the blob, and discards it as a discrete item. Keep it:

```sql
ALTER TABLE meeting_contacts ADD COLUMN note TEXT;
```

This is the change that makes everything else work. Consolidation input for Diego
becomes two dated lines rather than two full meeting summaries:

```
2026-09-12  Rilea
2026-09-19  land acquisition at Related, previously Rilea
```

Summaries are the wrong input: today's two stored meetings carry 15 and 17
contacts each, so consolidating one person from a summary means feeding the model
several hundred characters about fourteen other people and asking it to pick out
theirs. The notes in that same page sit adjacent — `Rayclif. The Estate. Seth
Goldman. DaGrossa Capital Development` — and the model has already shuffled which
belongs to whom between replay runs.

Consequences beyond consolidation, none of which need extra work:

- Deleting a meeting removes its contribution automatically: `meeting_contacts`
  is already `ON DELETE CASCADE`, and the next pass regenerates the profile
  without it.
- `merge()` stops being lossy for profiles. Re-pointing rows preserves each
  note's own date, and the next pass rebuilds one coherent profile.

**Only org-visible meetings get notes**, mirroring `save_meeting` today, which
drops `profile_update` entirely when `private` is true and keeps it only inside
`raw_input` via `_private_profile_notes`. Storing private per-contact notes would
create a surface the consolidation query must then be trusted to exclude forever.
Cheaper not to have it.

### 3.2 `profile_versions` — an audit trail, not an undo column

```sql
CREATE TABLE profile_versions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    profile    TEXT NOT NULL,
    source     TEXT NOT NULL,   -- 'pre-consolidation' | 'consolidation'
    note_count INT  NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_profile_versions_contact ON profile_versions (contact_id, created_at DESC);
```

Each pass inserts the value it wrote. The first pass over a contact also inserts
the pre-existing string as `pre-consolidation`, so the timeline is complete from
the day this ships.

A single `previous_profile` column would make the last rewrite reversible and
nothing else. The interesting failure — a good fact quietly dropped three passes
ago — is only findable with history. It also gives the prompt an evaluation
target: diff consecutive passes and see whether the job converges or churns.

### 3.3 `contacts.consolidated_at`

```sql
ALTER TABLE contacts ADD COLUMN consolidated_at TIMESTAMPTZ;
```

Selection is `consolidated_at IS NULL OR updated_at > consolidated_at`.

**The consolidation write must not touch `updated_at`.** Two reasons, both
load-bearing: bumping it would leave every contact permanently dirty, so the job
would re-consolidate the whole book nightly and never converge; and `roster()`
(`core/db/contacts.py:28`) orders by `updated_at DESC` to tell each user who they
have worked with recently, so a background rewrite would reshuffle that list and
put words in the system prompt that are not true.

---

## 4. The job

`gaia/jobs/consolidate.py`, alongside `digest.py`, sharing its structure.

```python
async def due_contacts(conn, limit: int = 200) -> list[Row]: ...
async def consolidate_one(conn, client, contact) -> bool: ...
async def run_once(pool, client) -> int: ...
```

### 4.1 Selection

```sql
SELECT c.id, c.name, c.profile
  FROM contacts c
 WHERE c.visibility = 'org'
   AND (c.consolidated_at IS NULL OR c.updated_at > c.consolidated_at)
 ORDER BY c.updated_at DESC
 LIMIT %(limit)s
```

`LIMIT` bounds a first run over an existing book, and bounds the blast radius of
a prompt regression: at most `limit` profiles can be damaged in one night, and
every prior value is in `profile_versions`.

### 4.2 Input for one contact

```sql
SELECT m.happened_at, mc.note
  FROM meeting_contacts mc
  JOIN meetings m ON m.id = mc.meeting_id
 WHERE mc.contact_id = %(contact_id)s
   AND mc.note IS NOT NULL
   AND m.visibility = 'org'
 ORDER BY m.happened_at
```

**This query must not use `visible()`.** That helper means "org-visible *or*
owned by `scope_user_id`", which is correct for a user-scoped read and wrong
here: the job runs as no user, and `contacts.profile` is readable by the whole
company. Passing a user id into this path is how a private note gets laundered
into the company record. The predicate is literally `m.visibility = 'org'`.

Contacts whose notes predate §3.1 have none. Those fall back to consolidating the
existing profile string alone — weaker, since recency must be inferred from word
order, but it still resolves `Rilea | Rilea` and retires `At Rilea.` where the
correction is present in the text. This degradation is one-time and shrinks daily.

### 4.3 The rewrite

System prompt, in the shape of `digest.py:26`:

> Write a contact profile for a real-estate development company. Given dated
> facts about one person, state who they are and why Gaia Group would meet them:
> organization, role, what they build or buy, what they are looking for, how they
> are connected to the company. Later facts supersede earlier ones — state the
> current position, and keep a prior affiliation only where it explains the
> relationship ("previously at Rilea"). No events, dates or arrangements; those
> live on the meeting. One to three sentences. If nothing durable is known,
> return an empty string.

`output_config={"effort": "low"}`, as the digest uses. Output is capped at 600
characters — well under `PROFILE_CAP`, which becomes vestigial for consolidated
profiles.

### 4.4 Failure rules

Borrowed intact from the digest, where each was learned the hard way:

- **One contact's failure must not cost the rest of the pass** — per-contact
  `try/except` around its own transaction (`digest.py:164`).
- **Stamp only after the write lands.** `consolidated_at` is set in the same
  transaction as the profile write, never before (`digest.py:150`).
- **Never overwrite a non-empty profile with an empty result.** An empty
  completion is a model failure, not a finding. Log, leave the profile, and stamp
  so it is not retried until the contact changes again.
- **A concurrent `merge_profile` from a live turn is allowed to win.** Both run in
  transactions; last writer wins, the contact is left dirty, and the next pass
  reconciles. Locking a contact for the duration of a model call would block a
  user turn behind a background job.

### 4.5 Running it

The `jobs` service runs `python -m gaia.jobs.digest` as its command. Rather than
add a second container to a 2GB droplet, introduce `gaia/jobs/runner.py` running
both ticks in one process under `asyncio.gather`, and point compose at it. The
digest keeps its own `main()` so it stays runnable alone.

---

## 5. Testing

Unit, against the existing per-process Postgres:

- A private meeting's note never reaches a profile. Put a distinctive string in a
  private meeting, run a pass, assert it appears in no `contacts.profile` row.
  This is the test that justifies §4.2 and it should be written first.
- Selection: a contact untouched since its last pass is not returned; one touched
  after is; a `visibility = 'private'` contact never is.
- A pass does not modify `updated_at` (§3.3).
- `consolidated_at` is unchanged when the write raises.
- An empty completion leaves the existing profile intact.
- The first pass writes both a `pre-consolidation` and a `consolidation` row.
- Fallback: a contact with no notes consolidates from its profile string.

Live tier (`pytest -m live`), one real call: given the Diego fixture — `Rilea`
then `land acquisition at Related, previously Rilea` — the result mentions
Related and does not assert he is currently at Rilea.

---

## 6. Cost and scale

One short call per touched contact per night. Today's two meetings produced 25
contact rows between them; a busy day might touch 40. At ~500 input and ~150 output tokens each,
a nightly pass is cents. The `LIMIT` in §4.1 caps a backfill.

---

## 7. Open questions

- **Private contacts** are skipped (§1, non-goals). If developers start keeping
  private contact rows with meaningful profiles, they need an owner-scoped
  variant of the same job.
- **Re-consolidation cadence.** A contact that never changes is never revisited,
  so an improved prompt does not reach old profiles. A `--all` flag for a
  deliberate backfill is probably enough; a periodic full sweep is not.
- **`profile_versions` retention.** Unbounded by design. A contact in weekly
  motion accrues ~50 rows a year, each a few hundred bytes. Revisit if the
  backup dump size ever makes it interesting.
