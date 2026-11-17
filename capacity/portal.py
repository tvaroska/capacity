"""Pure HTML renderer for the GM3 Light Web Workbench (`portal.py`).

Takes a pre-evaluated `ShowcaseReport` or `RecommendationResponse` and calls zero calculations.
"""

from __future__ import annotations

import html
import re

from capacity.model_families import build_model_catalog, normalize_model_id
from capacity.models import (
    PACKAGE_VERSION,
    ModelTierPrefixCacheAudit,
    ModelTierPrefixCacheRequest,
    PercentileDetail,
    RecommendationResponse,
)
from capacity.portal_sections import (
    compute_mean_duty_pct,
    lifecycle_badge,
    lookup_annotation,
    render_finops_pool_cards,
    render_gateway_top_section,
    render_sizing_kpis,
    renewal_cell,
)
from capacity.portal_template import (
    FAVICON_DATA_URI,
    PORTAL_TEMPLATE,
    RECOMMENDATIONS_ONLY_TEMPLATE,
)
from capacity.scenario import (
    ModelInventoryAnnotation,
    ShowcaseReport,
    ShowcaseScenario,
    evaluate_showcase_scenario,
)


def _pct_cell(
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


def _build_sizing_table_html(
    recs: RecommendationResponse,
    annotations: dict[str, ModelInventoryAnnotation],
) -> tuple[str, str]:
    unit_lbl = "Rep" if recs.mode == "self-hosted" else "GSU"
    cap_header = "1 Replica Cap (TPM)" if recs.mode == "self-hosted" else "1 GSU Cap (TPM)"
    has_inventory_cols = bool(annotations)
    pct_keys = [f"P{p}" for p in recs.requested_percentiles]

    headers = (
        f"<th>Model</th><th>Region</th><th class='num'>{cap_header}</th>"
        "<th class='num'>Duty Cycle</th>"
    )
    if has_inventory_cols:
        headers += (
            "<th class='num'>Contracted GSUs</th><th class='num'>Spillover / 429s</th>"
            "<th>GA Lifecycle Track</th><th>Term &amp; Renewal</th>"
        )
    elif "P50" in pct_keys:
        headers += "<th class='num'>24x7 P50 (Zero-Padded)</th>"

    for k in pct_keys:
        headers += f"<th class='num'>Active {k}</th>"
    headers += f"<th class='num'>Holt Forecast +{recs.forecast_hours}h Peak</th>"

    rows_html = ""
    for m in recs.models:
        known_cap = m.gsu_capacity_tpm is not None and m.gsu_capacity_tpm > 0
        cap_str = f"{m.gsu_capacity_tpm:,}" if known_cap else "?"
        ann = lookup_annotation(annotations, m.model, m.region)
        if has_inventory_cols:
            c_gsus = (
                f"{ann.contracted_gsus:,} GSU"
                if ann and ann.contracted_gsus is not None
                else "0 GSU"
            )
            if ann and ann.scope_label:
                c_gsus += f" <small>({html.escape(ann.scope_label)})</small>"
            spill_s = f"{(ann.spillover_requests or 0):,}" if ann else "0"
            err_s = f"{(ann.http_429_count or 0):,}" if ann else "0"
            inv_cells = (
                f"<td class='num'>{c_gsus}</td>"
                f"<td class='num'>{spill_s} / <strong>{err_s}</strong></td>"
                f"<td>{lifecycle_badge(ann.lifecycle_track if ann else None)}</td>"
                f"<td>{renewal_cell(ann)}</td>"
            )
        elif "P50" in pct_keys:
            p50_txt = _pct_cell(m.all_hours, "P50", unit_lbl, known_capacity=known_cap)
            inv_cells = f"<td class='num'>{p50_txt}</td>"
        else:
            inv_cells = ""

        act_cells = "".join(
            f"<td class='num'>"
            f"{'<strong>' if idx == len(pct_keys) - 1 else ''}"
            f"{_pct_cell(m.active_hours, k, unit_lbl, known_capacity=known_cap)}"
            f"{'</strong>' if idx == len(pct_keys) - 1 else ''}</td>"
            for idx, k in enumerate(pct_keys)
        )
        f_gsus = str(m.forecast_24h_recommended_gsus) if known_cap else "?"
        rows_html += (
            f"<tr><td><code>{html.escape(m.model)}</code></td>"
            f"<td>{html.escape(m.region or 'global')}</td>"
            f"<td class='num'>{cap_str}</td><td class='num'>{m.duty_cycle_pct:.1f}%</td>"
            f"{inv_cells}{act_cells}"
            f"<td class='num'>{m.forecast_24h_peak_tpm:,} ({f_gsus} {unit_lbl})</td></tr>"
        )
    return headers, rows_html


def _flash_cache_presentation(
    audit: ModelTierPrefixCacheAudit,
    req: ModelTierPrefixCacheRequest,
) -> tuple[str, str, str, str, str]:
    caption = (
        f"{html.escape(audit.normalized_model)} uses {audit.block_size_tokens:,}-token blocks "
        f"({audit.min_prefix_floor_tokens:,} floor)"
        if audit.checkpoint_granularity == "block_4096"
        else f"{html.escape(audit.normalized_model)} uses token-boundary checkpointing "
        f"({audit.min_prefix_floor_tokens:,} floor)"
    )
    first_step = html.escape(audit.remediation_steps[0]) if audit.remediation_steps else ""
    if audit.block_zero_contaminated:
        offset = req.dynamic_header_offset_tokens or 0
        return (
            "badge-danger",
            f"Dynamic Token @ Offset {offset:,} (&lt;{audit.block_size_tokens:,})",
            caption,
            "danger-banner",
            f"Block 0 Contamination Active: {first_step} "
            f"(cuts weighted burndown waste by {audit.burndown_waste_pct:.1f}%).",
        )
    if audit.below_minimum_floor:
        return (
            "badge-warn",
            f"Below Cache Floor (&lt;{audit.min_prefix_floor_tokens:,} tok)",
            caption,
            "warn-banner",
            first_step,
        )
    if audit.strip_thought_signatures:
        return (
            "badge-warn",
            "thought_signature Stripped",
            caption,
            "warn-banner",
            first_step,
        )
    if audit.single_turn_request and not audit.dummy_turn_anchor_applied:
        return (
            "badge-warn",
            "Single-Turn 1/N Hash",
            caption,
            "warn-banner",
            first_step,
        )
    hit_pct = audit.effective_cache_hit_rate * 100.0
    return (
        "badge-green",
        f"Prefix Cache Healthy ({hit_pct:.1f}% Hit)",
        caption,
        "info-banner",
        first_step,
    )


def render_recommendations_html(
    recs: RecommendationResponse,
    *,
    static_export: bool = True,
) -> str:
    """Render the sizing-only HTML scorecard for `recommend --html-out` (zero sample data)."""
    headers, rows_html = _build_sizing_table_html(recs, {})
    mean_duty = compute_mean_duty_pct(recs)
    mode_lbl = "Self-Hosted GPU Replica" if recs.mode == "self-hosted" else "Managed GSU"
    proj_lbl = html.escape(recs.project)
    kpis_html = (
        f"<div class='kpi-grid'>"
        f"<div class='kpi-card'><div class='kpi-label'>Project / Scope</div>"
        f"<div class='kpi-value'>{proj_lbl}</div>"
        f"<span class='badge badge-blue'>{recs.days}d Window</span>"
        f"<div class='kpi-caption'>{mode_lbl} sizing ({len(recs.models)} series)</div></div>"
        f"<div class='kpi-card'><div class='kpi-label'>Active vs 24x7 Duty Cycle</div>"
        f"<div class='kpi-value'>{mean_duty:.1f}%</div>"
        f"<span class='badge badge-blue'>"
        f"{'Active Business Hours' if recs.active_only else '24x7 Window'}</span>"
        f"<div class='kpi-caption'>Computed directly from observed non-zero minutes</div></div>"
        f"<div class='kpi-card'><div class='kpi-label'>Forecast Horizon</div>"
        f"<div class='kpi-value'>+{recs.forecast_hours}h</div>"
        f"<span class='badge badge-green'>Daily-Peak Holt Trend</span>"
        f"<div class='kpi-caption'>Percentiles: "
        f"{', '.join(f'P{p}' for p in recs.requested_percentiles)}</div></div>"
        f"</div>"
    )
    docs_header_right = (
        '<span class="badge badge-blue">Offline Static Scorecard</span>'
        if static_export
        else '<a href="/docs" class="docs-link">OpenAPI /docs &rarr;</a>'
    )
    replacements = {
        "__PAGE_TITLE__": f"Capacity v{PACKAGE_VERSION} — {proj_lbl} {mode_lbl} Sizing Scorecard",
        "__FAVICON_URI__": FAVICON_DATA_URI,
        "__BRAND_HEADING__": f"Capacity {mode_lbl} Sizing ({proj_lbl})",
        "__BRAND_SUBTITLE__": (
            f"{recs.days}-day empirical trace &bull; +{recs.forecast_hours}h Holt forecast"
        ),
        "__BADGE_PRIMARY__": f"v{PACKAGE_VERSION} Sizing Report",
        "__DOCS_HEADER_RIGHT__": docs_header_right,
        "__SIZING_KPIS__": kpis_html,
        "__SIZING_CARD_TITLE__": (
            f"{mode_lbl} Sizing &amp; +{recs.forecast_hours}h Holt Forecast ({proj_lbl})"
        ),
        "__SIZING_CARD_SUB__": (
            "Computed solely from the supplied trace/telemetry series with zero sample tabs."
        ),
        "__GSU_TABLE_HEADERS__": headers,
        "__GSU_ROWS__": rows_html,
    }
    placeholder = re.compile("|".join(re.escape(key) for key in replacements))
    return placeholder.sub(lambda m: replacements[m.group(0)], RECOMMENDATIONS_ONLY_TEMPLATE)


def render_showcase_portal_html(
    *,
    static_export: bool = False,
    report: ShowcaseReport | None = None,
    custom_recommendations: RecommendationResponse | None = None,
    scenario: ShowcaseScenario | None = None,
) -> str:
    """Render the self-contained GM3 Light Web Workbench from a typed report."""
    if custom_recommendations is not None and report is None and scenario is None:
        return render_recommendations_html(custom_recommendations, static_export=static_export)

    eff_report = report if report is not None else evaluate_showcase_scenario(scenario)
    scen = eff_report.scenario
    catalog = build_model_catalog(overrides=scen.gsu_sizing.gsu_capacity_overrides or None)

    gsu_headers, gsu_rows_html = _build_sizing_table_html(
        eff_report.gsu_recommendations,
        scen.gsu_sizing.inventory_annotations,
    )

    header_rows_html = "".join(
        f"<tr><td class='num'>{hp.tier_id}</td>"
        f"<td><strong>{html.escape(hp.scenario)}</strong></td>"
        f"<td><code>{html.escape(hp.request_type_header)}</code></td>"
        f"<td><code>{html.escape(hp.shared_request_type_header)}</code></td>"
        f"<td><span class='badge badge-blue'>{html.escape(hp.uta_rank)}</span></td>"
        f"<td>{html.escape(hp.billing_and_capacity_behavior)}</td></tr>"
        for hp in eff_report.gateway.header_policies
    )

    cb_rows_html = "".join(
        f"<tr><td><code>{html.escape(t.tenant)}</code></td>"
        f"<td class='num'>{t.consumed_burndown_tokens:,.0f}</td>"
        f"<td class='num'>{t.usage_share_pct:.1f}%</td>"
        f"<td class='num'>{t.unweighted_raw_token_share_pct:.1f}%</td>"
        f"<td class='num'><strong>${t.active_allocated_cost_usd:,.2f}</strong></td>"
        f"<td class='num'>${t.unweighted_raw_token_cost_usd:,.2f}</td>"
        f"<td class='num'>${t.raw_token_distortion_usd:+,.2f}</td>"
        f"<td class='num'>${t.naive_full_pool_cost_usd:,.2f}</td>"
        f"<td class='num' style='color:var(--success);font-weight:600;'>"
        f"${t.idle_penalty_avoided_usd:,.2f}</td></tr>"
        for t in eff_report.chargeback.tenants
    )

    sched_rows_html = "".join(
        f"<tr><td><code>{html.escape(j.job_id)}</code></td>"
        f"<td class='num'>${j.value_usd:,.0f}</td>"
        f"<td class='num'>{j.burndown_tokens:,.0f}</td>"
        f"<td class='num'>T+{j.deadline_hour}h</td>"
        f"<td class='num'>${j.value_density_per_1m_tokens:,.2f}</td>"
        f"<td><span class='badge "
        f"{'badge-green' if j.status == 'admitted_edf_pt' else 'badge-warn'}'>"
        f"{html.escape(j.status)}</span></td>"
        f"<td><code>{html.escape(', '.join(f'{k}: {v}' for k, v in j.routing_headers.items()))}"
        f"</code></td><td class='num'>${j.marginal_cost_usd:,.2f}</td></tr>"
        for j in eff_report.residual_schedule.jobs
    )

    sizing_kpis_html, sh_section_html = render_sizing_kpis(eff_report)
    gw_top_html = render_gateway_top_section(eff_report.gateway, scen.gateway.config, scen)
    pool_kpi_html, pool_detail_html = render_finops_pool_cards(eff_report)

    selected_sim = normalize_model_id(scen.burst_and_cache.default_model)
    sim_options_html = "".join(
        f"<option value='{html.escape(val)}'{' selected' if val == selected_sim else ''}>"
        f"{html.escape(lbl)}</option>"
        for val, lbl in catalog.simulator_options()
    )

    flash_req = scen.burst_and_cache.flash_tier_audit
    if flash_req.strip_thought_signatures:
        selected_fail = "strip_sig"
    elif flash_req.single_turn_request and not flash_req.dummy_turn_anchor_applied:
        selected_fail = "single_unanchored"
    else:
        selected_fail = "none"
    fail_choices = (
        ("none", "Healthy Multi-Turn / Dummy Anchor"),
        ("single_unanchored", "Single-Turn Without Dummy Anchor (1/N hash)"),
        ("strip_sig", "Strip thought_signature (0.77% hit collapse)"),
    )
    sim_failure_options_html = "".join(
        f"<option value='{val}'{' selected' if val == selected_fail else ''}>{lbl}</option>"
        for val, lbl in fail_choices
    )

    mb = eff_report.micro_burst
    if mb.illusion_detected:
        burst_b_cls, burst_b_txt = (
            "badge-danger",
            f"60s Illusion Detected ({mb.refill_window_seconds}s Bucket)",
        )
    elif mb.triggers_429_or_spillover:
        burst_b_cls, burst_b_txt = (
            "badge-warn",
            f"Spillover Active ({mb.refill_window_seconds}s Bucket)",
        )
    else:
        burst_b_cls, burst_b_txt = (
            "badge-green",
            f"Within {mb.refill_window_seconds}s Token Bucket",
        )

    c_b_cls, c_b_txt, c_caption, sim_ban_cls, sim_ban_txt = _flash_cache_presentation(
        eff_report.prefix_cache_flash,
        flash_req,
    )

    pres = scen.presentation
    brand_name = html.escape(pres.platform_brand)
    is_central_gw = scen.gateway.presentation_mode == "central_proxy"
    sh_enabled = scen.self_hosted.enabled
    has_inventory_cols = bool(scen.gsu_sizing.inventory_annotations)
    fh = eff_report.gsu_recommendations.forecast_hours
    docs_header_right = (
        '<span class="badge badge-blue">Offline Static Scorecard</span>'
        if static_export
        else '<a href="/docs" class="docs-link">OpenAPI /docs &rarr;</a>'
    )
    default_page_title = (
        f"Capacity v{PACKAGE_VERSION} — AI Capacity, Micro-Burst, "
        "Multi-Gateway & FOCUS 1.2 Showcase"
    )
    page_title = html.escape(pres.title if eff_report.scenario_mode else default_page_title)

    kpis = eff_report.finops_kpis
    scav = eff_report.scavenger
    flash_c = eff_report.prefix_cache_flash
    pro_c = eff_report.prefix_cache_argon
    rr_c = eff_report.prefix_cache_rr

    replacements = {
        "__PAGE_TITLE__": page_title,
        "__FAVICON_URI__": FAVICON_DATA_URI,
        "__BRAND_HEADING__": html.escape(pres.title),
        "__BRAND_SUBTITLE__": html.escape(pres.subtitle),
        "__BADGE_PRIMARY__": html.escape(pres.primary_badge),
        "__BADGE_SECONDARY__": html.escape(pres.secondary_badge),
        "__DOCS_HEADER_RIGHT__": docs_header_right,
        "__TAB1_BUTTON_LABEL__": (
            "1. Dual-Mode Sizing &amp; Holt Forecast"
            if sh_enabled
            else f"1. {brand_name} GSU Sizing, Lifecycle &amp; Renewal"
        ),
        "__TAB3_BUTTON_LABEL__": (
            "3. Central Gateway Policies &amp; Upstream Contracts"
            if is_central_gw
            else "3. Multi-Gateway (LiteLLM / Envoy / K8s epp)"
        ),
        "__SIZING_KPIS__": sizing_kpis_html,
        "__SIZING_CARD_TITLE__": (
            f"Managed {brand_name} GSU Sizing &amp; +{fh}h Holt Forecast "
            "(Active Business Hours vs 24x7)"
        ),
        "__SIZING_CARD_SUB__": (
            "Includes Long-Term GA vs Short-Term GA lifecycle guardrails, fixed-term renewal "
            "tracking, suffix normalization, and daily-peak Holt trend projection."
            if has_inventory_cols
            else "Includes standard and high-speed (<code>-fast</code>) decode tiers, "
            "suffix normalization, and daily-peak Holt trend projection."
        ),
        "__GSU_TABLE_HEADERS__": gsu_headers,
        "__GSU_ROWS__": gsu_rows_html,
        "__SELF_HOSTED_SECTION__": sh_section_html,
        "__BURST_UTIL_SUMMARY__": (
            f"{mb.apparent_60s_utilization_pct:.1f}% &rarr; {mb.peak_window_utilization_pct:.1f}%"
        ),
        "__BURST_BADGE_CLASS__": burst_b_cls,
        "__BURST_BADGE_TEXT__": burst_b_txt,
        "__BURST_REQ_GSUS__": str(mb.gsus_required_for_zero_spillover),
        "__FLASH_CACHE_CUR__": f"{flash_c.effective_cache_hit_rate * 100.0:.1f}",
        "__FLASH_CACHE_OPT__": f"{flash_c.optimized_cache_hit_rate * 100.0:.1f}",
        "__FLASH_CACHE_BADGE_CLASS__": c_b_cls,
        "__FLASH_CACHE_BADGE_TEXT__": c_b_txt,
        "__FLASH_CACHE_CAPTION__": c_caption,
        "__PRO_CACHE_CUR__": f"{pro_c.effective_cache_hit_rate * 100.0:.1f}",
        "__RR_DILUTION_SUMMARY__": (
            f"{rr_c.baseline_cache_hit_rate * 100.0:.0f}% &rarr; "
            f"{rr_c.effective_cache_hit_rate * 100.0:.0f}% Hit"
        ),
        "__RR_WASTED_GSUS__": str(rr_c.wasted_gsus),
        "__RR_SAVINGS_PCT__": f"{rr_c.gsu_savings_pct:.1f}",
        "__SIM_MODEL_OPTIONS__": sim_options_html,
        "__SIM_GSUS_DEFAULT__": str(mb.provisioned_gsus),
        "__SIM_TPM60_DEFAULT__": str(round(scen.burst_and_cache.sixty_sec_tpm)),
        "__SIM_PEAK_TPS_DEFAULT__": str(round(scen.burst_and_cache.peak_1s_tps)),
        "__SIM_TOTAL_IN_DEFAULT__": str(flash_req.total_input_tokens),
        "__SIM_STATIC_IN_DEFAULT__": str(flash_req.static_prefix_tokens),
        "__SIM_DYN_OFFSET_DEFAULT__": str(
            flash_req.dynamic_header_offset_tokens
            if flash_req.dynamic_header_offset_tokens is not None
            else flash_req.static_prefix_tokens
        ),
        "__SIM_FAILURE_OPTIONS__": sim_failure_options_html,
        "__SIM_BANNER_CLASS__": sim_ban_cls,
        "__SIM_BANNER_TEXT__": sim_ban_txt,
        "__GATEWAY_TOP_SECTION__": gw_top_html,
        "__HEADER_CARD_TITLE__": (
            f"8 Canonical {brand_name} Upstream Wire Header Policies (6-Rank UTA Admission)"
            if is_central_gw
            else "8 Canonical Vertex AI Gateway Header Routing Policies (6-Rank UTA Admission)"
        ),
        "__HEADER_ROWS__": header_rows_html,
        "__KPI_CPVO__": f"{kpis.cpvo_usd:.4f}",
        "__KPI_CPWT__": f"{kpis.cpwt_1m_usd:.4f}",
        "__SCAV_SAVED__": f"{scav.double_payment_waste_avoided_usd:,.0f}",
        "__SCAV_DAY_UTIL__": f"{scav.daytime_active_utilization_pct:.0f}",
        "__SCAV_EFF_UTIL__": f"{scav.effective_utilization_after_scavenging_pct:.1f}",
        "__FINOPS_POOL_KPI_CARD__": pool_kpi_html,
        "__FINOPS_POOL_DETAIL_CARD__": pool_detail_html,
        "__KPI_CSR__": f"{kpis.csr_pct:.1f}",
        "__KPI_TER__": f"{kpis.ter_pct:.1f}",
        "__KPI_ECUR__": f"{kpis.ecur_168h_pct:.1f}",
        "__SCHED_ROWS__": sched_rows_html,
        "__CB_ROWS__": cb_rows_html,
    }

    placeholder = re.compile("|".join(re.escape(key) for key in replacements))
    return placeholder.sub(lambda m: replacements[m.group(0)], PORTAL_TEMPLATE)
