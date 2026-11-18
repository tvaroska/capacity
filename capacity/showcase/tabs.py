"""Workbench serving & burst physics tab builders (`tab-sizing`, `tab-burst`).

Projects `ShowcaseReport` sizing, disaggregated P/D, micro-burst, and prefix-cache results into
plain-text `TabView` trees with zero calculations (`P3.1`).
"""

from __future__ import annotations

from capacity.contract.requests import ModelTierPrefixCacheRequest
from capacity.contract.results import ModelTierPrefixCacheAudit
from capacity.contract.scenario import ShowcaseReport
from capacity.showcase.reallocation_cards import build_reallocation_and_spillover_rows
from capacity.showcase.view import (
    BadgeView,
    BannerView,
    CardRowView,
    CardView,
    ControlFieldView,
    ControlOptionView,
    KpiCardView,
    TabView,
    build_sizing_table_view,
    compute_mean_duty_pct,
)

OFFLINE_NOTE = " (Offline snapshot — run capacity serve for live API recalculation.)"


def num_control(
    field_id: str,
    label: str,
    value: int | float | str,
    *,
    handler: str,
    disabled: bool,
    min_val: str = "0",
    max_val: str | None = None,
    step: str | None = None,
) -> ControlFieldView:
    return ControlFieldView(
        field_id=field_id,
        label=label,
        value=str(value),
        min_val=min_val,
        max_val=max_val,
        step=step,
        handler=handler,
        disabled=disabled,
    )


