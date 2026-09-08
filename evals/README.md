# Transcription evals

The hardest thing this app does is read bad handwriting. Nothing in the pytest
suite measures whether it does it well — that needs real photos and real
(billable) model calls, so it lives here and is run by hand, not in CI.

## Adding examples

Drop photos into `evals/notes/`. For each `notes/<name>.jpg`, optionally write
`expected/<name>.md` describing what a correct extraction looks like: the
people, the commitments, the dates, anything ambiguous a human would have to
ask about.

## Privacy

`evals/notes/` and `evals/expected/` are **gitignored**. Real meeting notes
contain client names, budgets, and things sellers said in confidence — that
does not belong in a repository. Keep them local, or anonymise names and
figures before sharing the set.

## Running

    .venv/bin/python -m evals.run              # all
    .venv/bin/python -m evals.run --only ana1  # one

Costs real API calls. Compares against `expected/` where present, and prints
the extraction for eyeballing where not.
