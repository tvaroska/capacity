"""The shareable HTML scorecard: telemetry text is data, never markup."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from capacity.capacity import build_response
from capacity.portal import render_showcase_portal_html


def _page_for(model_name: str, percentiles: tuple[int, ...] = (50, 75, 90, 95)) -> str:
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    series = {start + timedelta(minutes=i): 241_800 for i in range(5)}
    recs = build_response("demo-project", 1, {model_name: series}, 1_440, list(percentiles))
    return render_showcase_portal_html(static_export=True, custom_recommendations=recs)


CLEAN_PAGE = _page_for("gemini-3.8-flash")


def test_model_name_cannot_add_a_script_element() -> None:
    page = _page_for("a</script><script>alert(1)</script>")
    assert "<script>alert(1)" not in page
    assert page.count("<script") == CLEAN_PAGE.count("<script")
    assert page.count("</script") == CLEAN_PAGE.count("</script")


def test_model_name_is_escaped_in_the_table() -> None:
    payload = "<img src=x onerror=alert(1)>"
    page = _page_for(payload)
    assert payload not in page
    assert f"<code>{html.escape(payload)}</code>" in page


def test_placeholder_text_in_data_is_not_expanded() -> None:
    page = _page_for("__HEADER_ROWS__")
    assert "<code>__HEADER_ROWS__</code>" in page
    tier_8 = "Pure Off-Peak / Batch PayGo (Projects Without PT)"
    assert page.count(tier_8) == CLEAN_PAGE.count(tier_8) == 1


def test_page_carries_no_unread_data_blob_and_no_leftover_placeholder() -> None:
    assert "CAPACITY_SHOWCASE_DATA" not in CLEAN_PAGE
    assert re.findall(r"__[A-Z][A-Z_]*__", CLEAN_PAGE) == []


def test_percentile_that_was_not_requested_shows_no_value() -> None:
    page = _page_for("gemini-3.8-flash", percentiles=(99,))
    row = page[page.index("<code>gemini-3.8-flash</code>") :]
    row = row[: row.index("</tr>")]
    assert row.count(">n/a<") == 4