def _sizing_kpis_and_self_hosted_rows(
    report: ShowcaseReport,
    *,
    static_export: bool,
) -> tuple[tuple[KpiCardView, ...], tuple[CardRowView, ...]]:
    scenario = report.scenario
    duty_val = f"{compute_mean_duty_pct(report.gsu_recommendations):.1f}%"
    wave_lbl = scenario.presentation.wave_badge_label

    if not scenario.self_hosted.enabled or report.self_hosted_audit is None:
        anns = list(scenario.gsu_sizing.inventory_annotations.values())
        total_gsus = sum(a.contracted_gsus or 0 for a in anns)
        total_429 = sum(a.http_429_count or 0 for a in anns)
        total_spill = sum(a.spillover_requests or 0 for a in anns)
        expiring_gsus = sum(
            (
                a.expiring_fixed_gsus
                if a.expiring_fixed_gsus is not None
                else (a.contracted_gsus or 0)
            )
            for a in anns
            if a.renewal_mode in ("fixed_term", "mixed")
        )
        exp_dates = sorted({a.expiry_date for a in anns if a.expiry_date})
        exp_label = exp_dates[0] if exp_dates else "Active Term"
        return (
            (
                KpiCardView(
                    label="Active vs 24x7 Duty Cycle",
                    value=duty_val,
                    badge=BadgeView(wave_lbl, "blue"),
                    caption="Sizes to active weekday peaks + Priority spillover",
                ),
                KpiCardView(
                    label="Contracted Managed PT Fleet",
                    value=f"{total_gsus:,} GSUs",
                    badge=BadgeView("0% HTTP 429 Inside PT Ceiling", "green"),
                    caption="Pooled across 1P Gemini & 3P Claude reservations",
                ),
                KpiCardView(
                    label="Spillover & Throttling",
                    value=f"{total_429:,} HTTP 429s",
                    badge=BadgeView(f"{total_spill:,} On-Demand Spillover Calls", "warn"),
                    caption="Route interactive spillover to 1.8x Priority PayGo",
                ),
                KpiCardView(
                    label="Fixed-Term Renewal Cliff",
                    value=f"{expiring_gsus:,} GSUs",
                    badge=BadgeView(f"Expires {exp_label}", "danger"),
                    caption="Right-size onto Long-Term GA models before expiry",
                ),
            ),
            (),
        )

    sh_d = report.self_hosted_audit
    disagg_d = report.disaggregated_sizing
    if disagg_d is None:
        return (), ()
    disagg_req = scenario.self_hosted.disaggregated
    disagg_badge = (
        BadgeView(
            f"Saves {disagg_d.replica_delta} Replica(s) vs Monolithic",
            "green",
            element_id="kpi-disagg-badge",
        )
        if disagg_d.replica_delta > 0
        else BadgeView(
            f"Monolithic Cheaper ({disagg_d.monolithic_replicas} vs "
            f"{disagg_d.total_disaggregated_replicas} Reps)",
            "warn",
            element_id="kpi-disagg-badge",
        )
    )
    kpis = (
        KpiCardView(
            label="Active vs 24x7 Duty Cycle",
            value=duty_val,
            badge=BadgeView(wave_lbl, "blue"),
            caption="Prevents 24x7 zero-padding P50=0 under-sizing",
        ),
        KpiCardView(
            label="Upfront GPU Quota Lock",
            value=f"{sh_d.locked_regional_accelerators} GPUs",
            badge=BadgeView(f"{sh_d.stranded_quota_accelerators} Stranded vs Min", "danger"),
            caption=(
                f"max_replica_count ({sh_d.max_replica_count}) \u00d7 "
                f"{sh_d.accelerators_per_replica} {sh_d.accelerator_type}"
            ),
            value_id="kpi-locked-gpus",
        ),
        KpiCardView(
            label="Disaggregated P/D Pool",
            value=f"{disagg_d.prefill_replicas}P + {disagg_d.decode_replicas}D",
            badge=disagg_badge,
            caption="Isolates TTFT prefill bursts from TPOT decode",
            value_id="kpi-disagg-reps",
        ),
        KpiCardView(
            label="Non-Prod Scale-to-Zero",
            value=f"${sh_d.scale_to_zero_nonprod_savings_usd:,.0f}/mo",
            badge=BadgeView("ScaleToZeroSpec (108h/wk idle)", "green"),
            caption="Eliminates overnight/weekend idle GPU burn",
        ),
    )
    sh_sub = (
        "Separate compute-bound Prefill replicas (TTFT SLA) from HBM bandwidth/KV-bound Decode "
        "replicas (TPOT SLA)." + (OFFLINE_NOTE if static_export else "")
    )
    sh_card = CardView(
        title=(
            "Interactive Disaggregated Prefill / Decode GPU Pool Sizer "
            "(llm-d / GKE Inference Gateway)"
        ),
        subtitle=sh_sub,
        controls=(
            num_control(
                "pd-qps",
                "Peak Arrival QPS",
                f"{disagg_req.peak_qps:g}",
                min_val="0.1",
                max_val="500",
                step="1",
                handler="recalcDisagg()",
                disabled=static_export,
            ),
            num_control(
                "pd-in",
                "Avg Input Tokens",
                disagg_req.avg_input_tokens,
                min_val="256",
                max_val="128000",
                step="256",
                handler="recalcDisagg()",
                disabled=static_export,
            ),
            num_control(
                "pd-out",
                "Avg Output Tokens",
                disagg_req.avg_output_tokens,
                min_val="32",
                max_val="16384",
                step="50",
                handler="recalcDisagg()",
                disabled=static_export,
            ),
            num_control(
                "pd-ttft",
                "Target TTFT (ms)",
                f"{disagg_req.target_ttft_ms:g}",
                min_val="50",
                max_val="2000",
                step="25",
                handler="recalcDisagg()",
                disabled=static_export,
            ),
        ),
        banner=BannerView(disagg_d.recommendation, "info", element_id="pd-output"),
    )
    return kpis, (CardRowView((sh_card,)),)


def build_sizing_tab(report: ShowcaseReport, *, static_export: bool) -> TabView:
    scen = report.scenario
    brand_name = scen.presentation.platform_brand
    fh = report.gsu_recommendations.forecast_hours
    has_inventory_cols = bool(scen.gsu_sizing.inventory_annotations)
    sizing_kpis, sh_rows = _sizing_kpis_and_self_hosted_rows(report, static_export=static_export)
    sizing_card = CardView(
        title=(
            f"Managed {brand_name} GSU Sizing & +{fh}h Holt Forecast "
            "(Active Business Hours vs 24x7)"
        ),
        subtitle=(
            "Includes Long-Term GA vs Short-Term GA lifecycle guardrails, fixed-term renewal "
            "tracking, suffix normalization, and daily-peak Holt trend projection."
            if has_inventory_cols
            else "Includes standard and high-speed (-fast) decode tiers, suffix normalization, "
            "and daily-peak Holt trend projection."
        ),
        table=build_sizing_table_view(
            report.gsu_recommendations,
            scen.gsu_sizing.inventory_annotations,
        ),
    )
    realloc_rows = build_reallocation_and_spillover_rows(report)
    return TabView(
        panel_id="tab-sizing",
        button_label=(
            "1. Dual-Mode Sizing & Holt Forecast"
            if scen.self_hosted.enabled
            else f"1. {brand_name} GSU Sizing, Lifecycle & Renewal"
        ),
        kpis=sizing_kpis,
        rows=(*realloc_rows, CardRowView((sizing_card,)), *sh_rows),
        active=True,
    )


