# Voice Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A developer can dictate meeting notes as WhatsApp voice messages and have them filed exactly as typed or photographed notes are.

**Architecture:** Claude accepts no audio input, so transcription happens before the model, inside the turn. `_build_blocks` gains an audio branch: fetch the bytes, transcribe them through Deepgram Nova-3 with the contact roster injected as keyterms, and append a labelled text block. Everything downstream is unchanged, because by the time Claude sees it, it is text. The vendor lives behind one module so it can be swapped — including for local Whisper later — without the pipeline knowing.

**Tech Stack:** Python 3.11, httpx (already a dependency), Deepgram Nova-3 REST API, psycopg 3, pytest. No new packages — the vendor is called over plain HTTP, the way `core/whatsapp.py` calls Graph.

**Spec:** `docs/superpowers/specs/2026-09-13-voice-notes-design.md`

## Global Constraints

- **`mip_opt_out=true` on every transcription request.** Not a setting, not a default to rely on — a request without it is a bug (spec §3).
- **`language=en`** is forced. Notes are dictated in English with Spanish names; the roster carries the names (spec §4, §5).
- **`model=nova-3`.** Keyterm prompting requires Nova-3 or Flux.
- **Keyterms come from `contacts_db.roster(conn, user, limit=100)`** — already ownership- and visibility-scoped, so only names this developer legitimately works with leave the box. Documented vendor limit: 100 terms, 500 tokens total.
- **Audio is never stored.** The transcript is the durable record (spec §3).
- **Telemetry must never cost a reply** — the existing rule from `usage.record`.
- **A failed transcription must never lose the burst** (spec §6).
- **No secret values in any committed file.** `DEEPGRAM_API_KEY` is named in `.env.example` and `docs/RUNBOOK.md`; its value lives only in `.env`.

---

### Task 1: Rename `llm_calls` → `model_calls`, add `audio_seconds`

**Files:**
- Create: `migrations/004_model_calls.sql`
- Modify: `gaia/core/usage.py`, `gaia/core/stats.py`
- Test: `tests/test_migrate.py`, `tests/test_usage.py`, `tests/test_stats.py`, `tests/test_admin.py`, `tests/test_llm.py`, `tests/test_digest.py`

**Interfaces:**
- Consumes: the `llm_calls` table shipped 2026-09-13.
- Produces: table `model_calls`, same columns plus `audio_seconds NUMERIC(10,2) NULL`.

Nova-3 is an ASR model, not an LLM, so `llm_calls` becomes a misnomer the moment transcription rows land in it. The table is one day old and `ALTER TABLE ... RENAME` preserves every row.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_migrate.py`:

```python
async def test_model_calls_replaces_llm_calls_and_carries_audio_seconds(pool):
    """Renamed because Nova-3 is an ASR model, not an LLM — the old name goes
    wrong the moment a transcription row lands in it. audio_seconds is NULL
    for token-billed calls; cost branches on which unit the row carries."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name IN ('llm_calls','model_calls')"""
        )
        tables = {r[0] for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'model_calls' AND column_name = 'audio_seconds'"""
        )
        has_audio = await cur.fetchone() is not None

    assert tables == {"model_calls"}, "llm_calls must be gone, not duplicated"
    assert has_audio


async def test_the_rename_preserves_existing_rows(pool):
    """The table is young but not empty in production. A rename that loses
    rows is a data-loss bug wearing a refactor's clothes."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO model_calls (job, model, input_tokens, output_tokens)
               VALUES ('turn', 'claude-opus-5', 10, 5)"""
        )
        cur = await conn.execute("SELECT count(*) FROM model_calls")
        assert (await cur.fetchone())[0] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_migrate.py -q`
Expected: FAIL — `tables == {"llm_calls"}`, and `model_calls` does not exist.

- [ ] **Step 3: Write the migration**

Create `migrations/004_model_calls.sql`:

```sql
-- Renamed from llm_calls. Nova-3, which transcribes voice notes, is a
-- dedicated ASR model and not an LLM — the old name became wrong the moment
-- transcription rows joined the table. The table was one day old when this
-- ran; RENAME preserves every row and every index.
ALTER TABLE llm_calls RENAME TO model_calls;
ALTER INDEX idx_llm_calls_day RENAME TO idx_model_calls_day;

-- NULL for token-billed calls. Transcription bills in audio-seconds, and
-- every other column in this table assumes tokens, so the unit a row is
-- priced in is what this column records. stats.row_cost branches on it.
ALTER TABLE model_calls ADD COLUMN audio_seconds NUMERIC(10,2);
```

- [ ] **Step 4: Propagate the name**

Replace `llm_calls` with `model_calls` in `gaia/core/usage.py` (the INSERT) and `gaia/core/stats.py` (three queries: the main SELECT, the calls-per-turn subquery). Then in the test files:

```bash
grep -rln "llm_calls" gaia/ tests/ docs/RUNBOOK.md
sed -i 's/llm_calls/model_calls/g' $(grep -rl "llm_calls" gaia/ tests/ docs/RUNBOOK.md)
```

Check the result by hand — `docs/RUNBOOK.md` mentions the table in prose, and that sentence should still read well.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add migrations/004_model_calls.sql gaia/ tests/ docs/RUNBOOK.md
git commit -m "refactor: llm_calls becomes model_calls, gains audio_seconds"
```

---

### Task 2: Price audio, and surface it in the report

**Files:**
- Modify: `gaia/core/stats.py`, `gaia/core/admin.py`, `gaia/core/stats_html.py`
- Test: `tests/test_stats.py`, `tests/test_admin.py`

**Interfaces:**
- Consumes: `model_calls.audio_seconds` from Task 1.
- Produces: `stats.AUDIO_PRICES: dict[str, float]` (USD per minute); `row_cost` gains an `audio_seconds` parameter; `collect()` returns `audio_minutes: float`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stats.py`:

```python
def test_audio_is_priced_per_minute_not_per_token():
    """Transcription bills in audio-seconds. 120 seconds of nova-3 at
    $0.0043/min is $0.0086 — and the token columns are zero, so a token-based
    reading of the same row would report it as free."""
    cost = stats.row_cost("nova-3", 0, 0, 0, 0, audio_seconds=120)

    assert round(cost, 6) == round(2 * 0.0043, 6)


def test_an_unpriced_audio_vendor_reports_no_cost():
    assert stats.row_cost("some-other-asr", 0, 0, 0, 0, audio_seconds=60) is None


def test_token_pricing_is_unaffected_by_the_audio_branch():
    """The existing path must not move. Same fixed mix as before."""
    cost = stats.row_cost("claude-opus-5", 1000, 500, 1000, 10000)

    assert round(cost, 6) == 0.02875


async def test_collect_reports_audio_minutes_and_transcription_cost(conn, ana):
    await conn.execute(
        """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                    audio_seconds)
           VALUES ('transcription', %s, 'nova-3', 0, 0, 150)""",
        (ana.id,),
    )

    data = await stats.collect(conn, days=30)

    assert data["audio_minutes"] == 2.5
    by_job = {r["job"]: r for r in data["by_job"]}
    assert round(by_job["transcription"]["cost"], 6) == round(2.5 * 0.0043, 6)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stats.py -q`
Expected: FAIL — `row_cost() got an unexpected keyword argument 'audio_seconds'`.

- [ ] **Step 3: Implement**

In `gaia/core/stats.py`, add beside `PRICES`:

```python
# USD per MINUTE of audio. A different unit from PRICES above, deliberately
# kept in a separate dict rather than bolted into it: a per-token rate and a
# per-minute rate that live in the same shape are a mistake waiting to be made.
AUDIO_PRICES: dict[str, float] = {
    "nova-3": 0.0043,
}
```

Change `row_cost`:

```python
def row_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    audio_seconds: float | None = None,
) -> float | None:
    """Dollars for one call, or None if the model has no rate card.

    Branches on the unit the row is billed in. A transcription row carries
    audio_seconds and zeros in every token column, so pricing it as tokens
    would report it as free rather than as unpriced.
    """
    if audio_seconds is not None:
        rate = AUDIO_PRICES.get(model)
        return None if rate is None else (audio_seconds / 60.0) * rate

    price = PRICES.get(model)
    if price is None:
        return None
    return (
        input_tokens * price["input"]
        + cache_creation * price["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read * price["input"] * CACHE_READ_MULTIPLIER
        + output_tokens * price["output"]
    ) / 1e6
```

Change `_cost_of`:

```python
def _cost_of(row: dict) -> float:
    return row_cost(
        row["model"], row["input_tokens"], row["output_tokens"],
        row["cache_creation_input_tokens"], row["cache_read_input_tokens"],
        audio_seconds=row.get("audio_seconds"),
    ) or 0.0
```

In `collect`, add `audio_seconds` to the main SELECT column list, change the unknown-model set to respect both rate cards, and add the total to the returned dict:

```python
    unknown = sorted({
        c["model"] for c in calls
        if (c["audio_seconds"] is not None and c["model"] not in AUDIO_PRICES)
        or (c["audio_seconds"] is None and c["model"] not in PRICES)
    })
```

```python
        "audio_minutes": sum(
            float(c["audio_seconds"] or 0) for c in calls
        ) / 60.0,
```

(replace the existing `"unknown_models": ...` line with `"unknown_models": unknown,`)

- [ ] **Step 4: Surface it in both reports**

In `gaia/core/admin.py`, inside `_format_stats`, after the cache-hit-rate line:

```python
        if d["audio_minutes"]:
            lines.append(f"  audio transcribed: {d['audio_minutes']:.1f} min")
```

In `gaia/core/stats_html.py`, in the cards block, after the cache-hit-rate card:

```python
            f"<div class='card'><div class='label'>Audio transcribed</div>"
            f"<div class='big'>{d['audio_minutes']:.0f}<span style='font-size:16px'> min</span></div></div>",
```

Add `"audio_minutes": 12.5` to the `DATA` fixture in `tests/test_stats_html.py`, and `"audio_minutes": 0` to the `empty` dict in `test_an_empty_window_still_renders`.

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gaia/core/stats.py gaia/core/admin.py gaia/core/stats_html.py tests/
git commit -m "feat: price audio per minute and report the minutes transcribed"
```

---

### Task 3: Parse audio messages, and split fetching from image processing

**Files:**
- Modify: `gaia/core/whatsapp.py`
- Test: `tests/test_whatsapp.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_messages` emits `{"id", "from", "type": "audio", "audio_id", "mime_type", "voice"}`; `WhatsAppClient.download_audio(media_id) -> tuple[bytes, str]` returning `(raw_bytes, mime_type)`; `download_media` unchanged for images.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_whatsapp.py`:

```python
def test_an_audio_message_is_parsed_rather_than_marked_unsupported():
    """Before this, every voice note reached the model as the literal string
    '[unsupported message type: audio]'."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.1", "from": "13055550001", "type": "audio",
        "audio": {"id": "1908647269898587", "mime_type": "audio/ogg; codecs=opus",
                  "sha256": "abc=", "voice": True},
    }]}}]}]}

    parsed = whatsapp.parse_messages(payload)

    assert parsed == [{
        "id": "wamid.1", "from": "13055550001", "type": "audio",
        "audio_id": "1908647269898587", "mime_type": "audio/ogg; codecs=opus",
        "voice": True,
    }]


def test_an_uploaded_audio_file_parses_the_same_way():
    """voice=False is a forwarded recording rather than the microphone button.
    Same content by a different route, so it takes the same path — the flag is
    recorded, not acted on."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.2", "from": "13055550001", "type": "audio",
        "audio": {"id": "999", "mime_type": "audio/mpeg", "voice": False},
    }]}}]}]}

    assert whatsapp.parse_messages(payload)[0]["voice"] is False


def test_an_unknown_message_type_still_degrades_to_a_note():
    """The existing fallback must survive: a sticker or a location is not a
    crash, it is a message the model can explain."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.3", "from": "1", "type": "sticker", "sticker": {"id": "x"},
    }]}}]}]}

    assert "unsupported message type" in whatsapp.parse_messages(payload)[0]["text"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatsapp.py -q`
Expected: FAIL — the audio message comes back as `{"type": "text", "text": "[unsupported message type: audio]"}`.

- [ ] **Step 3: Add the parse branch**

In `gaia/core/whatsapp.py`, inside `parse_messages`, add before the `else`:

```python
                elif m["type"] == "audio":
                    base["audio_id"] = m["audio"]["id"]
                    base["mime_type"] = m["audio"].get("mime_type", "audio/ogg")
                    # True when recorded with WhatsApp's microphone button,
                    # False for a forwarded audio file. Recorded because it is
                    # cheap to keep and tells you what people actually do; not
                    # acted on, because both are meeting notes.
                    base["voice"] = m["audio"].get("voice", False)
```

- [ ] **Step 4: Split fetching from image processing**

`download_media` currently fetches *and* calls `downscale`, which is Pillow. Audio through that path dies inside Pillow rather than at a boundary. Replace the single method with:

```python
    async def _fetch_media(self, media_id: str) -> tuple[str, bytes]:
        """The two-step Graph fetch: metadata, then the bytes. Knows nothing
        about what kind of media it is holding."""
        async with httpx.AsyncClient(timeout=30) as client:
            meta = (await client.get(f"{GRAPH}/{media_id}", headers=self._headers)).json()
            if "url" not in meta:
                raise RuntimeError(f"no media url for {media_id}: {meta}")
            blob = await client.get(meta["url"], headers=self._headers)
            blob.raise_for_status()
        return meta.get("mime_type", ""), blob.content

    async def download_media(self, media_id: str) -> dict:
        """An image, downscaled to the model's resolution ceiling."""
        _, content = await self._fetch_media(media_id)
        media_type, data = downscale(content)
        return {"media_type": media_type, "data": data}

    async def download_audio(self, media_id: str) -> tuple[bytes, str]:
        """Raw bytes and MIME type, untouched. Deepgram accepts OGG/Opus
        directly, so there is nothing to transcode and no ffmpeg in the image."""
        mime_type, content = await self._fetch_media(media_id)
        return content, mime_type or "audio/ogg"
```

- [ ] **Step 5: Test the split**

`tests/test_whatsapp.py` already has `_stub_async_client(monkeypatch, handler)`. Add:

```python
async def test_audio_is_returned_raw_and_never_reaches_pillow(monkeypatch):
    """download_media downscales, which is Pillow. Audio bytes through that
    path die inside Pillow rather than at a boundary, which is why fetching
    and image processing were separated."""
    def handler(request):
        if "/media-1" in str(request.url) or request.url.path.endswith("media-1"):
            return httpx.Response(200, json={
                "url": "https://lookaside.example/blob", "mime_type": "audio/ogg; codecs=opus",
            })
        return httpx.Response(200, content=b"OggS-raw-bytes")

    _stub_async_client(monkeypatch, handler)
    called = []
    monkeypatch.setattr(
        whatsapp, "downscale", lambda b: called.append(b) or ("image/jpeg", "x")
    )

    content, mime_type = await whatsapp.WhatsAppClient(
        token="t", phone_number_id="1"
    ).download_audio("media-1")

    assert content == b"OggS-raw-bytes", "audio must come back untouched"
    assert mime_type == "audio/ogg; codecs=opus"
    assert called == [], "downscale must not be called for audio"


async def test_images_still_go_through_downscale(monkeypatch):
    """The other half: the split must not have changed the image path."""
    def handler(request):
        if request.url.path.endswith("media-2"):
            return httpx.Response(200, json={
                "url": "https://lookaside.example/blob", "mime_type": "image/jpeg",
            })
        return httpx.Response(200, content=b"jpeg-bytes")

    _stub_async_client(monkeypatch, handler)
    called = []
    monkeypatch.setattr(
        whatsapp, "downscale", lambda b: called.append(b) or ("image/jpeg", "ZmFrZQ==")
    )

    out = await whatsapp.WhatsAppClient(
        token="t", phone_number_id="1"
    ).download_media("media-2")

    assert called == [b"jpeg-bytes"]
    assert out == {"media_type": "image/jpeg", "data": "ZmFrZQ=="}
```

- [ ] **Step 6: Add the fake**

In `tests/fakes.py`, add to `FakeWhatsApp`:

```python
    async def download_audio(self, media_id: str) -> tuple[bytes, str]:
        self.downloaded.append(media_id)
        if media_id in self._media_errors:
            raise RuntimeError(f"could not download {media_id}")
        return b"OggS-fake-audio-bytes", "audio/ogg; codecs=opus"
```

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `test_images.py` and `test_butler.py` also prove the image path is unchanged.

- [ ] **Step 8: Commit**

```bash
git add gaia/core/whatsapp.py tests/test_whatsapp.py tests/fakes.py
git commit -m "feat: parse audio messages; separate media fetching from image processing"
```

---

### Task 4: The transcription module

**Files:**
- Create: `gaia/core/transcription.py`
- Modify: `gaia/core/config.py`, `.env.example`, `tests/conftest.py`
- Test: `tests/test_transcription.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `async def transcribe(audio: bytes, mime_type: str, keyterms: list[str]) -> tuple[str, float]` returning `(transcript, audio_seconds)`, raising `TranscriptionError` on vendor failure or empty result. Settings gain `deepgram_api_key: str = ""`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcription.py`:

Tests monkeypatch `transcription.httpx.AsyncClient`, copying the
`_stub_async_client` helper that `tests/test_whatsapp.py` already uses for the
Graph API. **No test hook in the module itself** — a module-level `_transport`
that only tests ever set is production code existing for tests, which is the
thing the pool parameter in the observability work was written to avoid.

```python
import httpx
import pytest

from gaia.core import transcription


def _stub_async_client(monkeypatch, handler):
    """Point transcription.httpx.AsyncClient at an in-process MockTransport so
    no request leaves the machine. Same technique as tests/test_whatsapp.py."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(transcription.httpx, "AsyncClient", factory)


async def test_the_transcript_and_duration_come_back(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "metadata": {"duration": 12.5},
            "results": {"channels": [{"alternatives": [
                {"transcript": "Met Marta Delgado at the Coral Gables listing."}
            ]}]},
        })

    _stub_async_client(monkeypatch, handler)

    text, seconds = await transcription.transcribe(b"audio", "audio/ogg", [])

    assert text == "Met Marta Delgado at the Coral Gables listing."
    assert seconds == 12.5


async def test_every_request_opts_out_of_model_training(monkeypatch):
    """Not a setting and not a default to rely on. The program is documented
    as opt-in and the API also exposes a per-request opt-out; depending on
    which applies to a given plan is not a decision worth making."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "metadata": {"duration": 1.0},
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
        })

    _stub_async_client(monkeypatch, handler)
    await transcription.transcribe(b"audio", "audio/ogg", [])

    assert "mip_opt_out=true" in seen["url"]
    assert "model=nova-3" in seen["url"]
    assert "language=en" in seen["url"]


