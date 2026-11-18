"""Typed, display-ready `PageView` objects built from `ShowcaseReport` or `RecommendationResponse`.

Carries every card, table, label, badge, banner, and control default as unescaped plain text so
`capacity/showcase/renderer.py` turns it into HTML and escapes every slot in one place (`P3.1`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from capacity.contract.common import PACKAGE_VERSION
from capacity.contract.results import PercentileDetail, RecommendationResponse
from capacity.contract.scenario import ModelInventoryAnnotation, ShowcaseReport

BadgeTone = Literal["blue", "green", "warn", "danger"]
BannerTone = Literal["info", "warn", "danger"]


@dataclass(frozen=True)
class BadgeView:
    text: str
    tone: BadgeTone = "blue"
    element_id: str | None = None


@dataclass(frozen=True)
class LinkView:
    href: str
    label: str


@dataclass(frozen=True)
class KpiCardView:
    label: str
    value: str
    badge: BadgeView
    caption: str
    value_id: str | None = None
    caption_id: str | None = None


@dataclass(frozen=True)
class CellView:
    text: str = ""
    code: str | None = None
    badge: BadgeView | None = None
    bold_suffix: str | None = None
    note: str | None = None
    numeric: bool = False
    strong: bool = False
    tone: Literal["default", "success"] = "default"
    colspan: str | None = None


@dataclass(frozen=True)
class TableHeaderView:
    label: str
    numeric: bool = False


@dataclass(frozen=True)
class TableView:
    headers: tuple[TableHeaderView, ...]
    rows: tuple[tuple[CellView, ...], ...]


@dataclass(frozen=True)
class ControlOptionView:
    value: str
    label: str
    selected: bool = False


@dataclass(frozen=True)
class ControlFieldView:
    field_id: str
    label: str
    kind: Literal["number", "select"] = "number"
    value: str = ""
    min_val: str | None = None
    max_val: str | None = None
    step: str | None = None
    handler: str | None = None
    options: tuple[ControlOptionView, ...] = ()
    disabled: bool = False


@dataclass(frozen=True)
class BannerView:
    text: str
    tone: BannerTone = "info"
    element_id: str | None = None


@dataclass(frozen=True)
class CardView:
    title: str
    subtitle: str | None = None
    controls: tuple[ControlFieldView, ...] = ()
    banner: BannerView | None = None
    table: TableView | None = None
    code_block: str | None = None


@dataclass(frozen=True)
class CardRowView:
    cards: tuple[CardView, ...]


@dataclass(frozen=True)
class TabView:
    panel_id: str
    button_label: str
    kpis: tuple[KpiCardView, ...]
    rows: tuple[CardRowView, ...]
    active: bool = False


@dataclass(frozen=True)
class PageView:
    page_title: str
    brand_heading: str
    brand_subtitle: str
    badges: tuple[BadgeView, ...]
    docs_link: LinkView | None = None
    tabs: tuple[TabView, ...] = ()
    standalone_kpis: tuple[KpiCardView, ...] = ()
    standalone_rows: tuple[CardRowView, ...] = ()
    include_script: bool = False
    live_api: bool = False
    api_context: tuple[tuple[str, str], ...] = ()


def compute_mean_duty_pct(recs: RecommendationResponse) -> float:
    if not recs.models:
        return 0.0
    return sum(m.duty_cycle_pct for m in recs.models) / len(recs.models)


def _lifecycle_badge(track: str | None) -> BadgeView:
    if track == "long_term_ga":
        return BadgeView("Long-Term GA (12m+ SLA)", "green")
    if track == "short_term_ga":
        return BadgeView("Short-Term GA (Eval/Pilot)", "warn")
    if track == "preview":
        return BadgeView("Preview (Non-Prod)", "warn")
    if track == "legacy_sunset":
        return BadgeView("Legacy / Migrate", "danger")
    return BadgeView("Standard GA", "blue")


def _renewal_badge(ann: ModelInventoryAnnotation | None) -> BadgeView:
    if ann is None or ann.renewal_mode in (None, "none"):
        return BadgeView("PayGo / Unreserved", "blue")
    if ann.renewal_mode == "fixed_term":
        exp = ann.expiry_date or "fixed"
        return BadgeView(f"Fixed \u2192 {exp}", "danger")
    if ann.renewal_mode == "mixed":
        exp = ann.expiry_date or "term"
        fixed = ann.expiring_fixed_gsus or 0
        return BadgeView(f"{fixed} Fixed ({exp}) + Auto", "warn")
    return BadgeView("Auto-Renew (1m)", "green")


def _lookup_annotation(
    annotations: dict[str, ModelInventoryAnnotation],
    model: str,
    normalized_model: str,
    region: str | None,
) -> ModelInventoryAnnotation | None:
    loc = region or "global"
    for candidate in (f"{model}@{loc}", f"{normalized_model}@{loc}", model):
        if candidate in annotations:
            return annotations[candidate]
    return None


def _pct_cell_text(
    pct_map: dict[str, PercentileDetail],
    key: str,
    unit: str,
    *,
    known_capacity: bool,
) -> str:
    entry = pct_map.get(key)
    if entry is None:
        return "n/a"
    gsu_txt = str(entry.recommended_gsus) if known_capacity else "?"
    return f"{entry.tpm:,} ({gsu_txt} {unit})"


def build_sizing_table_view(
    recs: RecommendationResponse,
    annotations: dict[str, ModelInventoryAnnotation],
) -> TableView:
    unit_lbl = "Rep" if recs.mode == "self-hosted" else "GSU"
    cap_header = "1 Replica Cap (TPM)" if recs.mode == "self-hosted" else "1 GSU Cap (TPM)"
    has_inventory_cols = bool(annotations)
    pct_keys = [f"P{p}" for p in recs.requested_percentiles]

    headers: list[TableHeaderView] = [
        TableHeaderView("Model"),
        TableHeaderView("Region"),
        TableHeaderView(cap_header, numeric=True),
        TableHeaderView("Duty Cycle", numeric=True),
    ]
    if has_inventory_cols:
        headers.extend(
            (
                TableHeaderView("Contracted GSUs", numeric=True),
                TableHeaderView("Spillover / 429s", numeric=True),
                TableHeaderView("GA Lifecycle Track"),
                TableHeaderView("Term & Renewal"),
            )
        )
    elif "P50" in pct_keys:
        headers.append(TableHeaderView("24x7 P50 (Zero-Padded)", numeric=True))

    headers.extend(TableHeaderView(f"Active {k}", numeric=True) for k in pct_keys)
    headers.append(TableHeaderView(f"Holt Forecast +{recs.forecast_hours}h Peak", numeric=True))

    rows: list[tuple[CellView, ...]] = []
    for m in recs.models:
        known_cap = m.gsu_capacity_tpm is not None and m.gsu_capacity_tpm > 0
        cap_str = f"{m.gsu_capacity_tpm:,}" if known_cap else "?"
        ann = _lookup_annotation(annotations, m.model, m.normalized_model, m.region)

        row_cells: list[CellView] = [
            CellView(code=m.model),
            CellView(text=m.region or "global"),
            CellView(text=cap_str, numeric=True),
            CellView(text=f"{m.duty_cycle_pct:.1f}%", numeric=True),
        ]
        if has_inventory_cols:
            c_gsus = (
                f"{ann.contracted_gsus:,} GSU"
                if ann and ann.contracted_gsus is not None
                else "0 GSU"
            )
            spill_s = f"{(ann.spillover_requests or 0):,}" if ann else "0"
            err_s = f"{(ann.http_429_count or 0):,}" if ann else "0"
            row_cells.extend(
                (
                    CellView(
                        text=c_gsus,
                        note=ann.scope_label if ann and ann.scope_label else None,
                        numeric=True,
                    ),
                    CellView(text=f"{spill_s} / ", bold_suffix=err_s, numeric=True),
                    CellView(badge=_lifecycle_badge(ann.lifecycle_track if ann else None)),
                    CellView(badge=_renewal_badge(ann)),
                )
            )
        elif "P50" in pct_keys:
            row_cells.append(
                CellView(
                    text=_pct_cell_text(m.all_hours, "P50", unit_lbl, known_capacity=known_cap),
                    numeric=True,
                )
            )

        for idx, k in enumerate(pct_keys):
            row_cells.append(
                CellView(
                    text=_pct_cell_text(m.active_hours, k, unit_lbl, known_capacity=known_cap),
                    numeric=True,
                    strong=(idx == len(pct_keys) - 1),
                )
            )
        f_gsus = str(m.forecast_24h_recommended_gsus) if known_cap else "?"
        row_cells.append(
            CellView(text=f"{m.forecast_24h_peak_tpm:,} ({f_gsus} {unit_lbl})", numeric=True)
        )
        rows.append(tuple(row_cells))

    return TableView(headers=tuple(headers), rows=tuple(rows))


def build_recommendations_page_view(
    recs: RecommendationResponse,
    *,
    static_export: bool = True,
) -> PageView:
    """Build the sizing-only `PageView` for `recommend --html-out` (zero sample tabs)."""
    mean_duty = compute_mean_duty_pct(recs)
    mode_lbl = "Self-Hosted GPU Replica" if recs.mode == "self-hosted" else "Managed GSU"
    badges: list[BadgeView] = [BadgeView(f"v{PACKAGE_VERSION} Sizing Report", "blue")]
    docs_link: LinkView | None = None
    if static_export:
        badges.append(BadgeView("Offline Static Scorecard", "blue"))
    else:
        docs_link = LinkView("/docs", "OpenAPI /docs \u2192")

    kpis = (
        KpiCardView(
            label="Project / Scope",
            value=recs.project,
            badge=BadgeView(f"{recs.days}d Window", "blue"),
            caption=f"{mode_lbl} sizing ({len(recs.models)} series)",
        ),
        KpiCardView(
            label="Active vs 24x7 Duty Cycle",
            value=f"{mean_duty:.1f}%",
            badge=BadgeView("Active Business Hours" if recs.active_only else "24x7 Window", "blue"),
            caption="Computed directly from observed non-zero minutes",
        ),
        KpiCardView(
            label="Forecast Horizon",
            value=f"+{recs.forecast_hours}h",
            badge=BadgeView("Daily-Peak Holt Trend", "green"),
            caption="Percentiles: " + ", ".join(f"P{p}" for p in recs.requested_percentiles),
        ),
    )
    sizing_card = CardView(
        title=f"{mode_lbl} Sizing & +{recs.forecast_hours}h Holt Forecast ({recs.project})",
        subtitle="Computed solely from the supplied trace/telemetry series with zero sample tabs.",
        table=build_sizing_table_view(recs, {}),
    )
    return PageView(
        page_title=f"Capacity v{PACKAGE_VERSION} — {recs.project} {mode_lbl} Sizing Scorecard",
        brand_heading=f"Capacity {mode_lbl} Sizing ({recs.project})",
        brand_subtitle=(
            f"{recs.days}-day empirical trace \u2022 +{recs.forecast_hours}h Holt forecast"
        ),
        badges=tuple(badges),
        docs_link=docs_link,
        standalone_kpis=kpis,
        standalone_rows=(CardRowView((sizing_card,)),),
        include_script=False,
        live_api=False,
    )


def build_showcase_page_view(
    report: ShowcaseReport,
    *,
    static_export: bool = False,
) -> PageView:
    """Build the 4-Tab GM3 Light Workbench `PageView` solely from `ShowcaseReport`."""
    from capacity.showcase.governance_tabs import build_finops_tab, build_gateway_tab
    from capacity.showcase.tabs import build_burst_tab, build_sizing_tab

    scen = report.scenario
    pres = scen.presentation
    default_page_title = (
        f"Capacity v{PACKAGE_VERSION} — AI Capacity, Micro-Burst, "
        "Multi-Gateway & FOCUS 1.2 Showcase"
    )
    page_title = pres.title if report.scenario_mode else default_page_title

    badges: list[BadgeView] = [
        BadgeView(pres.primary_badge, "blue"),
        BadgeView(pres.secondary_badge, "green"),
    ]
    docs_link: LinkView | None = None
    if static_export:
        badges.append(BadgeView("Offline Static Scorecard", "blue"))
    else:
        docs_link = LinkView("/docs", "OpenAPI /docs \u2192")

    disagg_req = scen.self_hosted.disaggregated
    flash_req = scen.burst_and_cache.flash_tier_audit
    gw_cfg = scen.gateway.config
    api_context: tuple[tuple[str, str], ...] = (
        ("data-disagg-profile", disagg_req.profile),
        ("data-disagg-tpot-ms", f"{disagg_req.target_tpot_ms:g}"),
        ("data-cache-out-tokens", str(flash_req.output_tokens)),
        ("data-cache-thoughts-tokens", str(flash_req.thoughts_tokens)),
        ("data-cache-replicas", str(flash_req.num_replicas)),
        ("data-gw-model", gw_cfg.model),
        ("data-gw-project", gw_cfg.project_id),
        ("data-gw-location", gw_cfg.location),
        ("data-gw-gsus", str(gw_cfg.provisioned_gsus)),
        ("data-gw-self-hosted-model", gw_cfg.self_hosted_model),
        ("data-gw-bu-shares", str(gw_cfg.business_unit_shares)),
    )

    return PageView(
        page_title=page_title,
        brand_heading=pres.title,
        brand_subtitle=pres.subtitle,
        badges=tuple(badges),
        docs_link=docs_link,
        tabs=(
            build_sizing_tab(report, static_export=static_export),
            build_burst_tab(report, static_export=static_export),
            build_gateway_tab(report, static_export=static_export),
            build_finops_tab(report),
        ),
        include_script=True,
        live_api=not static_export,
        api_context=api_context,
    )