def _flash_cache_views(
    audit: ModelTierPrefixCacheAudit,
    req: ModelTierPrefixCacheRequest,
) -> tuple[BadgeView, str, BannerView]:
    caption = (
        f"{audit.normalized_model} uses {audit.block_size_tokens:,}-token blocks "
        f"({audit.min_prefix_floor_tokens:,} floor)"
        if audit.checkpoint_granularity == "block_4096"
        else f"{audit.normalized_model} uses token-boundary checkpointing "
        f"({audit.min_prefix_floor_tokens:,} floor)"
    )
    first_step = audit.remediation_steps[0] if audit.remediation_steps else ""
    if audit.block_zero_contaminated:
        offset = req.dynamic_header_offset_tokens or 0
        bound = (
            audit.min_prefix_floor_tokens
            if audit.checkpoint_granularity == "token_boundary"
            else audit.block_size_tokens
        )
        return (
            BadgeView(
                f"Dynamic Token @ Offset {offset:,} (<{bound:,})",
                "danger",
                element_id="cache-block0-badge",
            ),
            caption,
            BannerView(
                f"Block 0 Contamination Active: {first_step} "
                f"(cuts weighted burndown waste by {audit.burndown_waste_pct:.1f}%).",
                "danger",
                element_id="sim-remediation-box",
            ),
        )
    if audit.below_minimum_floor:
        return (
            BadgeView(
                f"Below Cache Floor (<{audit.min_prefix_floor_tokens:,} tok)",
                "warn",
                element_id="cache-block0-badge",
            ),
            caption,
            BannerView(first_step, "warn", element_id="sim-remediation-box"),
        )
    if audit.strip_thought_signatures:
        return (
            BadgeView("thought_signature Stripped", "warn", element_id="cache-block0-badge"),
            caption,
            BannerView(first_step, "warn", element_id="sim-remediation-box"),
        )
    if audit.single_turn_request and not audit.dummy_turn_anchor_applied:
        return (
            BadgeView("Single-Turn 1/N Hash", "warn", element_id="cache-block0-badge"),
            caption,
            BannerView(first_step, "warn", element_id="sim-remediation-box"),
        )
    hit_pct = audit.effective_cache_hit_rate * 100.0
    return (
        BadgeView(
            f"Prefix Cache Healthy ({hit_pct:.1f}% Hit)",
            "green",
            element_id="cache-block0-badge",
        ),
        caption,
        BannerView(first_step, "info", element_id="sim-remediation-box"),
    )


