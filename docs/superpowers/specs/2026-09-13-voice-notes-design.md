# Voice notes — design

**Status:** designed, not implemented. Implementation plan not written yet.
**Research:** `docs/superpowers/research/2026-09-13-voice-notes.md`
**Depends on:** the observability work (`llm_calls`, shipped 2026-09-13) — §7
renames and extends it.

**Use case.** A developer walks out of a meeting and dictates their notes as
WhatsApp voice messages, often several in a row, while walking to the car.
Those notes must be filed exactly as typed or photographed notes are: a
summary, the people involved, commitments, follow-up dates.

---

## 1. The constraint everything follows from

**Claude accepts no audio input.** Verified against the live Files API
documentation: the Messages API takes `document` (PDF, `text/plain`), `image`
(jpeg/png/gif/webp) and `container_upload`. Nothing else. Audio appears in
Anthropic's documentation only as something Claude *produces* via code
execution.

So transcription happens **before** Claude, in a separate service, and Claude
only ever sees text. That is not a detail — it creates an asymmetry with the
photo path that §5 exists to mitigate.

## 2. Vendor: Deepgram Nova-3

A dedicated ASR model, not a multimodal LLM. `gpt-4o-transcribe` and Gemini
would also work and are roughly twice the price; they also add a second AI
vendor to a stack that is deliberately Anthropic-only.

### Why not self-host

Whisper is open-weight and genuinely self-hostable, and it is the only option
where audio never leaves the droplet. It was rejected on economics, not
principle:

| | Vendor API | Self-hosted Whisper |
|---|---|---|
| Cost at 10 developers | ~$2.58/mo | droplet resize, ~+$12/mo |
| Name accuracy | keyterm prompting, 100 terms | smaller, less targeted `prompt` field |
| Ops | an API key | a model file, RAM headroom, a new failure mode |

The current droplet is 1 vCPU / 2 GB running five containers with a swap file
precisely because there is no headroom. **Self-hosting costs more than the API
until STT spend passes the resize cost — about 2,800 minutes a month, or ~47
two-minute notes a day.** That is 47 people at one note a day, or ten people at
five. Revisit at that point, not before.

### Cost model, 10 developers × 1 note/day × 30 days

| Component | Basis | Monthly |
|---|---|---|
| Butler turns (Opus 5) | 300 × ~$0.04 | ~$12.00 |
| Transcription (Nova-3) | 600 min × $0.0043 | ~$2.58 |
| Digests (Sonnet 5) | 300 × ~$0.005 | ~$1.50 |
| Embeddings (Voyage) | ~300 chunks | cents |

**Transcription is ~16% of AI spend; the butler turn is ~75%.** Vendor choice
here is an accuracy and privacy decision, not a cost one. The per-turn figure
is modelled from measured token counts, not observed; §7 is what will replace
it with the real number.

## 3. Privacy posture

Voice notes send client confidences to a third processor in the seller's own
voice — a materially different exposure from text the user typed. The decision
taken is a contracted zero-retention vendor rather than no vendor at all.

**Every request sets `mip_opt_out=true`.** Deepgram's Model Improvement
Partnership Program is documented as opt-in, and the API also exposes a
per-request opt-out. Rather than depend on which default applies to a given
plan, the parameter is set explicitly on every call. This is not optional and
is not a configuration toggle — a request without it is a bug.

Audio is **never stored**. This mirrors the photo path, which does not keep the
image: the transcript is the durable record, and it reaches `meetings.raw_input`
by the same route a photo's transcription does.

## 4. Ingestion

Transcription happens inside the turn, in `_build_blocks`, exactly where a
photo is downloaded and processed today.

```
webhook ──200── queue(3s debounce) ── handle_turn ── _build_blocks
                                                          │
                                            audio ────────┤
                                                          ├─ fetch bytes
                                                          ├─ transcribe(roster)
                                                          └─ text block
                                                                 │
                                                            run_agent → save_meeting
```

Two alternatives were rejected:

- **Transcribe at the webhook.** The webhook's job is to return 200 fast. A
  redelivery inside Meta's retry window already produced duplicate turns once
  (`butler.py`, `receive`); adding a vendor call before the 200 makes that
  worse.
