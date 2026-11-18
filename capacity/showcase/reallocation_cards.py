"""Visual chart and table builders for critical spillover windows and GSU reallocation."""

from __future__ import annotations

from capacity.contract.reallocation import GsuReallocationPlan, SpilloverWindow
from capacity.contract.scenario import ShowcaseReport
from capacity.showcase.view import (
    BadgeTone,
    BadgeView,
    BannerView,
    BarGroupView,
    BarSegmentView,
    CardRowView,
    CardView,
    CellView,
    ChartLegendView,
    GroupedBarChartView,
    TableHeaderView,
    TableView,
)


def _pct_str(val: int, max_val: int) -> str:
    if max_val <= 0 or val <= 0:
        return "0.0"
    pct = min(100.0, max(2.0, (val / max_val) * 100.0))
    return f"{pct:.1f}"


def _build_capacity_vs_percentile_chart(plan: GsuReallocationPlan) -> GroupedBarChartView:
    candidates = [
        g
        for g in plan.pool_gaps
        if g.active_p95_gsus > 0 or (g.provider == "1p_gemini" and g.contracted_gsus > 0)
    ]
    candidates.sort(
        key=lambda g: max(g.contracted_gsus, g.active_p95_gsus),
        reverse=True,
    )
    top = candidates[:8]
    max_val = max(
        (
            max(g.contracted_gsus, g.active_p95_gsus, g.active_p75_gsus, g.active_p50_gsus)
            for g in top
        ),
        default=1,
    )

    groups: list[BarGroupView] = []
    for g in top:
        groups.append(
            BarGroupView(
                label=f"{g.model_id} ({g.region})",
                subtitle=(
                    f"Contracted {g.contracted_gsus} • P50 {g.active_p50_gsus} • "
                    f"P75 {g.active_p75_gsus} • P95 {g.active_p95_gsus}"
                ),
                bars=(
                    BarSegmentView(
                        "Contracted",
                        _pct_str(g.contracted_gsus, max_val),
                        f"{g.contracted_gsus}",
                        "blue",
                    ),
                    BarSegmentView(
                        "Active P50",
                        _pct_str(g.active_p50_gsus, max_val),
                        f"{g.active_p50_gsus}",
                        "green",
                    ),
                    BarSegmentView(
                        "Active P75",
                        _pct_str(g.active_p75_gsus, max_val),
                        f"{g.active_p75_gsus}",
                        "warn",
                    ),
                    BarSegmentView(
                        "Active P95",
                        _pct_str(g.active_p95_gsus, max_val),
                        f"{g.active_p95_gsus}",
                        "danger",
                    ),
                ),
            )
        )

    return GroupedBarChartView(
        title="Contracted PT vs. Active P50 / P75 / P95 GSUs (1P & Active Models)",
        legend=(
            ChartLegendView("Contracted PT", "blue"),
            ChartLegendView("Active P50", "green"),
            ChartLegendView("Active P75", "warn"),
            ChartLegendView("Active P95", "danger"),
        ),
        groups=tuple(groups),
        caption="Highlights over-provisioned 1P donors (Contracted > P75/P95) vs. deficit models.",
    )


def _build_spillover_chart(windows: list[SpilloverWindow]) -> GroupedBarChartView:
    top = windows[:8]
    max_val = max((max(w.peak_gsus, w.contracted_gsus) for w in top), default=1)
    groups: list[BarGroupView] = []
    for w in top:
        groups.append(
            BarGroupView(
                label=f"{w.model_id} • {w.window_start_utc}",
                subtitle=f"{w.duration_minutes}m over ceiling • +{w.excess_gsus:,} excess GSUs",
                bars=(
                    BarSegmentView(
                        "Contracted",
                        _pct_str(w.contracted_gsus, max_val),
                        f"{w.contracted_gsus}",
                        "blue",
                    ),
                    BarSegmentView(
                        "Peak Demand", _pct_str(w.peak_gsus, max_val), f"{w.peak_gsus}", "danger"
                    ),
                ),
            )
        )
    return GroupedBarChartView(
        title="Top Critical Spillover Windows (Peak vs. Contracted GSUs)",
        legend=(
            ChartLegendView("Contracted PT Ceiling", "blue"),
            ChartLegendView("Peak Window Demand", "danger"),
        ),
        groups=tuple(groups),
        caption="Hourly UTC windows sorted by excess spillover tokens above contracted PT.",
    )