def build_burst_tab(report: ShowcaseReport, *, static_export: bool) -> TabView:
    scen = report.scenario
    mb = report.micro_burst
    if mb.illusion_detected:
        burst_badge = BadgeView(
            f"60s Illusion Detected ({mb.refill_window_seconds}s Bucket)",
            "danger",
            element_id="burst-badge",
        )
    elif mb.triggers_429_or_spillover:
        burst_badge = BadgeView(
            f"Spillover Active ({mb.refill_window_seconds}s Bucket)",
            "warn",
            element_id="burst-badge",
        )
    else:
        burst_badge = BadgeView(
            f"Within {mb.refill_window_seconds}s Token Bucket",
            "green",
            element_id="burst-badge",
        )

    flash_req = scen.burst_and_cache.flash_tier_audit
    flash_c = report.prefix_cache_flash
    pro_c = report.prefix_cache_argon
    rr_c = report.prefix_cache_rr
    cache_badge, cache_caption, sim_banner = _flash_cache_views(flash_c, flash_req)

    if flash_req.strip_thought_signatures:
        selected_fail = "strip_sig"
    elif flash_req.single_turn_request and not flash_req.dummy_turn_anchor_applied:
        selected_fail = "single_unanchored"
    else:
        selected_fail = "none"

    burst_kpis = (
        KpiCardView(
            label="60s Chart vs Sub-Second Reality",
            value=(
                f"{mb.apparent_60s_utilization_pct:.1f}% \u2192 "
                f"{mb.peak_window_utilization_pct:.1f}%"
            ),
            badge=burst_badge,
            caption=f"Requires {mb.gsus_required_for_zero_spillover} GSUs for zero 429s/spillover",
            value_id="burst-60s-val",
            caption_id="burst-caption",
        ),
        KpiCardView(
            label="Block-0 Prefix Contamination",
            value=(
                f"{flash_c.effective_cache_hit_rate * 100.0:.1f}% \u2192 "
                f"{flash_c.optimized_cache_hit_rate * 100.0:.1f}%"
            ),
            badge=cache_badge,
            caption=cache_caption,
            value_id="cache-hit-val",
        ),
        KpiCardView(
            label="Argon/Pro Token Checkpointing",
            value=f"{pro_c.effective_cache_hit_rate * 100.0:.1f}% Hit",
            badge=BadgeView(f"{pro_c.min_prefix_floor_tokens:,} Floor + Token Boundary", "green"),
            caption="Checkpoints exact system_instruction & tools boundary",
        ),
        KpiCardView(
            label="1/N Round-Robin Cache Dilution",
            value=(
                f"{rr_c.baseline_cache_hit_rate * 100.0:.0f}% \u2192 "
                f"{rr_c.effective_cache_hit_rate * 100.0:.0f}% Hit"
            ),
            badge=BadgeView(f"{rr_c.wasted_gsus} Wasted GSUs Across Replicas", "warn"),
            caption=f"Sticky / prefix-aware routing saves {rr_c.gsu_savings_pct:.1f}% burndown",
        ),
    )
    sim_sub = (
        "Adjust GSU pool size, prompt tokens, static prefix length, and dynamic header offset "
        "to test 1s (\u226550 GSUs) vs 10s (<50 GSUs) buckets, 3P Claude 30s/60s buckets, "
        "Short-Term vs Long-Term GA rules, and Block-0 contamination."
        + (OFFLINE_NOTE if static_export else "")
    )
    dyn_default = (
        flash_req.dynamic_header_offset_tokens
        if flash_req.dynamic_header_offset_tokens is not None
        else flash_req.static_prefix_tokens
    )
    sim_card = CardView(
        title="Interactive Sub-Second Token-Bucket & Model-Tier Prefix-Cache Simulator",
        subtitle=sim_sub,
        controls=(
            ControlFieldView(
                field_id="sim-model",
                label="Model Tier",
                kind="select",
                handler="recalcBurstAndCache()",
                options=tuple(
                    ControlOptionView(val, lbl, selected=(val == report.selected_simulator_model))
                    for val, lbl in report.simulator_options
                ),
                disabled=static_export,
            ),
            num_control(
                "sim-gsus",
                "Provisioned GSUs",
                mb.provisioned_gsus,
                min_val="1",
                max_val="2000",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            num_control(
                "sim-60s-tpm",
                "60s Cloud Monitoring TPM",
                round(scen.burst_and_cache.sixty_sec_tpm),
                step="25000",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            num_control(
                "sim-peak-tps",
                "Sub-Second Peak TPS",
                round(scen.burst_and_cache.peak_1s_tps),
                step="1000",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            num_control(
                "sim-total-in",
                "Total Prompt Tokens",
                flash_req.total_input_tokens,
                min_val="512",
                step="1024",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            num_control(
                "sim-static-in",
                "Static Prefix Tokens",
                flash_req.static_prefix_tokens,
                step="1024",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            num_control(
                "sim-dyn-offset",
                "Dynamic Token Offset (<Static)",
                dyn_default,
                step="100",
                handler="recalcBurstAndCache()",
                disabled=static_export,
            ),
            ControlFieldView(
                field_id="sim-strip-sig",
                label="Multi-Turn Failure Mode",
                kind="select",
                handler="recalcBurstAndCache()",
                options=(
                    ControlOptionView(
                        "none",
                        "Healthy Multi-Turn / Dummy Anchor",
                        selected=(selected_fail == "none"),
                    ),
                    ControlOptionView(
                        "single_unanchored",
                        "Single-Turn Without Dummy Anchor (1/N hash)",
                        selected=(selected_fail == "single_unanchored"),
                    ),
                    ControlOptionView(
                        "strip_sig",
                        "Strip thought_signature (0.77% hit collapse)",
                        selected=(selected_fail == "strip_sig"),
                    ),
                ),
                disabled=static_export,
            ),
        ),
        banner=sim_banner,
    )
    return TabView(
        panel_id="tab-burst",
        button_label="2. Micro-Burst & Prefix-Cache Physics",
        kpis=burst_kpis,
        rows=(CardRowView((sim_card,)),),
    )