- **Transcription as a tool Claude calls.** The model cannot hear the audio, so
  it cannot decide whether to call the tool. It would always call it, for an
  extra round trip.

### `gaia/core/transcription.py` (new)

One module, one vendor, mirroring how `core/embeddings.py` isolates Voyage —
so the vendor can be swapped, including for local Whisper later, without the
pipeline knowing.

```python
async def transcribe(
    audio: bytes, mime_type: str, keyterms: list[str]
) -> tuple[str, float]:
    """Returns (transcript, audio_seconds). Raises on vendor failure."""
```

Request parameters, all fixed rather than configurable:

| Parameter | Value | Why |
|---|---|---|
| `model` | `nova-3` | keyterm prompting requires Nova-3 or Flux |
| `language` | `en` | notes are dictated in English in practice, with Spanish names; forcing it beats per-file detection |
| `mip_opt_out` | `true` | §3 |
| `keyterm` | repeated, from the roster | §5 |

`audio_seconds` comes back from the vendor response and is what §7 prices.

### `gaia/core/whatsapp.py`

`parse_messages` gains an `audio` branch carrying `audio_id`, `mime_type` and
the `voice` boolean. Uploaded audio files (`voice: false`) are handled
identically — the distinction is recorded but not acted on, because a forwarded
recording of a meeting is the same content by a different route.

**A targeted split.** `download_media` currently fetches *and* downscales in
one function, and `downscale` is Pillow. Reusing it for audio fails inside
Pillow rather than at a boundary. Separate them:

- `_fetch_media(media_id) -> (content_type, bytes)` — private, the two-step
  Graph fetch, unchanged behaviour
- `download_media(media_id) -> dict` — image path, downscaled and base64, as
  today
- `download_audio(media_id) -> tuple[bytes, str]` — raw bytes and MIME type

### `gaia/butler.py`

`_build_blocks` gains an audio branch. It already holds a `conn`, so it can
read `contacts_db.roster(conn, user, limit=100)` for the keyterms (§5).

The transcript enters the conversation **labelled**, not as bare text:

```
[voice note] Met Marta Delgado at the Coral Gables listing...
```

This mirrors the existing `[photo] <caption>` convention and buys two things:
the model knows names may be misheard and can check them against the roster it
already has, and the message log reads honestly instead of implying the user
typed it. `transcript_text` renders it the same way in history.

### `meetings.source`

Gains a third value, `voice_note`, beside `text` and `photo_notes`. Without it
a dictated meeting is indistinguishable from a typed one in the record, and
§7's photo/text split silently loses them.

## 5. Accuracy: the roster is the whole trick

General English transcription is solved. **Rare proper nouns are not**, and
this client book is almost entirely rare proper nouns — Cesia, Maurice,
Okonkwo, Flanzer, Marta Delgado, Empira, Two Roads, Fort Partners. No ASR model
has seen them, and every one will confidently emit a common word that sounds
close.

With a photo, Claude sees the raw image *and* the roster, and resolves the name
itself. With audio it cannot: it sees only what the vendor produced, so a
mangled name is mangled irrecoverably.

**Mitigation 1 — keyterm prompting.** `contacts_db.roster(conn, user, limit)`
already returns the contact names this user has worked with, most recently
touched first, and already feeds the system prompt. The same list goes to
Deepgram as repeated `keyterm=` parameters.

Documented limits: Nova-3, up to 100 terms, 500 tokens total across all
keyterms, proper nouns explicitly supported. `roster` defaults to `limit=40`
for the system prompt; transcription calls it with **`limit=100`** to use the
keyterm budget — at roughly two to three tokens per name, 100 names sits inside
the 500-token cap. The limit parameter is the cap; no separate truncation is
needed.

**This also bounds what leaves the box.** `roster` is scoped by ownership *and*
visibility — deliberately, and for a reason documented in its own docstring: a
purely visibility-scoped version once put the whole company's contacts into one
developer's system prompt. That scoping carries over here unchanged, so the
keyterm list sent to the vendor contains only the names this developer
legitimately works with, never the company's whole contact book.

