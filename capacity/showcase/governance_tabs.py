"""Workbench gateway & FinOps governance tab builders (`tab-gateway`, `tab-finops`).

Projects `ShowcaseReport` gateway manifests, header policies, 3-Tier EDF schedules, and FOCUS 1.2
chargeback results into plain-text `TabView` trees with zero calculations (`P3.1`).
"""

from __future__ import annotations

from capacity.contract.requests import GatewayConfigRequest
from capacity.contract.results import GatewayConfigBundle
from capacity.contract.scenario import ShowcaseReport, ShowcaseScenario
from capacity.showcase.tabs import OFFLINE_NOTE, num_control
from capacity.showcase.view import (
    BadgeView,
    BannerTone,
    BannerView,
    CardRowView,
    CardView,
    CellView,
    KpiCardView,
    TableHeaderView,
    TableView,
    TabView,
)


def _build_central_gateway_views(
    scenario: ShowcaseScenario,
    header_card: CardView,
) -> tuple[tuple[KpiCardView, ...], tuple[CardRowView, ...]]:
    brand = scenario.presentation.platform_brand
    proxy_lbl = scenario.gateway.proxy_label
    kpis = (
        KpiCardView(
            label="100% Gateway Mediation",
            value="Single Choke Point",
            badge=BadgeView(proxy_lbl, "blue"),
            caption="HttpRetryOptions(attempts=1) prevents 9x retry storms",
        ),
        KpiCardView(
            label="Per-Desk Burndown Isolation",
            value="Two-Phase Quota",
            badge=BadgeView("1x/0.1x/4x \u2264200K \u2022 2x/0.2x/7.5x >200K", "green"),
            caption="Spills bursting tenants to 1.8x Priority before pool drain",
        ),
        KpiCardView(
            label="Prefix-Cache Checklist",
            value="0.77% \u2192 89.9%",
            badge=BadgeView("Preserve thought_signature", "green"),
            caption="\u22656,144 Flash prefix, Turn-0 anchor & regional pinning",
        ),
        KpiCardView(
            label="Stateful API Boundary",
            value="Option A / Option B",
            badge=BadgeView("Neutral Ports, Adapters & Contracts", "blue"),
            caption="Identical OTel & FOCUS 1.2 schema across both options",
        ),
    )
    contract_card = CardView(
        title=f"Phase 1 Non-Breaking /v1/chat/completions Upstream Contract ({brand})",
        subtitle=(
            f"Compiled upstream rules for {proxy_lbl} without changing downstream "
            "application endpoints."
        ),
        table=TableView(
            headers=(
                TableHeaderView("Control Point"),
                TableHeaderView("Gateway Contract"),
                TableHeaderView("Impact"),
            ),
            rows=(
                (
                    CellView(text="1P Interactive Spillover", strong=True),
                    CellView(
                        text=(
                            "Omit X-Vertex-AI-LLM-Request-Type + "
                            "X-Vertex-AI-LLM-Shared-Request-Type: priority"
                        )
                    ),
                    CellView(
                        text="0% PT 429s + 0.014% Priority spillover vs 2.58% sheddable PayGo"
                    ),
                ),
                (
                    CellView(text="Multi-Turn Reasoning Cache", strong=True),
                    CellView(text="Pass thought_signature untouched + regional pool affinity"),
                    CellView(
                        text="Prevents 0.77% cache collapse; sustains ~89.9% multi-turn hit rate"
                    ),
                ),
                (
                    CellView(text="Batch / Eval Isolation", strong=True),
                    CellView(
                        text="X-Vertex-AI-LLM-Request-Type: shared + flex (or night dedicated)"
                    ),
                    CellView(text="Isolates eval & optimization bursts from daytime desk PT"),
                ),
                (
                    CellView(text="1P vs 3P Claude Parity", strong=True),
                    CellView(
                        text="3P uses 30s/60s buckets, 1x/0.1x/1.25x/2x/5x & standard spillover"
                    ),
                    CellView(
                        text=(
                            "Reconciles 1P & 3P attribution via zero-payload inference_request logs"
                        )
                    ),
                ),
            ),
        ),
    )
    options_card = CardView(
        title=(
            "Non-Prescriptive Stateful Evolution "
            "(Option A vs Option B via Ports, Adapters and Contracts)"
        ),
        subtitle=(
            f"Keeps internal ownership between Central AI Platform and {proxy_lbl} open "
            "while preserving unified auditability."
        ),
        table=TableView(
            headers=(
                TableHeaderView("Dimension"),
                TableHeaderView("Option A: Thin Stateless Gateway + Platform Spine"),
                TableHeaderView("Option B: Dual-Lane Stateful Central Gateway"),
            ),
            rows=(
                (
                    CellView(text="Session & Tool State", strong=True),
                    CellView(
                        text=(
                            "AI Platform owns session history & MCP loops; "
                            "gateway stays ~2ms stateless"
                        )
                    ),
                    CellView(text="Gateway adds /v1beta1/interactions (store=false ZDR or CMEK)"),
                ),
                (
                    CellView(text="Token Budgets & Spillover", strong=True),
                    CellView(
                        text="Gateway enforces pool rate limits; Platform tracks per-agent budgets"
                    ),
                    CellView(
                        text=(
                            "Gateway enforces per-desk burndown budgets & Priority headers directly"
                        )
                    ),
                ),
                (
                    CellView(text="Audit & FOCUS 1.2 Output", strong=True),
                    CellView(
                        text=(
                            "Identical W3C traceparent OTel spans + zero-payload "
                            "inference_request join for Cost per Verified Outcome (CPVO)"
                        ),
                        colspan="2",
                    ),
                ),
            ),
        ),
    )
    return kpis, (CardRowView((contract_card, options_card)), CardRowView((header_card,)))


