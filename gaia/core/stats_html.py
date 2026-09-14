"""One self-contained page: inline CSS, inline SVG, no CDN, no JavaScript.

It has to open from a file:// URL on a laptop and on a phone, which rules out
every external asset. It is also the one artefact of this system designed to
be screenshotted and shared, so it carries aggregates only — no contact
names, no meeting text, ever. The moment it lists a client it becomes another
copy of the client book with worse access control than the database it came
from.

Every interpolated string goes through escape(). Developer names come from
the roster, which a human typed into a CLI; the report is a file someone
opens, and it must not execute what the roster says.
"""

from html import escape

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #fbfbf9; color: #1a1a18; }
h1 { font-size: 20px; margin: 0 0 2px; letter-spacing: -.01em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
     color: #6b6b64; margin: 28px 0 8px; }
.sub { color: #6b6b64; margin: 0 0 22px; }
.big { font-size: 29px; font-weight: 600; letter-spacing: -.02em; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; }
.card { flex: 1 1 150px; background: #fff; border: 1px solid #e6e6e0;
        border-radius: 10px; padding: 13px 16px; }
.card .label { color: #6b6b64; font-size: 13px; margin-bottom: 2px; }
table { border-collapse: collapse; width: 100%; max-width: 620px; }
td, th { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid #ececE6; }
th { color: #6b6b64; font-weight: 500; font-size: 13px; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
.note { color: #8a8a80; font-size: 13px; max-width: 620px; }
@media (prefers-color-scheme: dark) {
  body { background: #17171a; color: #ececE8; }
  .card { background: #202024; border-color: #32323a; }
  td, th { border-color: #2a2a31; }
}
"""


def _bars(by_day: list[dict]) -> str:
    """Inline SVG, one bar per day, scaled to the costliest day."""
    if not by_day:
        return ""
    peak = max(r["cost"] for r in by_day) or 1.0
    width, height, gap = 26, 90, 6
    bars = []
    for i, r in enumerate(by_day):
        h = max(2, round(r["cost"] / peak * (height - 20)))
        x = i * (width + gap)
        bars.append(
            f'<rect x="{x}" y="{height - h}" width="{width}" height="{h}" rx="3" '
            f'fill="#5b7fd4"><title>{escape(str(r["day"]))}: '
            f'${r["cost"]:.2f}</title></rect>'
        )
    total = len(by_day) * (width + gap)
    return (
        f'<svg viewBox="0 0 {total} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="cost per day">{"".join(bars)}</svg>'
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="n">{escape(c)}</td>' if i else f"<td>{escape(c)}</td>"
            for i, c in enumerate(r)
        )
        + "</tr>"
        for r in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def render(d: dict) -> str:
    t = d["totals"]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>gaia-butler — last {d['days']} days</title><style>{_CSS}</style>",
        "</head><body>",
        "<h1>gaia-butler</h1>",
        f"<p class='sub'>Last {d['days']} days</p>",
    ]

    if not t["calls"]:
        parts.append("<p>No model calls recorded in this window.</p>")
    else:
        rate = (
            f"{d['cache_hit_rate'] * 100:.0f}%"
            if d["cache_hit_rate"] is not None else "—"
        )
        parts += [
            "<div class='cards'>",
            f"<div class='card'><div class='label'>Cost</div>"
            f"<div class='big'>${t['cost']:.2f}</div></div>",
            f"<div class='card'><div class='label'>Model calls</div>"
            f"<div class='big'>{t['calls']:,}</div></div>",
            f"<div class='card'><div class='label'>Cache hit rate</div>"
            f"<div class='big'>{rate}</div></div>",
        ]
        if d["audio_minutes"]:
            parts.append(
                f"<div class='card'><div class='label'>Audio transcribed</div>"
                f"<div class='big'>{d['audio_minutes']:.0f}"
                f"<span style='font-size:16px'> min</span></div></div>"
            )
        parts += [
            "</div>",
            "<h2>Cost per day</h2>", _bars(d["by_day"]),
            "<h2>By job</h2>",
            _table(["Job", "Calls", "Cost"],
                   [[r["job"], f"{r['calls']:,}", f"${r['cost']:.2f}"] for r in d["by_job"]]),
            "<h2>By model</h2>",
            _table(["Model", "Calls", "Cost"],
                   [[r["model"], f"{r['calls']:,}", f"${r['cost']:.2f}"]
                    for r in d["by_model"]]),
            "<h2>By developer</h2>",
            _table(["Developer", "Calls", "Cost"],
                   [[r["name"], f"{r['calls']:,}", f"${r['cost']:.2f}"]
                    for r in d["by_user"]]),
            # Spec §5.2: the framing belongs where it will be read. At two
            # people this is curiosity; beyond a handful it can quietly become
            # a performance metric, and someone filing careful photo notes
            # every day costs several times someone who texts occasionally.
            # That is the product working, not waste.
            "<p class='note'>Per-developer cost tracks how much each person "
            "used the assistant, not how well they work.</p>",
        ]
        if d["calls_per_turn"]:
            parts += [
                "<h2>Calls per turn</h2>",
                _table(["Calls", "Turns"],
                       [[str(k), f"{v:,}"] for k, v in sorted(d["calls_per_turn"].items())]),
            ]
        if d["stop_reasons"]:
            parts += [
                "<h2>Stop reasons</h2>",
                _table(["Reason", "Calls"],
                       [[str(k or "none"), f"{v:,}"]
                        for k, v in sorted(d["stop_reasons"].items(),
                                           key=lambda kv: str(kv[0]))]),
            ]

    if d["unknown_models"]:
        parts.append(
            "<p class='note'>Tokens counted with no cost — no rate card for: "
            + escape(", ".join(d["unknown_models"])) + "</p>"
        )

    m, c, le = d["meetings"], d["commitments"], d["leads"]
    cpm = d["contacts_per_meeting"]
    parts += [
        "<h2>Product</h2>",
        _table(["", "Count", "Of which"], [
            ["Meetings filed", f"{m['total']:,}", f"{m['photo']} photo / {m['text']} text"],
            ["Contacts per meeting", f"{cpm['mean']:.1f}", f"{cpm['max']} at most"],
            ["Commitments", f"{c['total']:,}",
             f"{c['with_due_date']} dated / {c['done']} done"],
            ["Leads opened", f"{le['total']:,}",
             f"{le['with_next_action']} with next action"],
            ["Active developers", f"{d['users']['active']:,}",
             f"{d['users']['digested_today']} digested today"],
        ]),
        "</body></html>",
    ]
    return "".join(parts)