**Mitigation 2 — the echo-back that already exists.** `BASE_PROMPT` tells the
assistant to echo back what it understood and ask about anything ambiguous:
names, numbers, dates. That habit is the correction point, and it is why no
separate transcript-confirmation step is specified. Someone walking to their
car gets one reply to skim, not a transcript to approve.

`BASE_PROMPT` gains one sentence: a voice note's text is a machine
transcription and may mishear names, so check them against the roster and ask
about any that do not match.

## 6. Failure handling

A failed transcription must never cost the burst. `_build_blocks` already
handles one unreadable photo among several messages by turning it into a
visible note rather than rolling back the batch; audio follows exactly that
pattern with its own constant:

```python
VOICE_FAILED_NOTE = "[a voice note in this message could not be transcribed]"
```

The note is written both into the content blocks the model sees — so its reply
can tell the user which part did not land — and amended into the row `receive`
already wrote, so the history matches what happened.

An empty transcript (the vendor succeeded but heard nothing — silence, a
pocket recording) is treated as a failure and takes the same path. A meeting
filed from an empty transcript is worse than one visibly not filed.

## 7. Observability

The user asked for transcription cost and metrics to be captured. It does not
fit `llm_calls` as shipped: every column there assumes token billing, and
transcription bills in audio-seconds.

**Migration `004`:**

1. `ALTER TABLE llm_calls RENAME TO model_calls`. Nova-3 is not an LLM, so the
   name becomes a misnomer the moment transcription rows land in it. The table
   is a day old and the rename preserves every row; doing it now costs one line
   and doing it later costs a deprecation.
2. `ADD COLUMN audio_seconds NUMERIC(10,2)` — NULL for token-billed calls.

**Pricing.** `PRICES` keeps its per-MTok entries; a second dict holds
per-minute audio rates (`nova-3`: $0.0043). `row_cost` branches on whether
`audio_seconds` is set. The existing rule is unchanged: priced on read, and an
unpriced vendor reports its duration with no cost rather than guessing.

**`turn_id` is NULL for transcription rows**, as it is for digests.
Transcription runs in `_build_blocks` before `run_agent` exists, and
`calls_per_turn` means *agent-loop iterations* — the number exists to show
turns reaching `MAX_ITERATIONS`, and counting a transcription as one would
corrupt it.

**Failures are recorded too**, with `stop_reason='error'` and no cost. A
transcription failure rate is a product signal; without a row it is invisible
until users complain.

**The report gains:** transcription in `by_job` and `by_user` automatically,
plus a line for audio minutes transcribed, and a third bucket in the meetings
source split so voice notes do not vanish from it.

## 8. Testing

- `parse_messages` turns an audio webhook payload into an audio message dict,
  carrying `voice` both true and false.
- `download_audio` returns raw bytes and does not go through `downscale`; the
  image path still does.
- A fake transcription client, beside `FakeAnthropic`, so the hermetic tier can
  drive the audio branch without a vendor.
- One voice note in a burst becomes a labelled text block; the roster reaches
  the transcribe call, capped at 100 terms.
- A failed transcription leaves the rest of the burst intact and writes
  `VOICE_FAILED_NOTE` into both the blocks and the logged row.
- An empty transcript takes the failure path.
- A transcription writes one `model_calls` row with `audio_seconds` set,
  `turn_id` NULL, and a cost derived from the per-minute rate.
- A failed transcription writes a row with `stop_reason='error'` and no cost.
- **Live tier:** one real short recording through the real vendor, asserting a
  non-empty transcript and that a keyterm name survives. Vendor schema
  correctness is exactly what a fake cannot prove.

## 9. Out of scope

- **Replying with voice.** Input only.
- **Self-hosted Whisper.** Revisit at ~47 notes/day (§2).
- **Diarisation.** These are one person dictating, not a recorded meeting.
- **Storing the audio.** §3.
- **A duration cap.** No evidence one is needed; a 10-minute note still costs
  under $0.05 to transcribe.
- **Language auto-detection.** English is forced (§4). Revisit if a developer
  starts dictating in Spanish — the vendor supports it, the design does not
  need to anticipate it.