async def test_keyterms_are_sent_one_parameter_each(monkeypatch):
    """The whole point of the vendor choice. Rare proper nouns are what this
    feature gets wrong without them."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "metadata": {"duration": 1.0},
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
        })

    _stub_async_client(monkeypatch, handler)
    await transcription.transcribe(b"a", "audio/ogg", ["Cesia", "Marta Delgado"])

    assert "keyterm=Cesia" in seen["url"]
    assert "keyterm=Marta+Delgado" in seen["url"] or "keyterm=Marta%20Delgado" in seen["url"]


async def test_keyterms_are_capped_at_the_documented_limit(monkeypatch):
    """100 terms, 500 tokens. Sending an unbounded roster takes an error
    instead of a transcript."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "metadata": {"duration": 1.0},
            "results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]},
        })

    _stub_async_client(monkeypatch, handler)
    await transcription.transcribe(b"a", "audio/ogg", [f"Name{i}" for i in range(250)])

    assert seen["url"].count("keyterm=") == transcription.MAX_KEYTERMS


async def test_a_vendor_error_raises(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"err_msg": "bad key"})

    _stub_async_client(monkeypatch, handler)

    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"a", "audio/ogg", [])


async def test_an_empty_transcript_raises_rather_than_filing_nothing(monkeypatch):
    """Silence, or a pocket recording. A meeting filed from an empty
    transcript is worse than one visibly not filed."""
    def handler(request):
        return httpx.Response(200, json={
            "metadata": {"duration": 3.0},
            "results": {"channels": [{"alternatives": [{"transcript": "   "}]}]},
        })

    _stub_async_client(monkeypatch, handler)

    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"a", "audio/ogg", [])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_transcription.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.transcription'`.

- [ ] **Step 3: Write the module**

Create `gaia/core/transcription.py`:

```python
"""Speech to text, behind one interface.

Claude accepts no audio input, so a voice note is transcribed before the model
ever sees it. That makes this the one place a third party hears a client — and
the reason the vendor is isolated here the way core/embeddings.py isolates
Voyage: swapping it, including for a locally hosted model later, must not
reach the pipeline.
"""

import logging

import httpx

from gaia.core.config import settings

log = logging.getLogger("gaia.transcription")

ENDPOINT = "https://api.deepgram.com/v1/listen"

# Documented Nova-3 limits: 100 keyterms, 500 tokens across all of them.
# Sending an unbounded roster returns an error instead of a transcript.
MAX_KEYTERMS = 100

class TranscriptionError(Exception):
    """The vendor failed, or heard nothing. Either way there is no transcript,
    and the caller must degrade visibly rather than file an empty meeting."""


async def transcribe(
    audio: bytes, mime_type: str, keyterms: list[str]
) -> tuple[str, float]:
    """Returns (transcript, audio_seconds). Raises TranscriptionError.

    Every parameter below is fixed rather than configurable. `mip_opt_out` in
    particular is a correctness property, not a setting: the model-improvement
    program is documented as opt-in and the API also exposes a per-request
    opt-out, and which default applies to a given plan is not something this
    code should depend on.
    """
    params = [
        ("model", "nova-3"),
        ("language", "en"),
        ("mip_opt_out", "true"),
        ("smart_format", "true"),
    ]
    params += [("keyterm", term) for term in keyterms[:MAX_KEYTERMS]]

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                ENDPOINT,
                params=params,
                content=audio,
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": mime_type,
                },
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        raise TranscriptionError(f"transcription request failed: {exc}") from exc

    try:
        alternative = body["results"]["channels"][0]["alternatives"][0]
        text = (alternative.get("transcript") or "").strip()
        seconds = float(body.get("metadata", {}).get("duration", 0.0))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TranscriptionError(f"unexpected transcription response: {exc}") from exc

    if not text:
        # Silence, or a pocket recording. Filing a meeting from this is worse
        # than visibly not filing one.
        raise TranscriptionError("empty transcript")

    return text, seconds
```

