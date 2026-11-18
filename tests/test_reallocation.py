"""Tests for critical spillover window detection, GSU reallocation, and chart rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from capacity.contract.common import SeriesKey
from capacity.engine.catalog import build_model_catalog
from capacity.engine.reallocation import (
    build_gsu_reallocation_plan,
    detect_critical_spillover_windows,
)
from capacity.engine.sizing import build_response
from capacity.showcase.evaluate import evaluate_showcase_scenario
from capacity.showcase.renderer import render_page_view
from capacity.showcase.view import build_showcase_page_view


def test_detect_critical_spillover_windows_identifies_peak_hours() -> None:
    cat = build_model_catalog()
    # gemini-3.8-flash cap = 120,900 TPM/GSU. Contracted = 2 GSUs -> 241,800 TPM ceiling.
    start = datetime(2026, 11, 15, 14, 0, tzinfo=UTC)
    skey = SeriesKey(model="gemini-3.8-flash", region="global")
    sparse = {
        skey: {
            start + timedelta(minutes=0): 300_000,
            start + timedelta(minutes=1): 483_600,  # 4 GSUs -> +2 excess
            start + timedelta(minutes=2): 200_000,  # under ceiling
        }
    }
    windows = detect_critical_spillover_windows(sparse, {skey: 2}, cat, top_n=5)
    assert len(windows) == 1
    w = windows[0]
    assert w.model_id == "gemini-3.8-flash"
    assert w.window_start_utc == "2026-11-15 14:00Z"
    assert w.window_end_utc == "15:00Z"
    assert w.duration_minutes == 2
    assert w.contracted_gsus == 2
    assert w.peak_gsus == 4
    assert w.excess_gsus == 2
    assert w.excess_tokens == (300_000 - 241_800) + (483_600 - 241_800)


def test_build_gsu_reallocation_plan_moves_1p_surplus_and_skips_zero_3p() -> None:
    cat = build_model_catalog()
    start = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    donor_key = SeriesKey(model="gemini-3.8-flash", region="global")
    deficit_key = SeriesKey(model="gemini-3.5-flash-lite", region="global")
    claude_key = SeriesKey(model="claude-opus-4-6", region="global")

    # donor: 10 contracted, ~1 GSU active -> 9 surplus
    # deficit: 2 contracted, 6 GSUs active -> 4 deficit
    # claude: 20 contracted, 0 telemetry -> must NOT donate to 1P
    sparse = {
        donor_key: {start + timedelta(minutes=i): 120_000 for i in range(60)},
        deficit_key: {start + timedelta(minutes=i): 241_800 * 6 for i in range(60)},
    }
    recs = build_response(
        project="test-proj",
        days=1,
        sparse_series=sparse,
        total_minutes=1440,
        percentiles=[50, 75, 90, 95],
        active_only=True,
        by_region=True,
        forecast_steps=0,
        catalog=cat,
    )
    contracted = {donor_key: 10, deficit_key: 2, claude_key: 20}
    plan = build_gsu_reallocation_plan(
        recs.models, contracted, target_percentile="P75", catalog=cat
    )
    assert plan.total_reallocated_gsus == 4
    assert plan.total_new_gsus_needed == 0
    assert len(plan.moves) == 1
    assert plan.moves[0].from_model == "gemini-3.8-flash"
    assert plan.moves[0].to_model == "gemini-3.5-flash-lite"
    assert plan.moves[0].gsus_moved == 4


def test_showcase_html_renders_reallocation_and_spillover_charts() -> None:
    report = evaluate_showcase_scenario(None)
    assert report.reallocation_p50 is not None
    assert report.reallocation_p75 is not None
    assert report.reallocation_p95 is not None
    html = render_page_view(build_showcase_page_view(report, static_export=True))
    assert "Capacity vs. Active P50 / P75 / P95 Demand" in html
    assert "Recommended GSU Reallocations (P75 Target)" in html
    assert "chart-bar-fill" in html