def _build_oss_gateway_views(
    gw: GatewayConfigBundle,
    gw_req: GatewayConfigRequest,
    header_card: CardView,
    *,
    static_export: bool,
) -> tuple[tuple[KpiCardView, ...], tuple[CardRowView, ...]]:
    audit = gw.litellm_audit
    if audit.scale_status == "migrate_dataplane_to_envoy":
        status_badge = BadgeView(
            "Migrate Data Plane to Envoy AI Gateway", "danger", element_id="gw-status-badge"
        )
        banner_tone: BannerTone = "danger"
    elif audit.scale_status == "tune_required":
        status_badge = BadgeView("Tune Required", "warn", element_id="gw-status-badge")
        banner_tone = "warn"
    else:
        status_badge = BadgeView("Healthy Scale Envelope", "green", element_id="gw-status-badge")
        banner_tone = "info"
    banner_msg = audit.bottleneck_warnings[-1] if audit.bottleneck_warnings else status_badge.text

    kpis = (
        KpiCardView(
            label="LiteLLM Prisma Engines",
            value=f"{audit.total_prisma_rust_engines} Engines",
            badge=status_badge,
            caption=(
                f"PgBouncer pool={audit.recommended_pgbouncer_pool_size}, "
                f"Postgres max_conns\u2265{audit.recommended_postgres_max_connections}"
            ),
            value_id="gw-prisma-val",
            caption_id="gw-pg-caption",
        ),
        KpiCardView(
            label="Anti-Blackhole Cooldown",
            value=f"{audit.anti_blackhole_cooldown_seconds}s Cooldown",
            badge=BadgeView(f"Matched to {gw.refill_window_seconds}s PT Bucket", "green"),
            caption="Prevents 1s/10s micro-burst 429 from blackholing PT",
            value_id="gw-cooldown-val",
        ),
        KpiCardView(
            label="Envoy AI Gateway CEL",
            value="Two-Phase RLS",
            badge=BadgeView("cost.request=256 + SSE CEL", "blue"),
            caption="Enforces \u2264200K & >200K per-BU burndown at >2,000 RPS",
        ),
        KpiCardView(
            label="GKE Inference Gateway (epp)",
            value="Weight 4:2:2:1",
            badge=BadgeView("Prefix + KV + Queue + LoRA", "green"),
            caption="Eliminates 1/N GPU prefix-cache dilution",
        ),
    )
    gw_sub = (
        "Simulate target RPS, streaming SSE concurrency, and pod/worker topology to size "
        "PgBouncer pools and detect the Envoy data-plane crossover."
        + (OFFLINE_NOTE if static_export else "")
    )
    scale_card = CardView(
        title="Interactive LiteLLM Proxy High-QPS Scale & Bottleneck Auditor",
        subtitle=gw_sub,
        controls=(
            num_control(
                "gw-rps",
                "Target Gateway RPS",
                gw_req.target_rps,
                min_val="10",
                max_val="10000",
                step="50",
                handler="recalcGatewayAudit()",
                disabled=static_export,
            ),
            num_control(
                "gw-sse",
                "Concurrent SSE Streams",
                gw_req.concurrent_sse_streams,
                max_val="5000",
                step="50",
                handler="recalcGatewayAudit()",
                disabled=static_export,
            ),
            num_control(
                "gw-pods",
                "LiteLLM Pods",
                gw_req.pods,
                min_val="1",
                max_val="100",
                step="1",
                handler="recalcGatewayAudit()",
                disabled=static_export,
            ),
            num_control(
                "gw-workers",
                "Uvicorn Workers / Pod",
                gw_req.uvicorn_workers_per_pod,
                min_val="1",
                max_val="16",
                step="1",
                handler="recalcGatewayAudit()",
                disabled=static_export,
            ),
        ),
        banner=BannerView(banner_msg, banner_tone, element_id="gw-audit-msg"),
    )
    litellm_row = CardRowView(
        (
            CardView(
                title="1. LiteLLM Proxy Config (litellm_config.yaml)",
                subtitle=(
                    "Tag-based PT \u2192 Priority 1.8x spillover, Non-Prod 0.5x Flex isolation, "
                    "and anti-blackholing cooldowns."
                ),
                code_block=gw.litellm_config_yaml,
            ),
            CardView(
                title="2. LiteLLM Weighted Burndown Hook (litellm_burndown_callback.py)",
                subtitle=(
                    "Enforces stateful Redis weighted burndown quotas, dynamic PT-bypass on "
                    "burst, and 0ms traffic_type SpendLogs tagging."
                ),
                code_block=gw.litellm_burndown_callback_py,
            ),
        )
    )
    envoy_epp_row = CardRowView(
        (
            CardView(
                title="3. Envoy AI Gateway CEL (AIGatewayRoute + BackendTrafficPolicy)",
                subtitle=(
                    "Two-phase pre-flight floor (cost=256) + post-response SSE type: CEL "
                    "per-BU burndown deduction."
                ),
                code_block=gw.envoy_ai_gateway_yaml,
            ),
            CardView(
                title="4. GKE Inference Gateway (InferencePool + EndpointPickerConfig)",
                subtitle=(
                    "Prefix-cache affinity and KV-cache utilization pod scoring with "
                    "Critical/Sheddable admission."
                ),
                code_block=gw.gke_epp_inference_pool_yaml,
            ),
        )
    )
    return kpis, (
        CardRowView((scale_card,)),
        litellm_row,
        envoy_epp_row,
        CardRowView((header_card,)),
    )


def build_gateway_tab(report: ShowcaseReport, *, static_export: bool) -> TabView:
    scen = report.scenario
    brand = scen.presentation.platform_brand
    is_central = scen.gateway.presentation_mode == "central_proxy"
    header_card = CardView(
        title=(
            f"8 Canonical {brand} Upstream Wire Header Policies (6-Rank UTA Admission)"
            if is_central
            else "8 Canonical Vertex AI Gateway Header Routing Policies (6-Rank UTA Admission)"
        ),
        table=TableView(
            headers=(
                TableHeaderView("#", numeric=True),
                TableHeaderView("Workload Tier & Scenario"),
                TableHeaderView("X-Vertex-AI-LLM-Request-Type"),
                TableHeaderView("X-Vertex-AI-LLM-Shared-Request-Type"),
                TableHeaderView("UTA Admission Rank"),
                TableHeaderView("Capacity & Chargeback Behavior"),
            ),
            rows=tuple(
                (
                    CellView(text=str(hp.tier_id), numeric=True),
                    CellView(text=hp.scenario, strong=True),
                    CellView(code=hp.request_type_header),
                    CellView(code=hp.shared_request_type_header),
                    CellView(badge=BadgeView(hp.uta_rank, "blue")),
                    CellView(text=hp.billing_and_capacity_behavior),
                )
                for hp in report.gateway.header_policies
            ),
        ),
    )
    if is_central:
        gw_kpis, gw_rows = _build_central_gateway_views(scen, header_card)
    else:
        gw_kpis, gw_rows = _build_oss_gateway_views(
            report.gateway, scen.gateway.config, header_card, static_export=static_export
        )
    return TabView(
        panel_id="tab-gateway",
        button_label=(
            "3. Central Gateway Policies & Upstream Contracts"
            if is_central
            else "3. Multi-Gateway (LiteLLM / Envoy / K8s epp)"
        ),
        kpis=gw_kpis,
        rows=gw_rows,
    )


def _finops_pool_views(report: ShowcaseReport) -> tuple[KpiCardView, CardRowView | None]:
    scenario = report.scenario
    fpool = report.folder_pooling
    if scenario.finops.pooling_mode == "1p_vs_3p_hub":
        brand = scenario.presentation.platform_brand
        kpi_card = KpiCardView(
            label="1P Gemini vs 3P Claude Parity",
            value="GA Priority + RHPT",
            badge=BadgeView("1P Full Parity \u2022 3P Hub Bridge", "blue"),
            caption=f"{brand} 1P supports Priority PayGo & Folder RHPT",
        )
        detail_card = CardView(
            title=(
                f"1P {brand} vs 3P Anthropic Claude PT Governance & "
                "Self-Service Chargeback Reconciliation"
            ),
            subtitle=(
                "Transparent capability matrix across merged FCFS host pools and zero-payload "
                "inference_request log exports."
            ),
            table=TableView(
                headers=(
                    TableHeaderView("Capability"),
                    TableHeaderView(f"1P {brand} (Argon / 3.8 Flash / 3.5 Flash-Lite)"),
                    TableHeaderView("3P Anthropic Claude PT"),
                ),
                rows=(
                    (
                        CellView(text="Burst Spillover Tier", strong=True),
                        CellView(text="GA 1.8x Priority PayGo (Rank 2 CRITICAL, 0.014% 429s)"),
                        CellView(
                            text=(
                                "Standard On-Demand spillover "
                                "(3P Priority PayGo tracked separately)"
                            )
                        ),
                    ),
                    (
                        CellView(text="Pool Scope & Restructure", strong=True),
                        CellView(text="Project or Shared Folder RHPT; self-service Console swaps"),
                        CellView(text="Project-scoped 30s/60s dual buckets (0% burst credit)"),
                    ),
                    (
                        CellView(text="Per-Desk PT Chargeback", strong=True),
                        CellView(text="usage_metadata.traffic_type + inference_request logs"),
                        CellView(
                            text="Zero-payload inference_request log join + gateway burndown ledger"
                        ),
                    ),
                ),
            ),
        )
        return kpi_card, CardRowView((detail_card,))

    kpi_card = KpiCardView(
        label="Shared Folder RHPT Pooling",
        value=f"{fpool.siloed_total_gsus} \u2192 {fpool.pooled_folder_gsus} GSUs",
        badge=BadgeView(
            f"Saves ${fpool.monthly_savings_usd:,.0f}/mo (${fpool.annual_savings_usd:,.0f}/yr)",
            "blue",
        ),
        caption="Follow-the-sun EMEA + AMER + APAC spoke pooling",
    )
    return kpi_card, None