- [ ] **Step 4: Add the setting**

In `gaia/core/config.py`, after `digest_model`:

```python
    # Deepgram, for transcribing voice notes. Empty by default so the test
    # suite and a checkout with no voice feature configured both import
    # cleanly; a transcription attempted without it fails visibly at the
    # vendor rather than silently doing nothing.
    deepgram_api_key: str = ""
```

In `.env.example`, after the Voyage block:

```
# ---------- transcription ----------
# Deepgram, for voice notes. Nova-3, English, with the contact roster sent as
# keyterms so rare names survive. Every request sets mip_opt_out=true, so audio
# is not retained for model training.
DEEPGRAM_API_KEY=...
```

In `tests/conftest.py`, beside the other pinned values:

```python
os.environ.setdefault("DEEPGRAM_API_KEY", "dg-test")
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_transcription.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gaia/core/transcription.py gaia/core/config.py .env.example tests/
git commit -m "feat: transcription module — nova-3, roster keyterms, training opt-out"
```

---

### Task 5: The audio branch in the turn

**Files:**
- Modify: `gaia/butler.py`
- Test: `tests/test_butler.py`

**Interfaces:**
- Consumes: `whatsapp.download_audio`, `transcription.transcribe`, `contacts_db.roster`.
- Produces: an audio message in a burst becomes a text block `[voice note] <transcript>`; `VOICE_FAILED_NOTE` on failure.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_butler.py`:

```python
async def test_a_voice_note_becomes_a_labelled_text_block(conn, ana, monkeypatch):
    """Labelled, not bare. The model must know the text is a machine
    transcription so it can check names against the roster, and the message log
    must not imply she typed it."""
    async def fake_transcribe(audio, mime_type, keyterms):
        return "Met Marta Delgado at the Coral Gables listing.", 12.5

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)
    wa = FakeWhatsApp()
    batch = [{"id": "wamid.1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    blocks, wa_ids = await butler._build_blocks(conn, ana, batch, wa)

    assert blocks == [{"type": "text",
                       "text": "[voice note] Met Marta Delgado at the Coral Gables listing."}]
    assert wa_ids == ["wamid.1"]


async def test_the_roster_is_sent_as_keyterms(conn, ana, monkeypatch):
    """The whole trick. No ASR model has seen 'Okonkwo'; the roster is what
    makes it survive."""
    seen = {}

    async def fake_transcribe(audio, mime_type, keyterms):
        seen["keyterms"] = keyterms
        return "text", 1.0

    from gaia.core.db import meetings as meetings_db
    await meetings_db.save(conn, ana, summary="s", source="text",
                           contact_names=["Marta Delgado", "Okonkwo"])

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)
    batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    await butler._build_blocks(conn, ana, batch, FakeWhatsApp())

    assert "Okonkwo" in seen["keyterms"]
    assert "Marta Delgado" in seen["keyterms"]


async def test_a_failed_transcription_does_not_lose_the_rest_of_the_burst(
    conn, ana, monkeypatch
):
    """Same rule as an unreadable photo: one bad item becomes a visible note,
    never a dropped batch."""
    from gaia.core.transcription import TranscriptionError

    async def boom(audio, mime_type, keyterms):
        raise TranscriptionError("vendor down")

    monkeypatch.setattr("gaia.butler.transcription.transcribe", boom)
    batch = [
        {"id": "w1", "type": "audio", "audio_id": "m1",
         "mime_type": "audio/ogg", "voice": True},
        {"id": "w2", "type": "text", "text": "and the comps are due Friday"},
    ]

    blocks, wa_ids = await butler._build_blocks(conn, ana, batch, FakeWhatsApp())

    assert butler.VOICE_FAILED_NOTE in [b["text"] for b in blocks]
    assert "and the comps are due Friday" in [b["text"] for b in blocks]
    assert wa_ids == ["w1", "w2"]


async def test_a_failed_download_is_also_survivable(conn, ana):
    """The vendor is not the only thing that can fail."""
    wa = FakeWhatsApp(media_errors={"m1"})
    batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
              "mime_type": "audio/ogg", "voice": True}]

    blocks, _ = await butler._build_blocks(conn, ana, batch, wa)

    assert blocks == [{"type": "text", "text": butler.VOICE_FAILED_NOTE}]


def test_a_voice_note_reads_as_one_in_history():
    assert butler.transcript_text(
        {"type": "audio", "transcript": "Met Marta."}
    ) == "[voice note] Met Marta."
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_butler.py -q`
Expected: FAIL — `module 'gaia.butler' has no attribute 'transcription'` and no `VOICE_FAILED_NOTE`.

- [ ] **Step 3: Implement**

In `gaia/butler.py`, add to the imports:

```python
from gaia.core import transcription
```

Add beside `PHOTO_FAILED_NOTE`:

```python
VOICE_FAILED_NOTE = "[a voice note in this message could not be transcribed]"
VOICE_PREFIX = "[voice note] "
```

In `transcript_text`, add before the image branch:

```python
    if message["type"] == "audio":
        return f"{VOICE_PREFIX}{message.get('transcript', '')}".rstrip()
