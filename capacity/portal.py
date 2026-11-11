"""Payload builder and HTML renderer for the GM3 Light 4-Tab Web Workbench (`portal.py`)."""

from __future__ import annotations

import html
import json
from typing import Any

from capacity.burst import (
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import build_response
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
from capacity.models import RecommendationResponse, make_default_deadline_jobs
from capacity.portal_template import FAVICON_DATA_URI, PORTAL_TEMPLATE
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)


def build_showcase_summary_payload(
    custom_recommendations: RecommendationResponse | None = None,
) -> dict[str, Any]:
    """Build the complete Showcase executive payload across all 4 architectural planes."""
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
        sixty_sec_tpm=750_000.0,
        peak_1s_tps=25_000.0,
        provisioned_gsus=10,
        tps_per_gsu=2_015.0,
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
        model="gemini-3.8-flash",
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
    )
    schedule = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
        deadline_jobs=make_default_deadline_jobs(),
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
    if key in pct_map:
        entry = pct_map[key]
        return f"{entry['tpm']:,} ({entry['recommended_gsus']} {unit})"
    first = next(iter(pct_map.values()), {"tpm": 0, "recommended_gsus": 0})
    return f"{first['tpm']:,} ({first['recommended_gsus']} {unit})"


def render_showcase_portal_html(
    *,
    static_export: bool = False,
    custom_recommendations: RecommendationResponse | None = None,
) -> str:
    """Render the self-contained GM3 Light 4-Tab Interactive Web Calculator & Scorecard."""
    data = build_showcase_summary_payload(custom_recommendations=custom_recommendations)
    embedded_json = json.dumps(data)

    unit_lbl = "Rep" if data["gsu_recommendations"].get("mode") == "self-hosted" else "GSU"
    gsu_rows_html = ""
    for m in data["gsu_recommendations"]["models"]:
        act = m["active_hours"]
        all_h = m["all_hours"]
        cap_val = m["gsu_capacity_tpm"]
        cap_str = f"{cap_val:,}" if cap_val else "?"
        gsu_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(m['model']))}</code></td>"
            f"<td>{html.escape(str(m['region'] or 'global'))}</td>"
            f"<td class='num'>{cap_str}</td>"
            f"<td class='num'>{m['duty_cycle_pct']:.1f}%</td>"
            f"<td class='num'>{_pct_cell(all_h, 'P50', unit_lbl)}</td>"
            f"<td class='num'>{_pct_cell(act, 'P50', unit_lbl)}</td>"
            f"<td class='num'>{_pct_cell(act, 'P90', unit_lbl)}</td>"
            f"<td class='num'><strong>{_pct_cell(act, 'P95', unit_lbl)}</strong></td>"
            f"<td class='num'>{m['forecast_24h_peak_tpm']:,} "
            f"({m['forecast_24h_recommended_gsus']} {unit_lbl})</td>"
            f"</tr>"
        )

    header_rows_html = ""
    for hp in data["gateway"]["header_policies"]:
        header_rows_html += (
            f"<tr>"
            f"<td class='num'>{hp['tier_id']}</td>"
            f"<td><strong>{html.escape(str(hp['scenario']))}</strong></td>"
            f"<td><code>{html.escape(str(hp['request_type_header']))}</code></td>"
            f"<td><code>{html.escape(str(hp['shared_request_type_header']))}</code></td>"
            f"<td><span class='badge badge-blue'>{html.escape(str(hp['uta_rank']))}</span></td>"
            f"<td>{html.escape(str(hp['billing_and_capacity_behavior']))}</td>"
            f"</tr>"
        )

    cb_rows_html = ""
    for t in data["chargeback"]["tenants"]:
        cb_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(t['tenant']))}</code></td>"
            f"<td class='num'>{t['consumed_burndown_tokens']:,.0f}</td>"
            f"<td class='num'>{t['usage_share_pct']:.1f}%</td>"
            f"<td class='num'>{t['unweighted_raw_token_share_pct']:.1f}%</td>"
            f"<td class='num'><strong>${t['active_allocated_cost_usd']:,.2f}</strong></td>"
            f"<td class='num'>${t['unweighted_raw_token_cost_usd']:,.2f}</td>"
            f"<td class='num'>${t['raw_token_distortion_usd']:+,.2f}</td>"
            f"<td class='num'>${t['naive_full_pool_cost_usd']:,.2f}</td>"
            f"<td class='num' style='color:var(--success);font-weight:600;'>"
            f"${t['idle_penalty_avoided_usd']:,.2f}</td>"
            f"</tr>"
        )

    sched_rows_html = ""
    for j in data["residual_schedule"]["jobs"]:
        badge_cls = "badge-green" if j["status"] == "admitted_edf_pt" else "badge-warn"
        hdr_str = ", ".join(f"{k}: {v}" for k, v in j["routing_headers"].items())
        sched_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(j['job_id']))}</code></td>"
            f"<td class='num'>${j['value_usd']:,.0f}</td>"
            f"<td class='num'>{j['burndown_tokens']:,.0f}</td>"
            f"<td class='num'>T+{j['deadline_hour']}h</td>"
            f"<td class='num'>${j['value_density_per_1m_tokens']:,.2f}</td>"
            f"<td><span class='badge {badge_cls}'>{html.escape(str(j['status']))}</span></td>"
            f"<td><code>{html.escape(hdr_str)}</code></td>"
            f"<td class='num'>${j['marginal_cost_usd']:,.2f}</td>"
            f"</tr>"
        )

    gw = data["gateway"]
    kpis = data["finops_kpis"]
    scav = data["scavenger"]
    fpool = data["folder_pooling"]
    disagg_d = data["disaggregated_sizing"]
    sh_d = data["self_hosted_audit"]
    flash_c = data["prefix_cache_flash"]

    docs_header_right = (
        '<span class="badge badge-blue">Offline Static Scorecard</span>'
        if static_export
        else '<a href="/docs" class="docs-link">OpenAPI /docs &rarr;</a>'
    )

    replacements = {
        "__FAVICON_URI__": FAVICON_DATA_URI,
        "__DOCS_HEADER_RIGHT__": docs_header_right,
        "__LOCKED_GPUS__": str(sh_d["locked_regional_accelerators"]),
        "__STRANDED_GPUS__": str(sh_d["stranded_quota_accelerators"]),
        "__MAX_REPS__": str(sh_d["max_replica_count"]),
        "__ACCEL_PER_REP__": str(sh_d["accelerators_per_replica"]),
        "__ACCEL_TYPE__": html.escape(str(sh_d["accelerator_type"])),
        "__PREFILL_REPS__": str(disagg_d["prefill_replicas"]),
        "__DECODE_REPS__": str(disagg_d["decode_replicas"]),
        "__REPLICA_DELTA__": str(disagg_d["replica_delta"]),
        "__SCALE_ZERO_SAVINGS__": f"{sh_d['scale_to_zero_nonprod_savings_usd']:,.0f}",
        "__GSU_ROWS__": gsu_rows_html,
        "__DISAGG_RECOMMENDATION__": html.escape(str(disagg_d["recommendation"])),
        "__FLASH_CACHE_CUR__": f"{flash_c['effective_cache_hit_rate'] * 100.0:.1f}",
        "__FLASH_CACHE_OPT__": f"{flash_c['optimized_cache_hit_rate'] * 100.0:.1f}",
        "__LITELLM_CFG__": html.escape(str(gw["litellm_config_yaml"])),
        "__LITELLM_CB__": html.escape(str(gw["litellm_burndown_callback_py"])),
        "__ENVOY_CFG__": html.escape(str(gw["envoy_ai_gateway_yaml"])),
        "__EPP_CFG__": html.escape(str(gw["gke_epp_inference_pool_yaml"])),
        "__HEADER_ROWS__": header_rows_html,
        "__KPI_CPVO__": f"{kpis['cpvo_usd']:.4f}",
        "__KPI_CPWT__": f"{kpis['cpwt_1m_usd']:.4f}",
        "__SCAV_SAVED__": f"{scav['double_payment_waste_avoided_usd']:,.0f}",
        "__SCAV_DAY_UTIL__": f"{scav['daytime_active_utilization_pct']:.0f}",
        "__SCAV_EFF_UTIL__": f"{scav['effective_utilization_after_scavenging_pct']:.1f}",
        "__POOL_SILOED__": str(fpool["siloed_total_gsus"]),
        "__POOL_POOLED__": str(fpool["pooled_folder_gsus"]),
        "__POOL_MO_SAVINGS__": f"{fpool['monthly_savings_usd']:,.0f}",
        "__POOL_YR_SAVINGS__": f"{fpool['annual_savings_usd']:,.0f}",
        "__KPI_CSR__": f"{kpis['csr_pct']:.1f}",
        "__KPI_TER__": f"{kpis['ter_pct']:.1f}",
        "__KPI_ECUR__": f"{kpis['ecur_168h_pct']:.1f}",
        "__SCHED_ROWS__": sched_rows_html,
        "__CB_ROWS__": cb_rows_html,
        "__EMBEDDED_JSON__": embedded_json,
    }

    rendered = PORTAL_TEMPLATE
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered
