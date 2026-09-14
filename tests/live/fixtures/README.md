# Live test fixtures

`note.ogg` is a ~10 second recording of someone saying:

> Met Marta Delgado at the Coral Gables listing this morning. I promised to
> call Cesia about the Okonkwo closing.

Record it on a phone and convert to OGG/Opus, which is what WhatsApp delivers:

    ffmpeg -i recording.m4a -c:a libopus note.ogg

It is committed deliberately. The names are the ones already used throughout
the test suite, so it carries no client data — and a live test that skips for
want of a file nobody knows how to make is a test that never runs.

`tests/live/test_live_transcription.py` skips cleanly when it is absent.
