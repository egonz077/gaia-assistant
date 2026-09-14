# Voice notes as input — research

**Status:** research only. No design, no spec, no decision taken.
**Question:** can a developer send a voice note instead of typing or
photographing their meeting notes, and have it filed the same way?

---

## 1. The finding that shapes everything

**Claude cannot accept audio as input.** Verified against the live Files API
documentation (2026-09-13): the Messages API accepts exactly three input
content block types —

| Content block | MIME types |
|---|---|
| `document` | `application/pdf`, `text/plain` |
| `image` | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `container_upload` | datasets, for the code execution tool |

Audio appears in Anthropic's documentation only as something Claude
*produces* via the code execution tool (those outputs carry C2PA Content
Credentials). There is no audio input path.

**Consequence:** a voice note needs a separate speech-to-text step before
Claude ever sees it. This is not an implementation detail — it changes the
shape of the feature, and §4 explains why.

## 2. What WhatsApp actually delivers

An inbound voice note arrives as `type: "audio"` with this object
([webhook reference](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/audio)):

```json
"audio": {
  "mime_type": "audio/ogg; codecs=opus",
  "sha256": "wvqXMe6n7n1W0zphvLPoLj+s/NtKqmr3zZ7YzTP7xFI=",
  "id": "1908647269898587",
  "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=133...",
  "voice": true
}
```

Notes:

- **`voice: true`** distinguishes a recording made with WhatsApp's own
  microphone button from an uploaded audio file. Both arrive as `type:
  "audio"`. Worth branching on — a forwarded MP3 is a different intent from a
  20-second note dictated in a car.
- **Format is fixed: OGG/Opus.** Every STT vendor below accepts it; no
  transcoding needed, so no `ffmpeg` in the image.
- **Limits:** 100 MB per media file (Cloud API), webhook payloads up to 3 MB.
  A realistic voice note is tens of kilobytes. Not a constraint.
- **`url` is rolling out** (since Nov 2025) alongside the existing two-step
  fetch. `download_media` already does the two-step (`GET /{media_id}` →
  `GET` the returned URL), so no change is needed there.

### What happens today

`parse_messages` (`gaia/core/whatsapp.py:30`) has branches for `text` and
`image`, and everything else falls through to:

```python
base["type"] = "text"
base["text"] = f"[unsupported message type: {m['type']}]"
```

So a voice note today reaches the model as the literal string
`[unsupported message type: audio]`. It is not dropped, and the user gets a
reply — just a useless one. That is a decent failure mode to be starting from.

**One trap:** `download_media` (`whatsapp.py:145`) calls `downscale()`
unconditionally on whatever bytes come back. That is Pillow, and it is
image-specific. Reusing `download_media` for audio without branching on the
media type will fail inside Pillow, not at a nice boundary.

## 3. Speech-to-text options

No Anthropic-native path exists, so this is a new vendor decision.

Prices below are **approximate and gathered from secondary comparison sources,
not vendor pricing pages.** Verify against the vendor before committing —
published rates move, and several of these have per-feature add-ons that can
multiply the effective rate.