```

In `_build_blocks`, add a branch beside the image one:

```python
        elif message["type"] == "audio":
            try:
                audio, mime_type = await wa.download_audio(message["audio_id"])
                # Ownership- and visibility-scoped, so only the names this
                # developer actually works with leave the box — never the
                # company's whole contact book.
                keyterms = await contacts_db.roster(conn, user, limit=100)
                text, _seconds = await transcription.transcribe(
                    audio, mime_type, keyterms
                )
            except Exception:
                log.exception(
                    "could not transcribe audio %s for user %s",
                    message["audio_id"], user.id,
                )
                blocks.append({"type": "text", "text": VOICE_FAILED_NOTE})
                await messages_db.set_content(
                    conn, user, message["id"], VOICE_FAILED_NOTE
                )
                continue
            blocks.append({"type": "text", "text": f"{VOICE_PREFIX}{text}"})
            await messages_db.set_content(
                conn, user, message["id"], f"{VOICE_PREFIX}{text}"
            )
```

Add one sentence to `BASE_PROMPT`, after the paragraph about meeting notes:

```
A message beginning "[voice note]" is a machine transcription of dictated audio. \
It hears ordinary English well and mishears names — check every name against the \
people listed below and ask {name} about any that do not match one.
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_butler.py -q`
Expected: PASS.

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gaia/butler.py tests/test_butler.py
git commit -m "feat: transcribe voice notes inside the turn, with the roster as keyterms"
```

---

### Task 6: `voice_note` as a meeting source

**Files:**
- Modify: `gaia/capabilities/meetings/__init__.py`, `gaia/capabilities/meetings/tools.py`, `gaia/core/stats.py`, `gaia/core/admin.py`, `gaia/core/stats_html.py`
- Test: `tests/test_capability_meetings.py`, `tests/test_stats.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `meetings.source` may be `voice_note`; `collect()["meetings"]` gains a `voice` count.

`save_meeting` currently derives `source="photo_notes" if raw_transcription else "text"`. A voice note whose transcript the model passes as `raw_transcription` would therefore be filed as a **photo**. The model can tell the difference — it sees the `[voice note]` label — so the schema should let it say.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_capability_meetings.py`:

```python
async def test_a_dictated_meeting_is_filed_as_a_voice_note(conn, ana):
    """Without this it lands as photo_notes, because source was derived from
    raw_transcription being present — which is true of a transcript too."""
    await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables",
        "raw_transcription": "Met Marta at the listing this morning...",
        "source": "voice_note",
    })

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "voice_note"


async def test_a_photo_still_defaults_to_photo_notes(conn, ana):
    """The derivation stays as the fallback; only an explicit source overrides."""
    await save_meeting(conn, ana, {
        "summary": "Showed Coral Gables",
        "raw_transcription": "handwriting, transcribed",
    })

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "photo_notes"


async def test_a_typed_note_still_defaults_to_text(conn, ana):
    await save_meeting(conn, ana, {"summary": "Quick note"})

    cur = await conn.execute("SELECT source FROM meetings WHERE user_id = %s", (ana.id,))
    assert (await cur.fetchone())["source"] == "text"
```

Add to `tests/test_stats.py`:

```python
async def test_collect_counts_voice_meetings_separately(conn, ana):
    """Without its own bucket a dictated meeting vanishes from the split —
    counted in the total, absent from photo and text."""
    await meetings_db.save(conn, ana, summary="dictated", source="voice_note")

    data = await stats.collect(conn, days=30)

    assert data["meetings"]["total"] == 1
    assert data["meetings"]["voice"] == 1
    assert data["meetings"]["photo"] == 0
    assert data["meetings"]["text"] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_capability_meetings.py tests/test_stats.py -q`
Expected: FAIL — source comes back `photo_notes`, and `KeyError: 'voice'`.

- [ ] **Step 3: Implement**

In `gaia/capabilities/meetings/__init__.py`, add to `SAVE_SCHEMA["properties"]`:

```python
        "source": {
            "type": "string",
            "enum": ["text", "photo_notes", "voice_note"],
            "description": "Where these notes came from. Set voice_note when the "
                           "message was prefixed [voice note], photo_notes for a "
                           "photograph of handwriting, text when they were typed. "
                           "Inferred from the transcription if omitted.",
        },
```

In `gaia/capabilities/meetings/tools.py`, replace the `source=` line:

```python
        # The model sets this when it can tell — it sees the [voice note]
        # prefix. The derivation below is the fallback, and it cannot
        # distinguish a dictated transcript from a photographed one: both
        # arrive as raw_transcription.
        source=args.get("source") or ("photo_notes" if raw_transcription else "text"),
```

In `gaia/core/stats.py`, extend the meetings query:

```python
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE source = 'photo_notes') AS photo,
                  count(*) FILTER (WHERE source = 'text') AS text,
                  count(*) FILTER (WHERE source = 'voice_note') AS voice
           FROM meetings
           WHERE created_at >= now() - make_interval(days => %(days)s)""",
```

In `gaia/core/admin.py`, change the meetings line in `_format_stats`:

```python
        f"  meetings filed     {m['total']}  ({m['photo']} photo, {m['text']} text, "
        f"{m['voice']} voice)",
```

In `gaia/core/stats_html.py`, change the meetings row:

