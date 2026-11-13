"""Payload builder and HTML renderer for the GM3 Light 4-Tab Web Workbench (`portal.py`)."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from capacity.burst import (
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import build_response, require_capacity
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.forecasting import build_demo_throughput
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import normalize_model_id, resolve_model_family
from capacity.models import RecommendationResponse, fields_of, make_default_deadline_jobs
from capacity.portal_sections import (
    SIM_MODELS,
    lifecycle_badge,
    lookup_annotation,
    render_finops_pool_cards,
    render_gateway_top_section,
    render_sizing_kpis,
    renewal_cell,
)
from capacity.portal_template import FAVICON_DATA_URI, PORTAL_TEMPLATE
from capacity.scenario import ShowcaseScenario
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)


def _scenario_recommendations(scenario: ShowcaseScenario) -> RecommendationResponse:
    gs = scenario.gsu_sizing
    base_dt = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    sparse: dict[str, dict[datetime, int]] = {}
    max_len = 0
    for key, arr in gs.minute_tpm_series.items():
        max_len = max(max_len, len(arr))
        sparse[key] = {
            base_dt + timedelta(minutes=i): round(val) for i, val in enumerate(arr) if val > 0.0
        }
    total_mins = max(gs.days * 24 * 60, max_len)
    return build_response(
        project=gs.project_label,
        days=gs.days,
        sparse_series=sparse,
        total_minutes=total_mins,
        percentiles=[50, 75, 90, 95],
        active_only=gs.active_only,
        by_region=gs.by_region,
        forecast_steps=gs.forecast_hours,
        mode="gsu",
        gsu_overrides=gs.gsu_capacity_overrides or None,
    )


def build_showcase_summary_payload(
    custom_recommendations: RecommendationResponse | None = None,
    scenario: ShowcaseScenario | None = None,
) -> dict[str, Any]:
    """Build the complete Showcase executive payload across all 4 architectural planes."""
    if scenario is not None:
        gsu_recs = _scenario_recommendations(scenario)
        sh_sparse, sh_mins = build_demo_throughput(days=7, by_region=True, mode="self-hosted")
        sh_recs = build_response(
            project=scenario.gsu_sizing.project_label,
            days=7,
            sparse_series=sh_sparse,
            total_minutes=sh_mins,
            percentiles=[50, 75, 90, 95],
            active_only=True,
            by_region=True,
            mode="self-hosted",
        )
        sh_audit = evaluate_self_hosted_endpoint(**fields_of(scenario.self_hosted.audit))
        disagg = size_disaggregated_prefill_decode(**fields_of(scenario.self_hosted.disaggregated))

        bc = scenario.burst_and_cache
        overrides = scenario.gsu_sizing.gsu_capacity_overrides or None
        b_cap = require_capacity(bc.default_model, overrides)
        b_spec = resolve_model_family(bc.default_model)
        burst = evaluate_micro_burst_window(
            sixty_sec_tpm=bc.sixty_sec_tpm,
            peak_1s_tps=bc.peak_1s_tps,
            provisioned_gsus=bc.provisioned_gsus,
            tps_per_gsu=b_cap / 60.0,
            peak_10s_tps=bc.peak_10s_tps,
            model=bc.default_model,
            peak_30s_tps=bc.peak_30s_tps,
        )
        pd_req = bc.prefix_dilution
        prefix_rr = evaluate_prefix_cache_dilution(
            total_input_tokens=pd_req.total_input_tokens,
            shared_prefix_ratio=pd_req.shared_prefix_ratio,
            baseline_cache_hit_rate=pd_req.baseline_cache_hit_rate,
            num_round_robin_endpoints=pd_req.num_round_robin_endpoints,
            tps_per_gsu=b_cap / 60.0,
            cached_token_weight=b_spec.standard_cached_read_weight,
            uncached_token_weight=b_spec.standard_input_weight,
            requests_per_second=pd_req.requests_per_second or 1.0,
        )
        prefix_flash = evaluate_model_tier_prefix_cache(**fields_of(bc.flash_tier_audit))
        prefix_argon = evaluate_model_tier_prefix_cache(**fields_of(bc.pro_tier_audit))
        gateway = generate_gateway_bundle(**fields_of(scenario.gateway.config))

        fo = scenario.finops
        c_req = fo.commitment
        commitment = optimize_hybrid_commitment(
            tpm_series=c_req.tpm_series,
            model=normalize_model_id(c_req.model),
            tpm_per_gsu=require_capacity(c_req.model, overrides),
            gsu_monthly_price_usd=c_req.gsu_monthly_price_usd,
            paygo_per_1m_burndown_tokens_usd=c_req.paygo_per_1m_burndown_tokens_usd,
            spillover_tier=c_req.spillover_tier,
            fsp_discount_pct=c_req.fsp_discount_pct,
            active_duty_cycle_pct=c_req.active_duty_cycle_pct,
        )
        chargeback = compute_focus_chargeback(
            tpm_per_gsu=require_capacity(fo.chargeback.model, overrides),
            **fields_of(fo.chargeback, without=("model",)),
        )
        scavenger = evaluate_batch_scavenger_economics(
            model=normalize_model_id(fo.scavenger.model),
            tpm_per_gsu=require_capacity(fo.scavenger.model, overrides),
            **fields_of(fo.scavenger, without=("model",)),
        )
        schedule = schedule_residual_capacity(
            tpm_per_gsu=require_capacity(fo.residual_schedule.model, overrides),
            **fields_of(fo.residual_schedule, without=("model",)),
        )
        folder_pooling = evaluate_folder_pooling(
            model=normalize_model_id(fo.folder_pooling.model),
            tpm_per_gsu=require_capacity(fo.folder_pooling.model, overrides),
            **fields_of(fo.folder_pooling, without=("model",)),
        )
        kpis = compute_finops_kpis(**fields_of(fo.kpis))
        return {
            "version": "0.3.0",
            "scenario_mode": True,
            "gsu_recommendations": gsu_recs.model_dump(),
            "self_hosted_recommendations": sh_recs.model_dump(),
            "self_hosted_audit": sh_audit.model_dump(),
            "disaggregated_sizing": disagg.model_dump(),
            "micro_burst": burst.model_dump(),
            "prefix_cache_rr": prefix_rr.model_dump(),
            "prefix_cache_flash": prefix_flash.model_dump(),
            "prefix_cache_argon": prefix_argon.model_dump(),
            "gateway": gateway.model_dump(),
            "commitment": commitment.model_dump(),
            "chargeback": chargeback.model_dump(),
            "scavenger": scavenger.model_dump(),
            "residual_schedule": schedule.model_dump(),
            "folder_pooling": folder_pooling.model_dump(),
            "finops_kpis": kpis.model_dump(),
        }

    if custom_recommendations is not None:
        gsu_recs = custom_recommendations
    else:
        gsu_sparse, total_mins = build_demo_throughput(days=7, by_region=True, mode="gsu")
        gsu_recs = build_response(
            project="demo-enterprise-ai",
            days=7,
            sparse_series=gsu_sparse,
            total_minutes=total_mins,
            percentiles=[50, 75, 90, 95],
            active_only=True,
            by_region=True,
            mode="gsu",
        )

    sh_sparse, sh_mins = build_demo_throughput(days=7, by_region=True, mode="self-hosted")
    sh_recs = build_response(
        project="demo-enterprise-ai",
        days=7,
        sparse_series=sh_sparse,
        total_minutes=sh_mins,
        percentiles=[50, 75, 90, 95],
        active_only=True,
        by_region=True,
        mode="self-hosted",
    )
    sh_audit = evaluate_self_hosted_endpoint(
        tpm_series=[520_000.0, 480_000.0, 380_000.0, 290_000.0, 180_000.0],
        profile="llama-3.3-70b-fp8",
        min_replica_count=2,
        max_replica_count=8,
    )
    disagg = size_disaggregated_prefill_decode(
        profile="llama-3.3-70b-fp8",
        peak_qps=18.0,
        avg_input_tokens=6_400,
        avg_output_tokens=450,
    )
    burst = evaluate_micro_burst_window(
        sixty_sec_tpm=750_000.0, peak_1s_tps=25_000.0, provisioned_gsus=10, tps_per_gsu=2_015.0
    )
    prefix_rr = evaluate_prefix_cache_dilution(
        total_input_tokens=20_000,
        shared_prefix_ratio=0.80,
        baseline_cache_hit_rate=0.80,
        num_round_robin_endpoints=8,
        tps_per_gsu=2_015.0,
    )
    prefix_flash = evaluate_model_tier_prefix_cache(
        model="gemini-3.8-flash",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=120,
        single_turn_request=True,
        dummy_turn_anchor_applied=False,
        strip_thought_signatures=False,
        num_replicas=8,
    )
    prefix_argon = evaluate_model_tier_prefix_cache(
        model="gemini-4-argon",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=None,
        single_turn_request=False,
        dummy_turn_anchor_applied=True,
        strip_thought_signatures=False,
        num_replicas=8,
    )
    gateway = generate_gateway_bundle(
        model="gemini-3.8-flash",
        project_id="prod-genai-pt-host",
        location="global",
        provisioned_gsus=20,
        target_rps=650,
        concurrent_sse_streams=400,
        pods=12,
        uvicorn_workers_per_pod=4,
    )
    commitment = optimize_hybrid_commitment(
        tpm_series=[
            420_000.0,
            390_000.0,
            360_000.0,
            310_000.0,
            280_000.0,
            250_000.0,
            240_000.0,
            220_000.0,
            190_000.0,
            160_000.0,
            90_000.0,
            40_000.0,
        ],
        model="gemini-3.8-flash",
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2_000.0,
        paygo_per_1m_burndown_tokens_usd=0.50,
        spillover_tier="priority",
        fsp_discount_pct=15.0,
    )
    chargeback = compute_focus_chargeback(
        hourly_consumed_tpm_by_tenant={
            "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
            "underwriting-copilot": [302_250.0] * 8 + [60_450.0] * 16,
            "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
        },
        provisioned_gsus=10,
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2_000.0,
        async_batch_waterfill_tpm=362_700.0,
    )
    scavenger = evaluate_batch_scavenger_economics(
        model="gemini-3.8-flash", provisioned_gsus=20, tpm_per_gsu=120_900
    )
    schedule = schedule_residual_capacity(
        provisioned_gsus=20, tpm_per_gsu=120_900, deadline_jobs=make_default_deadline_jobs()
    )
    folder_pooling = evaluate_folder_pooling(
        hourly_tpm_by_project={
            "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
            "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
            "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
        },
        model="gemini-3.8-flash",
        tpm_per_gsu=120_900,
    )
    kpis = compute_finops_kpis()

    return {
        "version": "0.3.0",
        "gsu_recommendations": gsu_recs.model_dump(),
        "self_hosted_recommendations": sh_recs.model_dump(),
        "self_hosted_audit": sh_audit.model_dump(),
        "disaggregated_sizing": disagg.model_dump(),
        "micro_burst": burst.model_dump(),
        "prefix_cache_rr": prefix_rr.model_dump(),
        "prefix_cache_flash": prefix_flash.model_dump(),
        "prefix_cache_argon": prefix_argon.model_dump(),
        "gateway": gateway.model_dump(),
        "commitment": commitment.model_dump(),
        "chargeback": chargeback.model_dump(),
        "scavenger": scavenger.model_dump(),
        "residual_schedule": schedule.model_dump(),
        "folder_pooling": folder_pooling.model_dump(),
        "finops_kpis": kpis.model_dump(),
    }


def _pct_cell(pct_map: dict[str, Any], key: str, unit: str) -> str:
    entry = pct_map.get(key)
    if entry is None:
        return "n/a"
    return f"{entry['tpm']:,} ({entry['recommended_gsus']} {unit})"


def render_showcase_portal_html(
    *,
    static_export: bool = False,
    custom_recommendations: RecommendationResponse | None = None,
    scenario: ShowcaseScenario | None = None,
) -> str:
    """Render the self-contained GM3 Light 4-Tab Interactive Web Calculator & Scorecard."""
    data = build_showcase_summary_payload(
        custom_recommendations=custom_recommendations,
        scenario=scenario,
    )

    unit_lbl = "Rep" if data["gsu_recommendations"].get("mode") == "self-hosted" else "GSU"
    annotations = scenario.gsu_sizing.inventory_annotations if scenario is not None else {}
    has_inventory_cols = bool(annotations)
    fh = scenario.gsu_sizing.forecast_hours if scenario is not None else 24

    gsu_headers = (
        "<th>Model</th><th>Region</th><th class='num'>1 GSU Cap (TPM)</th>"
        "<th class='num'>Duty Cycle</th>"
    )
    if has_inventory_cols:
        gsu_headers += (
            "<th class='num'>Contracted GSUs</th><th class='num'>Spillover / 429s</th>"
            "<th>GA Lifecycle Track</th><th>Term &amp; Renewal</th>"
        )
    else:
        gsu_headers += "<th class='num'>24x7 P50 (Zero-Padded)</th>"
    gsu_headers += (
        "<th class='num'>Active P50</th><th class='num'>Active P90</th>"
        f"<th class='num'>Active P95</th><th class='num'>Holt Forecast +{fh}h Peak</th>"
    )

    gsu_rows_html = ""
    for m in data["gsu_recommendations"]["models"]:
        act = m["active_hours"]
        all_h = m["all_hours"]
        cap_val = m["gsu_capacity_tpm"]
        cap_str = f"{cap_val:,}" if cap_val else "?"
        ann = lookup_annotation(annotations, str(m["model"]), m.get("region"))
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
        else:
            inv_cells = f"<td class='num'>{_pct_cell(all_h, 'P50', unit_lbl)}</td>"

        gsu_rows_html += (
            f"<tr><td><code>{html.escape(str(m['model']))}</code></td>"
            f"<td>{html.escape(str(m['region'] or 'global'))}</td>"
            f"<td class='num'>{cap_str}</td><td class='num'>{m['duty_cycle_pct']:.1f}%</td>"
            f"{inv_cells}<td class='num'>{_pct_cell(act, 'P50', unit_lbl)}</td>"
            f"<td class='num'>{_pct_cell(act, 'P90', unit_lbl)}</td>"
            f"<td class='num'><strong>{_pct_cell(act, 'P95', unit_lbl)}</strong></td>"
            f"<td class='num'>{m['forecast_24h_peak_tpm']:,} "
            f"({m['forecast_24h_recommended_gsus']} {unit_lbl})</td></tr>"
        )

    header_rows_html = "".join(
        f"<tr><td class='num'>{hp['tier_id']}</td>"
        f"<td><strong>{html.escape(str(hp['scenario']))}</strong></td>"
        f"<td><code>{html.escape(str(hp['request_type_header']))}</code></td>"
        f"<td><code>{html.escape(str(hp['shared_request_type_header']))}</code></td>"
        f"<td><span class='badge badge-blue'>{html.escape(str(hp['uta_rank']))}</span></td>"
        f"<td>{html.escape(str(hp['billing_and_capacity_behavior']))}</td></tr>"
        for hp in data["gateway"]["header_policies"]
    )

    cb_rows_html = "".join(
        f"<tr><td><code>{html.escape(str(t['tenant']))}</code></td>"
        f"<td class='num'>{t['consumed_burndown_tokens']:,.0f}</td>"
        f"<td class='num'>{t['usage_share_pct']:.1f}%</td>"
        f"<td class='num'>{t['unweighted_raw_token_share_pct']:.1f}%</td>"
        f"<td class='num'><strong>${t['active_allocated_cost_usd']:,.2f}</strong></td>"
        f"<td class='num'>${t['unweighted_raw_token_cost_usd']:,.2f}</td>"
        f"<td class='num'>${t['raw_token_distortion_usd']:+,.2f}</td>"
        f"<td class='num'>${t['naive_full_pool_cost_usd']:,.2f}</td>"
        f"<td class='num' style='color:var(--success);font-weight:600;'>"
        f"${t['idle_penalty_avoided_usd']:,.2f}</td></tr>"
        for t in data["chargeback"]["tenants"]
    )

    sched_rows_html = "".join(
        f"<tr><td><code>{html.escape(str(j['job_id']))}</code></td>"
        f"<td class='num'>${j['value_usd']:,.0f}</td>"
        f"<td class='num'>{j['burndown_tokens']:,.0f}</td>"
        f"<td class='num'>T+{j['deadline_hour']}h</td>"
        f"<td class='num'>${j['value_density_per_1m_tokens']:,.2f}</td>"
        f"<td><span class='badge "
        f"{'badge-green' if j['status'] == 'admitted_edf_pt' else 'badge-warn'}'>"
        f"{html.escape(str(j['status']))}</span></td>"
        f"<td><code>{html.escape(', '.join(f'{k}: {v}' for k, v in j['routing_headers'].items()))}"
        f"</code></td><td class='num'>${j['marginal_cost_usd']:,.2f}</td></tr>"
        for j in data["residual_schedule"]["jobs"]
    )

    gw = data["gateway"]
    kpis = data["finops_kpis"]
    scav = data["scavenger"]
    fpool = data["folder_pooling"]
    mb = data["micro_burst"]
    flash_c = data["prefix_cache_flash"]
    pro_c = data["prefix_cache_argon"]
    rr_c = data["prefix_cache_rr"]

    sizing_kpis_html, sh_section_html = render_sizing_kpis(data, scenario)
    gw_top_html = render_gateway_top_section(gw, scenario)
    pool_kpi_html, pool_detail_html = render_finops_pool_cards(fpool, scenario)

    selected_sim = (
        normalize_model_id(scenario.burst_and_cache.default_model)
        if scenario is not None
        else "gemini-3.8-flash"
    )
    sim_options_html = "".join(
        f"<option value='{val}'{' selected' if val == selected_sim else ''}>"
        f"{html.escape(lbl)}</option>"
        for val, lbl in SIM_MODELS
    )

    if mb["illusion_detected"]:
        burst_b_cls, burst_b_txt = (
            "badge-danger",
            f"60s Illusion Detected ({mb['refill_window_seconds']}s Bucket)",
        )
    elif mb["triggers_429_or_spillover"]:
        burst_b_cls, burst_b_txt = (
            "badge-warn",
            f"Spillover Active ({mb['refill_window_seconds']}s Bucket)",
        )
    else:
        burst_b_cls, burst_b_txt = (
            "badge-green",
            f"Within {mb['refill_window_seconds']}s Token Bucket",
        )

    pres = scenario.presentation if scenario is not None else None
    brand_name = html.escape(pres.platform_brand if pres else "Gemini Platform")
    is_central_gw = scenario is not None and scenario.gateway.presentation_mode == "central_proxy"
    sh_enabled = scenario is None or scenario.self_hosted.enabled
    docs_header_right = (
        '<span class="badge badge-blue">Offline Static Scorecard</span>'
        if static_export
        else '<a href="/docs" class="docs-link">OpenAPI /docs &rarr;</a>'
    )

    replacements = {
        "__PAGE_TITLE__": (
            html.escape(pres.title)
            if pres
            else "Capacity v0.3.0 — AI Capacity, Micro-Burst, Multi-Gateway & FOCUS 1.2 Showcase"
        ),
        "__FAVICON_URI__": FAVICON_DATA_URI,
        "__BRAND_HEADING__": (
            html.escape(pres.title) if pres else "Capacity Showcase Workbench (v0.3.0)"
        ),
        "__BRAND_SUBTITLE__": (
            html.escape(pres.subtitle)
            if pres
            else (
                "Managed Gemini Platform GSUs + Self-Hosted GPU/vLLM &bull; "
                "Sub-Second Burst &amp; Prefix Cache &bull; "
                "LiteLLM / Envoy / K8s epp &bull; FOCUS 1.2 FinOps"
            )
        ),
        "__BADGE_PRIMARY__": html.escape(pres.primary_badge)
        if pres
        else "GM3 Light Tier-2 Showcase",
        "__BADGE_SECONDARY__": (
            html.escape(pres.secondary_badge) if pres else "FOCUS 1.2 &amp; bq-eval CPVO Ready"
        ),
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
            f"{mb['apparent_60s_utilization_pct']:.1f}% &rarr; "
            f"{mb['peak_window_utilization_pct']:.1f}%"
        ),
        "__BURST_BADGE_CLASS__": burst_b_cls,
        "__BURST_BADGE_TEXT__": burst_b_txt,
        "__BURST_REQ_GSUS__": str(mb["gsus_required_for_zero_spillover"]),
        "__FLASH_CACHE_CUR__": f"{flash_c['effective_cache_hit_rate'] * 100.0:.1f}",
        "__FLASH_CACHE_OPT__": f"{flash_c['optimized_cache_hit_rate'] * 100.0:.1f}",
        "__PRO_CACHE_CUR__": f"{pro_c['effective_cache_hit_rate'] * 100.0:.1f}",
        "__RR_DILUTION_SUMMARY__": (
            f"{rr_c['baseline_cache_hit_rate'] * 100.0:.0f}% &rarr; "
            f"{rr_c['effective_cache_hit_rate'] * 100.0:.0f}% Hit"
        ),
        "__RR_WASTED_GSUS__": str(rr_c["wasted_gsus"]),
        "__RR_SAVINGS_PCT__": f"{rr_c['gsu_savings_pct']:.1f}",
        "__SIM_MODEL_OPTIONS__": sim_options_html,
        "__SIM_GSUS_DEFAULT__": str(mb["provisioned_gsus"]),
        "__SIM_TPM60_DEFAULT__": str(
            round(scenario.burst_and_cache.sixty_sec_tpm) if scenario is not None else 750_000
        ),
        "__SIM_PEAK_TPS_DEFAULT__": str(
            round(scenario.burst_and_cache.peak_1s_tps) if scenario is not None else 25_000
        ),
        "__GATEWAY_TOP_SECTION__": gw_top_html,
        "__HEADER_CARD_TITLE__": (
            f"8 Canonical {brand_name} Upstream Wire Header Policies (6-Rank UTA Admission)"
            if is_central_gw
            else "8 Canonical Vertex AI Gateway Header Routing Policies (6-Rank UTA Admission)"
        ),
        "__HEADER_ROWS__": header_rows_html,
        "__KPI_CPVO__": f"{kpis['cpvo_usd']:.4f}",
        "__KPI_CPWT__": f"{kpis['cpwt_1m_usd']:.4f}",
        "__SCAV_SAVED__": f"{scav['double_payment_waste_avoided_usd']:,.0f}",
        "__SCAV_DAY_UTIL__": f"{scav['daytime_active_utilization_pct']:.0f}",
        "__SCAV_EFF_UTIL__": f"{scav['effective_utilization_after_scavenging_pct']:.1f}",
        "__FINOPS_POOL_KPI_CARD__": pool_kpi_html,
        "__FINOPS_POOL_DETAIL_CARD__": pool_detail_html,
        "__KPI_CSR__": f"{kpis['csr_pct']:.1f}",
        "__KPI_TER__": f"{kpis['ter_pct']:.1f}",
        "__KPI_ECUR__": f"{kpis['ecur_168h_pct']:.1f}",
        "__SCHED_ROWS__": sched_rows_html,
        "__CB_ROWS__": cb_rows_html,
    }

    placeholder = re.compile("|".join(re.escape(key) for key in replacements))
    return placeholder.sub(lambda m: replacements[m.group(0)], PORTAL_TEMPLATE)
