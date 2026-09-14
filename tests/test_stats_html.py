from gaia.core import stats_html

DATA = {
    "days": 30,
    "totals": {"calls": 12, "input_tokens": 34000, "output_tokens": 2100, "cost": 1.2345},
    "by_job": [{"job": "turn", "calls": 10, "cost": 1.10, "input_tokens": 30000,
                "output_tokens": 2000, "cache_creation": 0, "cache_read": 0}],
    "by_model": [{"model": "claude-opus-5", "calls": 12, "cost": 1.2345,
                  "input_tokens": 34000, "output_tokens": 2100,
                  "cache_creation": 0, "cache_read": 0}],
    "by_user": [{"user_id": None, "name": "Ana", "calls": 12, "cost": 1.2345,
                 "input_tokens": 34000, "output_tokens": 2100,
                 "cache_creation": 0, "cache_read": 0}],
    "by_day": [{"day": "2026-09-12", "calls": 5, "cost": 0.5, "input_tokens": 1,
                "output_tokens": 1, "cache_creation": 0, "cache_read": 0},
               {"day": "2026-09-13", "calls": 7, "cost": 0.73, "input_tokens": 1,
                "output_tokens": 1, "cache_creation": 0, "cache_read": 0}],
    "cache_hit_rate": 0.42,
    "audio_minutes": 12.5,
    "stop_reasons": {"end_turn": 11, "max_tokens": 1},
    "unknown_models": [],
    "calls_per_turn": {1: 4, 2: 3, 8: 1},
    "meetings": {"total": 3, "photo": 2, "text": 1},
    "contacts_per_meeting": {"mean": 1.5, "max": 3},
    "meetings_by_day_and_user": [],
    "commitments": {"total": 8, "with_due_date": 3, "done": 7},
    "leads": {"total": 2, "with_next_action": 2},
    "users": {"active": 2, "digested_today": 2},
}


def test_the_page_carries_the_computed_figures():
    html = stats_html.render(DATA)

    assert "$1.23" in html
    assert "42%" in html
    assert "Ana" in html


def test_the_page_has_no_script_tag_and_no_external_asset():
    """It has to open from a file:// URL on a laptop and a phone, with no CDN
    and no JavaScript."""
    html = stats_html.render(DATA).lower()

    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_the_page_is_a_complete_document():
    html = stats_html.render(DATA)

    assert html.strip().startswith("<!doctype html>")
    assert "</html>" in html


def test_an_empty_window_still_renders():
    empty = {**DATA,
             "totals": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0},
             "by_job": [], "by_model": [], "by_user": [], "by_day": [],
             "cache_hit_rate": None, "stop_reasons": {}, "calls_per_turn": {},
             "audio_minutes": 0}

    html = stats_html.render(empty)

    assert "</html>" in html
    assert "no model calls" in html.lower()


def test_a_name_with_html_in_it_is_escaped():
    """Developer names come from the roster, which a human typed. The report
    is a file someone opens; it must not execute what the roster says."""
    data = {**DATA, "by_user": [{**DATA["by_user"][0], "name": "<script>x</script>"}]}

    html = stats_html.render(data)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_unknown_model_is_named_on_the_page():
    data = {**DATA, "unknown_models": ["claude-from-the-future"]}

    html = stats_html.render(data)

    assert "claude-from-the-future" in html
    assert "no rate card" in html.lower()


def test_the_per_developer_table_carries_its_framing():
    """Spec §5.2: at two people this is curiosity, and beyond a handful it can
    quietly become a performance metric. Someone filing careful photo notes
    daily costs several times someone who texts occasionally, and that is the
    product working. The caveat belongs where it will be read."""
    html = stats_html.render(DATA).lower()

    assert "not how well they work" in html