```python
            ["Meetings filed", f"{m['total']:,}",
             f"{m['photo']} photo / {m['text']} text / {m['voice']} voice"],
```

Add `"voice": 0` to the `meetings` dict in `tests/test_stats_html.py`'s `DATA`.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gaia/capabilities/meetings/ gaia/core/stats.py gaia/core/admin.py gaia/core/stats_html.py tests/
git commit -m "feat: voice_note as a meeting source, reported in its own bucket"
```

---

### Task 7: Record what transcription costs

**Files:**
- Modify: `gaia/butler.py`
- Test: `tests/test_butler.py`

**Interfaces:**
- Consumes: `usage.record` and `model_calls.audio_seconds` from Tasks 1–2.
- Produces: one `model_calls` row per transcription attempt — `job='transcription'`, `model='nova-3'`, `audio_seconds` set, `turn_id` NULL; failures carry `stop_reason='error'`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_butler.py`:

```python
async def test_a_transcription_is_recorded_with_its_duration(migrated, monkeypatch):
    """Priced per minute, so the duration is the billable fact. turn_id stays
    NULL: this runs before run_agent exists, and calls_per_turn counts
    agent-loop iterations."""
    from psycopg.rows import dict_row

    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async def fake_transcribe(audio, mime_type, keyterms):
        return "Met Marta.", 150.0

    monkeypatch.setattr("gaia.butler.transcription.transcribe", fake_transcribe)

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559003")
    async with tx(migrated) as conn:
        batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
                  "mime_type": "audio/ogg", "voice": True}]
        await butler._build_blocks(conn, user, batch, FakeWhatsApp(), pool=migrated)

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT * FROM model_calls")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["job"] == "transcription"
    assert rows[0]["model"] == "nova-3"
    assert float(rows[0]["audio_seconds"]) == 150.0
    assert rows[0]["turn_id"] is None
    assert rows[0]["user_id"] == user.id


async def test_a_failed_transcription_is_recorded_as_an_error(migrated, monkeypatch):
    """A failure rate is a product signal. Without a row it is invisible until
    users complain."""
    from psycopg.rows import dict_row

    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx
    from gaia.core.transcription import TranscriptionError

    async def boom(audio, mime_type, keyterms):
        raise TranscriptionError("vendor down")

    monkeypatch.setattr("gaia.butler.transcription.transcribe", boom)

    async with tx(migrated) as conn:
        user = await users_db.create_user(conn, name="Ana", wa_id="13055559004")
    async with tx(migrated) as conn:
        batch = [{"id": "w1", "type": "audio", "audio_id": "m1",
                  "mime_type": "audio/ogg", "voice": True}]
        await butler._build_blocks(conn, user, batch, FakeWhatsApp(), pool=migrated)

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        cur = await conn.execute("SELECT * FROM model_calls")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["stop_reason"] == "error"
    assert rows[0]["job"] == "transcription"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_butler.py -q`
Expected: FAIL — `_build_blocks() got an unexpected keyword argument 'pool'`.

- [ ] **Step 3: Thread the pool and record**

`_build_blocks` needs a pool, because `usage.record` opens its own transaction on another connection — joining the caller's would roll the telemetry back with it.

**This changes a signature Task 5 already committed tests against.** Every `butler._build_blocks(conn, user, batch, wa)` call in `tests/test_butler.py` gains a fifth argument. Find them first:

```bash
grep -n "_build_blocks(" tests/test_butler.py
```

The audio tests from Task 5 use the `conn`/`ana` fixtures and do not assert on telemetry, so they can pass `None` — `_record_transcription` is guarded and a `None` pool degrades to a logged miss, which is the behaviour those tests want anyway. Tests that *do* assert on rows pass `migrated`.

Change the signature and the one caller in `handle_turn`:

```python
async def _build_blocks(conn, user: User, batch: list[dict], wa, pool) -> tuple[list[dict], list[str]]:
```

```python
        async with tx() as conn:
            blocks, wa_ids = await _build_blocks(conn, user, batch, wa, get_pool())
```

Add to the imports:

```python
import time

from gaia.core import usage as usage_mod
```

Wrap the transcription call in the audio branch:

```python
        elif message["type"] == "audio":
            started = time.monotonic()
            try:
                audio, mime_type = await wa.download_audio(message["audio_id"])
                keyterms = await contacts_db.roster(conn, user, limit=100)
                text, seconds = await transcription.transcribe(
                    audio, mime_type, keyterms
                )
            except Exception:
                log.exception(
                    "could not transcribe audio %s for user %s",
                    message["audio_id"], user.id,
                )
                await _record_transcription(
                    pool, user, 0.0, started, stop_reason="error"
                )
                blocks.append({"type": "text", "text": VOICE_FAILED_NOTE})
                await messages_db.set_content(
                    conn, user, message["id"], VOICE_FAILED_NOTE
                )
                continue
            await _record_transcription(pool, user, seconds, started)
            blocks.append({"type": "text", "text": f"{VOICE_PREFIX}{text}"})
            await messages_db.set_content(
                conn, user, message["id"], f"{VOICE_PREFIX}{text}"
            )
```

Add the helper above `_build_blocks`:

```python
async def _record_transcription(
    pool, user: User, seconds: float, started: float, stop_reason: str | None = None
) -> None:
    """One row per transcription attempt, successes and failures alike.

    Guarded here as well as inside record(), matching the agent loop: a note
    that transcribed correctly must not be lost because a metrics insert
    failed. No turn_id — this runs before run_agent exists, and
    calls_per_turn counts agent-loop iterations.
    """
    class _NoTokens:
        input_tokens = 0
        output_tokens = 0

    try:
        await usage_mod.record(
            pool, job="transcription", user=user, model="nova-3",
            usage=_NoTokens(), stop_reason=stop_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
            audio_seconds=seconds,
        )
    except Exception:
        log.exception("could not record a transcription for user %s", user.id)
```