def build_finops_tab(report: ShowcaseReport) -> TabView:
    kpis = report.finops_kpis
    scav = report.scavenger
    pool_kpi, pool_detail_row = _finops_pool_views(report)
    finops_kpis = (
        KpiCardView(
            label="Cost / Verified Outcome (CPVO)",
            value=f"${kpis.cpvo_usd:.4f}",
            badge=BadgeView("Bridges to bq-eval v_step1_cost_per_outcome", "green"),
            caption=f"CPWT_1M: ${kpis.cpwt_1m_usd:.4f} / 1M weighted tokens",
        ),
        KpiCardView(
            label="Batch 0.5x Double-Pay Saved",
            value=f"${scav.double_payment_waste_avoided_usd:,.0f}/mo",
            badge=BadgeView(
                f"Lifts PT Util {scav.daytime_active_utilization_pct:.0f}% \u2192 "
                f"{scav.effective_utilization_after_scavenging_pct:.1f}%",
                "green",
            ),
            caption="Paced online scavenging vs separate 0.5x Batch API",
        ),
        pool_kpi,
        KpiCardView(
            label="Token & Cache Efficiency",
            value=f"CSR {kpis.csr_pct:.1f}%",
            badge=BadgeView(
                f"TER {kpis.ter_pct:.1f}% \u2022 ECUR_168h {kpis.ecur_168h_pct:.1f}%",
                "blue",
            ),
            caption="FinOps Foundation 5 Standardized AI KPIs",
        ),
    )
    sched_card = CardView(
        title=(
            "3-Tier Residual Capacity Scheduler "
            "(Tier 0 Revenue > Tier 1 Hard-Deadline EDF > Tier 2 Evergreen Water-Fill)"
        ),
        subtitle=(
            f"Selects the exact value-maximizing deadline-feasible subset over residual budget "
            f"A(t) = {report.residual_schedule.safety_headroom_ratio:.2f} \u00d7 C \u2212 R(t) "
            "and executes admitted jobs in Earliest Deadline First (EDF) order, spilling excess "
            "jobs to 0.5x Flex."
        ),
        table=TableView(
            headers=(
                TableHeaderView("Job ID"),
                TableHeaderView("Business Value", numeric=True),
                TableHeaderView("Burndown Tokens", numeric=True),
                TableHeaderView("Hard Cutoff", numeric=True),
                TableHeaderView("Value Density / 1M", numeric=True),
                TableHeaderView("Admission Decision"),
                TableHeaderView("Injected Gateway Headers"),
                TableHeaderView("Marginal Cost", numeric=True),
            ),
            rows=tuple(
                (
                    CellView(code=j.job_id),
                    CellView(text=f"${j.value_usd:,.0f}", numeric=True),
                    CellView(text=f"{j.burndown_tokens:,.0f}", numeric=True),
                    CellView(text=f"T+{j.deadline_hour}h", numeric=True),
                    CellView(text=f"${j.value_density_per_1m_tokens:,.2f}", numeric=True),
                    CellView(
                        badge=BadgeView(
                            j.status,
                            "green" if j.status == "admitted_edf_pt" else "warn",
                        )
                    ),
                    CellView(code=", ".join(f"{k}: {v}" for k, v in j.routing_headers.items())),
                    CellView(text=f"${j.marginal_cost_usd:,.2f}", numeric=True),
                )
                for j in report.residual_schedule.jobs
            ),
        ),
    )
    cb_card = CardView(
        title=(
            "FOCUS 1.2 Two-Pool Chargeback Ledger "
            "(Active Consumed vs Unallocated Idle + Batch Water-Fill)"
        ),
        subtitle=(
            "Separates Active Consumed Cost from Unallocated Idle Cost and eliminates unweighted "
            "raw-token distortion across reasoning-heavy vs cache-heavy tenants."
        ),
        table=TableView(
            headers=(
                TableHeaderView("Tenant / BU"),
                TableHeaderView("Weighted Burndown Tokens", numeric=True),
                TableHeaderView("Weighted Share", numeric=True),
                TableHeaderView("Raw Token Share", numeric=True),
                TableHeaderView("FOCUS Active Cost", numeric=True),
                TableHeaderView("Unweighted Raw Cost", numeric=True),
                TableHeaderView("Raw Distortion", numeric=True),
                TableHeaderView("Naive Full-Pool Cost", numeric=True),
                TableHeaderView("Idle Penalty Avoided", numeric=True),
            ),
            rows=tuple(
                (
                    CellView(code=t.tenant),
                    CellView(text=f"{t.consumed_burndown_tokens:,.0f}", numeric=True),
                    CellView(text=f"{t.usage_share_pct:.1f}%", numeric=True),
                    CellView(text=f"{t.unweighted_raw_token_share_pct:.1f}%", numeric=True),
                    CellView(
                        text=f"${t.active_allocated_cost_usd:,.2f}", numeric=True, strong=True
                    ),
                    CellView(text=f"${t.unweighted_raw_token_cost_usd:,.2f}", numeric=True),
                    CellView(text=f"${t.raw_token_distortion_usd:+,.2f}", numeric=True),
                    CellView(text=f"${t.naive_full_pool_cost_usd:,.2f}", numeric=True),
                    CellView(
                        text=f"${t.idle_penalty_avoided_usd:,.2f}",
                        numeric=True,
                        tone="success",
                    ),
                )
                for t in report.chargeback.tenants
            ),
        ),
    )
    finops_rows: list[CardRowView] = []
    if pool_detail_row is not None:
        finops_rows.append(pool_detail_row)
    finops_rows.extend((CardRowView((sched_card,)), CardRowView((cb_card,))))
    return TabView(
        panel_id="tab-finops",
        button_label="4. FOCUS 1.2 FinOps, EDF Scheduler & 5 KPIs",
        kpis=finops_kpis,
        rows=tuple(finops_rows),
    )