def _action_badge(action: str) -> BadgeView:
    mapping: dict[str, tuple[str, BadgeTone]] = {
        "donor_right_size": ("Donor (Reallocate Out)", "blue"),
        "reallocate_in": ("Covered via Reallocation", "green"),
        "buy_new": ("Reallocate + Buy New", "danger"),
        "balanced": ("Balanced", "green"),
    }
    text, tone = mapping.get(action, (action, "blue"))
    return BadgeView(text, tone)


def build_reallocation_and_spillover_rows(report: ShowcaseReport) -> list[CardRowView]:
    """Build visual chart, spillover timeline, and P50/P75/P95 reallocation rows for Tab 1."""
    r50 = report.reallocation_p50
    r75 = report.reallocation_p75
    r95 = report.reallocation_p95
    windows = report.critical_spillover_windows
    rows: list[CardRowView] = []

    # Row A: Dual visual bar charts (Capacity vs Percentiles + Critical Spillover Windows)
    chart_cards: list[CardView] = []
    if r75 is not None and r75.pool_gaps:
        chart_cards.append(
            CardView(
                title="Capacity vs. Active P50 / P75 / P95 Demand",
                subtitle=(
                    "Visualizes donor headroom vs. under-provisioned pools across active models."
                ),
                chart=_build_capacity_vs_percentile_chart(r75),
            )
        )
    if windows:
        chart_cards.append(
            CardView(
                title="Critical Spillover Windows (When Demand Exceeded PT)",
                subtitle=(
                    "Top hourly UTC windows where traffic spiked above contracted GSU ceilings."
                ),
                chart=_build_spillover_chart(windows),
            )
        )
    if chart_cards:
        rows.append(CardRowView(tuple(chart_cards)))

    # Row B: Critical Spillover Windows Table
    if windows:
        win_rows = tuple(
            (
                CellView(text=f"{w.window_start_utc} - {w.window_end_utc}", strong=True),
                CellView(code=w.model_id),
                CellView(text=w.region),
                CellView(text=f"{w.duration_minutes}m", numeric=True),
                CellView(text=f"{w.contracted_gsus:,}", numeric=True),
                CellView(text=f"{w.peak_gsus:,}", numeric=True, strong=True),
                CellView(
                    text=f"+{w.excess_gsus:,}",
                    numeric=True,
                    badge=BadgeView(f"+{w.excess_gsus:,} GSU", "danger"),
                ),
                CellView(text=f"{w.excess_tokens:,}", numeric=True),
            )
            for w in windows
        )
        rows.append(
            CardRowView(
                (
                    CardView(
                        title="Most Critical Spillover Times (Exact UTC Windows & Excess Volume)",
                        subtitle=(
                            "Sorted by excess tokens above contracted PT ceiling; "
                            "drives priority spillover & renewal sizing."
                        ),
                        table=TableView(
                            headers=(
                                TableHeaderView("Critical Window (UTC)"),
                                TableHeaderView("Model"),
                                TableHeaderView("Region"),
                                TableHeaderView("Minutes Over PT", numeric=True),
                                TableHeaderView("Contracted GSUs", numeric=True),
                                TableHeaderView("Peak GSUs", numeric=True),
                                TableHeaderView("Excess GSUs", numeric=True),
                                TableHeaderView("Excess Tokens", numeric=True),
                            ),
                            rows=win_rows,
                        ),
                    ),
                )
            )
        )

    # Row C: GSU Reallocation Moves + Net-New GSUs Needed (P50 / P75 / P95)
    if r50 is not None and r75 is not None and r95 is not None:
        summary_banner = BannerView(
            text=(
                "Intra-Provider 1P Reallocation Summary — "
                f"Cover P50: move {r50.total_reallocated_gsus:,} GSUs, "
                f"buy {r50.total_new_gsus_needed:,} new | "
                f"Cover P75: move {r75.total_reallocated_gsus:,} GSUs, "
                f"buy {r75.total_new_gsus_needed:,} new | "
                f"Cover P95: move {r95.total_reallocated_gsus:,} GSUs, "
                f"buy {r95.total_new_gsus_needed:,} new"
            ),
            tone="info",
        )
        move_rows: tuple[tuple[CellView, ...], ...] = tuple(
            (
                CellView(code=f"{m.from_model} ({m.from_region})"),
                CellView(code=f"{m.to_model} ({m.to_region})"),
                CellView(text=f"{m.gsus_moved:,} GSUs", numeric=True, strong=True),
                CellView(text=m.rationale),
            )
            for m in r75.moves
        )
        if not move_rows:
            move_rows = ((CellView(text="No surplus donor GSUs available at P75", colspan="4"),),)
        moves_card = CardView(
            title="Recommended GSU Reallocations (P75 Target)",
            subtitle=(
                "Moves surplus GSUs from over-provisioned 1P pools before buying net-new capacity."
            ),
            banner=summary_banner,
            table=TableView(
                headers=(
                    TableHeaderView("From Donor Pool"),
                    TableHeaderView("To Deficit Pool"),
                    TableHeaderView("GSUs Moved", numeric=True),
                    TableHeaderView("Rationale"),
                ),
                rows=move_rows,
            ),
        )

        r95_by_key = {(g.model_id, g.region): g for g in r95.pool_gaps}
        gap_rows = tuple(
            (
                CellView(code=g.model_id),
                CellView(text=g.region),
                CellView(text=f"{g.contracted_gsus:,}", numeric=True),
                CellView(
                    text=f"{g.active_p50_gsus:,} / {g.active_p75_gsus:,} / {g.active_p95_gsus:,}",
                    numeric=True,
                ),
                CellView(
                    text=(
                        f"{g.reallocated_delta_gsus:+,}" if g.reallocated_delta_gsus != 0 else "0"
                    ),
                    numeric=True,
                    strong=g.reallocated_delta_gsus != 0,
                ),
                CellView(
                    text=(
                        f"{g.new_gsus_needed:,} / "
                        f"{r95_by_key[(g.model_id, g.region)].new_gsus_needed:,}"
                        if (g.model_id, g.region) in r95_by_key
                        else f"{g.new_gsus_needed:,} / {g.new_gsus_needed:,}"
                    ),
                    numeric=True,
                    strong=True,
                ),
                CellView(badge=_action_badge(g.action)),
            )
            for g in r75.pool_gaps
        )
        gaps_card = CardView(
            title="Per-Model Reallocation & Net-New GSU Balance (P50 / P75 / P95)",
            subtitle=(
                "Shows contracted PT vs. active P50/P75/P95 demand, "
                "P75 reallocation delta, and net-new GSUs needed."
            ),
            table=TableView(
                headers=(
                    TableHeaderView("Model"),
                    TableHeaderView("Region"),
                    TableHeaderView("Contracted", numeric=True),
                    TableHeaderView("Active P50 / P75 / P95", numeric=True),
                    TableHeaderView("P75 Realloc Δ", numeric=True),
                    TableHeaderView("New GSUs (P75 / P95)", numeric=True),
                    TableHeaderView("Recommended Action"),
                ),
                rows=gap_rows,
            ),
        )
        rows.append(CardRowView((moves_card, gaps_card)))

    return rows