In `gaia/core/usage.py`, add `audio_seconds=None` to `record`'s signature, to the INSERT column list, and to the values tuple.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_butler.py tests/test_usage.py -q`
Expected: PASS — including the Task 5 audio tests, now passing a pool.

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gaia/butler.py gaia/core/usage.py tests/
git commit -m "feat: record transcription cost and failures in model_calls"
```

---

### Task 8: Live verification and documentation

**Files:**
- Create: `tests/live/test_live_transcription.py`, `tests/live/fixtures/note.ogg`
- Modify: `docs/RUNBOOK.md`, `README.md`
- Test: as created

**Interfaces:**
- Consumes: everything above.
- Produces: a live-tier test proving the real vendor's response shape, and the credential documented.

A fake cannot prove the vendor's JSON shape, the auth header, or that keyterms do anything. That is what this tier is for.

- [ ] **Step 1: Record the fixture**

Record roughly ten seconds of audio saying, clearly:

> "Met Marta Delgado at the Coral Gables listing this morning. I promised to call Cesia about the Okonkwo closing."

Save it as OGG/Opus at `tests/live/fixtures/note.ogg`:

```bash
ffmpeg -i input.m4a -c:a libopus tests/live/fixtures/note.ogg
```

This fixture contains no real client data — the names are the ones already used throughout the test suite — so it is safe to commit.

- [ ] **Step 2: Write the live test**

Create `tests/live/test_live_transcription.py`:

```python
"""The real vendor, the real response shape.

A fake proves the pipeline handles what it is handed. It cannot prove the
auth header is right, that the JSON has the shape the module unpacks, or that
keyterm prompting does anything at all — and keyterms are the entire reason
this vendor was chosen.
"""

import pathlib

import pytest

from gaia.core import transcription

pytestmark = pytest.mark.live

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "note.ogg"


@pytest.fixture(scope="session")
def deepgram_key():
    import os
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key or key == "dg-test":
        pytest.skip("no real DEEPGRAM_API_KEY")
    return key


async def test_a_real_recording_comes_back_as_text(deepgram_key):
    audio = FIXTURE.read_bytes()

    text, seconds = await transcription.transcribe(audio, "audio/ogg", [])

    assert text.strip(), "the vendor returned an empty transcript"
    assert seconds > 0, "no duration came back; audio_seconds prices the row"
    assert "coral gables" in text.lower()


async def test_keyterms_rescue_a_name_the_model_has_never_seen(deepgram_key):
    """The reason this vendor was chosen. Asserted as a comparison rather than
    against a fixed string: what matters is that the roster changes the
    outcome, not that any one spelling is produced."""
    audio = FIXTURE.read_bytes()

    without, _ = await transcription.transcribe(audio, "audio/ogg", [])
    with_terms, _ = await transcription.transcribe(
        audio, "audio/ogg", ["Cesia", "Okonkwo", "Marta Delgado"]
    )

    hits = sum(n.lower() in with_terms.lower() for n in ("cesia", "okonkwo"))
    assert hits >= 1, (
        f"keyterms rescued no names.\n  without: {without!r}\n  with: {with_terms!r}"
    )
```

- [ ] **Step 3: Run the live test**

Run: `.venv/bin/python -m pytest tests/live/test_live_transcription.py -m live -q`
Expected: PASS. It is billable — about a cent.

If `test_keyterms_rescue_a_name` fails, **do not weaken the assertion.** It is the evidence for the vendor decision in spec §2. Print both transcripts and reconsider: either the fixture is too clean to demonstrate the effect, or keyterms are not doing what the docs claim, and the second answer changes the design.

- [ ] **Step 4: Document the credential**

In `docs/RUNBOOK.md`, add a row to the secrets table in §3:

```markdown
| `DEEPGRAM_API_KEY` | `app` | Transcribes voice notes. Nova-3, English, with the contact roster sent as keyterms. Every request sets `mip_opt_out=true`. | Billable usage, and an attacker could transcribe their own audio on your account. Rotate in the Deepgram console. |
```

And add to §2's stack table:

```markdown
| Speech-to-text | **Deepgram Nova-3** | Claude accepts no audio input, so voice notes are transcribed before the model sees them. Chosen for keyterm prompting — general English is solved, rare client names are not. |
```

In `README.md`, update the "Next" section: voice notes move from queued to shipped.

- [ ] **Step 5: Run everything**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m live -q
```

Expected: both green.

- [ ] **Step 6: Commit**

```bash
git add tests/live/ docs/RUNBOOK.md README.md
git commit -m "test: live transcription against the real vendor; document the credential"
```

---

## Notes for the executor

- **`mip_opt_out=true` is not negotiable.** If a refactor makes the parameter list dynamic, it stays. Task 4 has a test asserting it is in the URL; that test is the guard.
- **Do not add `ffmpeg`.** WhatsApp delivers OGG/Opus and Deepgram accepts it directly. Transcoding is a dependency and a failure mode bought for nothing.
- **Deploy note for whoever ships this:** migration `004` renames a table that the running `app` and `jobs` containers query. `docker compose up -d --build` recreates both from the new image after migrations run at startup, so there is no window where old code meets the new name — but do not run the migration by hand against a live old container.
- **The live tier is billable and deselected by default.** Run `pytest -m live` before deploying; `eff08ce` is the precedent for leaving it silently red.
