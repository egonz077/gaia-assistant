"""The real vendor, the real response shape.

A fake proves the pipeline handles what it is handed. It cannot prove the auth
header is right, that the JSON has the shape the module unpacks, or that
keyterm prompting does anything at all — and keyterms are the entire reason
this vendor was chosen over a cheaper one.

The fixture is a real recording of someone saying the sentence below. It
contains no client data: the names are the ones already used throughout the
test suite.
"""

import os
import pathlib

import pytest

from gaia.core import transcription

pytestmark = pytest.mark.live

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "note.ogg"

# What the fixture says, for whoever records a replacement:
SCRIPT = (
    "Met Marta Delgado at the Coral Gables listing this morning. "
    "I promised to call Cesia about the Okonkwo closing."
)


@pytest.fixture(scope="session")
def deepgram_key():
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key or key == "dg-test":
        pytest.skip("no real DEEPGRAM_API_KEY in the environment")
    return key


@pytest.fixture
def recording(deepgram_key):
    if not FIXTURE.exists():
        pytest.skip(
            f"no recording at {FIXTURE}. Record ~10 seconds of someone saying:\n"
            f"  {SCRIPT}\n"
            "and save it as OGG/Opus there."
        )
    return FIXTURE.read_bytes()


async def test_a_real_recording_comes_back_as_text(recording):
    text, seconds = await transcription.transcribe(recording, "audio/ogg", [])

    assert text.strip(), "the vendor returned an empty transcript"
    assert seconds > 0, "no duration came back, and audio_seconds prices the row"
    assert "coral gables" in text.lower()


async def test_keyterms_rescue_a_name_the_model_has_never_seen(recording):
    """The reason this vendor was chosen over a cheaper one.

    Asserted as a comparison rather than against a fixed string: what matters
    is that the roster changes the outcome, not that any single spelling is
    produced. If this fails, do not weaken it — it is the evidence for the
    vendor decision in the design doc, and the honest readings are either that
    the fixture is too clean to show the effect or that keyterms do not do what
    the documentation claims.
    """
    without, _ = await transcription.transcribe(recording, "audio/ogg", [])
    with_terms, _ = await transcription.transcribe(
        recording, "audio/ogg", ["Cesia", "Okonkwo", "Marta Delgado"]
    )

    hits = sum(n in with_terms.lower() for n in ("cesia", "okonkwo"))
    assert hits >= 1, (
        f"keyterms rescued no names.\n  without: {without!r}\n  with: {with_terms!r}"
    )