| Option | ~Price/min | Notes |
|---|---|---|
| **Deepgram Nova-3** | ~$0.0043 batch | Strongest reported accuracy on noisy/accented/phone audio (~9.4% WER on telephony vs Whisper's ~12.8%). Supports keyterm prompting — see §4. |
| **AssemblyAI Universal-2** | ~$0.0025 | Cheapest of the managed tier; 99 languages. |
| **OpenAI `gpt-4o-transcribe`** | ~$0.006 | Adds a second AI vendor to a stack that is deliberately Anthropic-only. |
| **Groq (Whisper turbo)** | ~$0.0003 | Cheapest by an order of magnitude. Whisper-family accuracy. |
| **Self-hosted faster-whisper** | $0 marginal | **Not viable on the current droplet** — see below. |

### Self-hosting is out, for now

The droplet is 1 vCPU / 2 GB RAM, already running Postgres, two Python
containers and Caddy, with a 2 GB swap file that exists because there is no
headroom. A Whisper model resident in memory plus CPU-bound inference on a
single shared core would contend directly with the webhook path — which has a
hard latency budget, because WhatsApp retries a webhook that does not return
200 quickly. Self-hosting means resizing the droplet first. It is a real option
later; it is not a free one.

### Volume, for scale

At present usage — 2 developers, a handful of messages a day — every option
above is under a dollar a month. **Cost is not the deciding factor here.**
Accuracy on this specific audio, and the privacy question in §5, are.

## 4. The asymmetry nobody should miss

This is the part that matters most, and it is not obvious.

**Photographed notes and voice notes are not the same feature.**

With a photo, Claude sees the *raw image*. It transcribes and interprets in one
pass, with the contact roster in its system prompt — so when the handwriting
says something like "Flanzer", the model has "Tyler Flanzer (Two Roads)" in
front of it and can resolve the name. Transcription and understanding happen
together, with context.

With a voice note, transcription happens **before** Claude, in a vendor that
knows nothing about this company. Claude only ever sees the vendor's output.
Every name that STT mangles is mangled irrecoverably — and this client book is
full of exactly the names STT is worst at:

> Cesia · Maurice · Okonkwo · Flanzer · Marta Delgado · Empira · Aria ·
> Two Roads · Fort Partners

Production data already shows Spanish and English mixed in one company's notes,
which is the hardest case for any recognizer.

**The mitigation is the same idea the system already uses for the roster.**
Most vendors accept a vocabulary hint — Deepgram calls it keyterm prompting,
Whisper-family APIs take a `prompt` parameter. `contacts_db.roster()` already
exists and already feeds the system prompt. Passing that same roster into the
transcription call is cheap and is likely the difference between "Call Cesia"
and "Call Sasha".

**A second mitigation worth considering:** show the user the transcript and
let them correct it before anything is filed. The assistant already echoes
back what it understood and asks about ambiguous names — that habit extends
naturally, and it is the only recovery path for a name the vendor got wrong.

## 5. The privacy question

This deserves a decision, not a default.

The codebase is unusually careful about who can read what — `visibility`
versus `user_id`, derived rows inheriting their parent's visibility, private
meetings kept out of the shared contact profile. All of that protects data
*inside* the system.

Adding an STT vendor means **client confidences leave the system entirely**.
A note that says "they are divorcing and will take 540 if it moves quickly" —
verbatim, in the seller's own voice — would be sent to a third-party
processor. That is a materially different exposure from text the user typed,
and it is the kind of thing worth deciding deliberately rather than acquiring
as a side effect of a convenience feature.

Questions that need answers before an implementation is designed:

- Is a third processor acceptable at all for this content?
- If yes: does the vendor's data-retention and training policy matter enough
  to narrow the shortlist? (Several offer zero-retention or no-training
  tiers — that likely matters more here than price.)
- Does a voice note marked `private` by the user need different handling from
  an org-visible one — or is the exposure identical the moment the audio
  leaves the box, making the distinction meaningless?

## 6. What would change in the code

Rough shape only — no design decisions taken.

| File | Change |
|---|---|
| `core/whatsapp.py` | `parse_messages`: an `audio` branch carrying `audio_id`, `mime_type`, `voice`. `download_media`: branch on media type instead of always calling `downscale()`. |
| `core/transcription.py` *(new)* | The vendor call. One module, one responsibility, so the vendor is swappable — matching how `core/embeddings.py` isolates Voyage. |
| `butler.py` | `_build_blocks`: an audio branch that transcribes and appends the text. `transcript_text`: how a voice note reads in history. The existing `PHOTO_FAILED_NOTE` pattern is the precedent for a failed transcription. |
| `core/config.py` | Vendor API key; possibly a `TRANSCRIPTION_MODEL` setting. |
| `docs/RUNBOOK.md` | New credential, its purpose and blast radius. |

Two existing behaviours it must respect:

- **A failed transcription must not lose the whole burst.** `_build_blocks`
  already handles one unreadable photo in a multi-message batch by turning it
  into a visible note rather than rolling back the batch. Audio needs the
  same.
- **Telemetry.** `llm_calls` records Anthropic calls. Transcription is a
  *different* vendor with a *different* unit (audio-seconds, not tokens), so
  it does not fit that table as it stands. Either it gets its own column
  meaning, its own table, or it stays unmeasured — worth deciding rather than
  discovering later.

## 7. What this research did not settle

Deliberately left open, because they are product decisions:

- **Vendor choice**, which follows from the privacy answer more than price.
- **Whether the transcript is shown for confirmation** before filing.
- **Whether `voice: false` uploads are handled at all**, or only real voice
  notes.
- **What happens to the audio after transcription** — the photo path does not
  store the image and keeps only `raw_input`; the same rule probably applies,
  but "probably" is not a decision.
- **Language handling** — force Spanish/English, or auto-detect per note.

---

## Sources

- [Files API — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/files) — input content block types
- [Audio messages webhook reference — Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/audio) — inbound payload shape
- [Audio messages — Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/audio-messages) — OGG/Opus requirement
- [Media — Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media) — size limits
- [Best Speech-to-Text APIs in 2026 — Deepgram](https://deepgram.com/learn/best-speech-to-text-apis-2026) — comparison (vendor-published, read accordingly)
- [Speech-to-Text APIs in 2026: Benchmarks, Pricing — Future AGI](https://futureagi.com/blog/speech-to-text-apis-in-2026-benchmarks-pricing-developer-s-decision-guide/) — pricing aggregate
