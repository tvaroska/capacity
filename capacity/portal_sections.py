"""Section HTML builders for the GM3 Light 4-Tab Web Workbench (`portal_sections.py`)."""

from __future__ import annotations

import html

from capacity.model_families import normalize_model_id
from capacity.models import GatewayConfigBundle, GatewayConfigRequest, RecommendationResponse
from capacity.scenario import (
    ModelInventoryAnnotation,
    ShowcaseReport,
    ShowcaseScenario,
)


def lifecycle_badge(track: str | None) -> str:
    if track == "long_term_ga":
        return "<span class='badge badge-green'>Long-Term GA (12m+ SLA)</span>"
    if track == "short_term_ga":
        return "<span class='badge badge-warn'>Short-Term GA (Eval/Pilot)</span>"
    if track == "preview":
        return "<span class='badge badge-warn'>Preview (Non-Prod)</span>"
    if track == "legacy_sunset":
        return "<span class='badge badge-danger'>Legacy / Migrate</span>"
    return "<span class='badge badge-blue'>Standard GA</span>"


def renewal_cell(ann: ModelInventoryAnnotation | None) -> str:
    if ann is None or ann.renewal_mode in (None, "none"):
        return "<span class='badge badge-blue'>PayGo / Unreserved</span>"
    if ann.renewal_mode == "fixed_term":
        exp = html.escape(ann.expiry_date or "fixed")
        return f"<span class='badge badge-danger'>Fixed &rarr; {exp}</span>"
    if ann.renewal_mode == "mixed":
        exp = html.escape(ann.expiry_date or "term")
        fixed = ann.expiring_fixed_gsus or 0
        return f"<span class='badge badge-warn'>{fixed} Fixed ({exp}) + Auto</span>"
    return "<span class='badge badge-green'>Auto-Renew (1m)</span>"


def lookup_annotation(
    annotations: dict[str, ModelInventoryAnnotation],
    model: str,
    region: str | None,
) -> ModelInventoryAnnotation | None:
    loc = region or "global"
    for candidate in (f"{model}@{loc}", f"{normalize_model_id(model)}@{loc}", model):
        if candidate in annotations:
            return annotations[candidate]
    return None


def compute_mean_duty_pct(recs: RecommendationResponse) -> float:
    if not recs.models:
        return 0.0
    return sum(m.duty_cycle_pct for m in recs.models) / len(recs.models)


def render_sizing_kpis(report: ShowcaseReport) -> tuple[str, str]:
    scenario = report.scenario
    mean_duty = compute_mean_duty_pct(report.gsu_recommendations)
    duty_val = f"{mean_duty:.1f}"
    wave_lbl = html.escape(scenario.presentation.wave_badge_label)

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
        exp_label = html.escape(exp_dates[0] if exp_dates else "Active Term")
        kpis_html = (
            f"<div class='kpi-grid'>"
            f"<div class='kpi-card'><div class='kpi-label'>Active vs 24x7 Duty Cycle</div>"
            f"<div class='kpi-value'>{duty_val}%</div>"
            f"<span class='badge badge-blue'>{wave_lbl}</span>"
            f"<div class='kpi-caption'>Sizes to active weekday peaks + Priority spillover"
            f"</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Contracted Managed PT Fleet</div>"
            f"<div class='kpi-value'>{total_gsus:,} GSUs</div>"
            f"<span class='badge badge-green'>0% HTTP 429 Inside PT Ceiling</span>"
            f"<div class='kpi-caption'>Pooled across 1P Gemini &amp; 3P Claude reservations"
            f"</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Spillover &amp; Throttling</div>"
            f"<div class='kpi-value'>{total_429:,} HTTP 429s</div>"
            f"<span class='badge badge-warn'>{total_spill:,} On-Demand Spillover Calls</span>"
            f"<div class='kpi-caption'>Route interactive spillover to 1.8x Priority PayGo"
            f"</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Fixed-Term Renewal Cliff</div>"
            f"<div class='kpi-value'>{expiring_gsus:,} GSUs</div>"
            f"<span class='badge badge-danger'>Expires {exp_label}</span>"
            f"<div class='kpi-caption'>Right-size onto Long-Term GA models before expiry"
            f"</div></div>"
            f"</div>"
        )
        return kpis_html, ""

    sh_d = report.self_hosted_audit
    disagg_d = report.disaggregated_sizing
    if disagg_d is None:
        return "", ""
    disagg_req = scenario.self_hosted.disaggregated
    accel_type = html.escape(sh_d.accelerator_type)
    disagg_rec = html.escape(disagg_d.recommendation)
    if disagg_d.replica_delta > 0:
        disagg_badge_cls = "badge-green"
        disagg_badge_txt = f"Saves {disagg_d.replica_delta} Replica(s) vs Monolithic"
    else:
        disagg_badge_cls = "badge-warn"
        disagg_badge_txt = (
            f"Monolithic Cheaper ({disagg_d.monolithic_replicas} vs "
            f"{disagg_d.total_disaggregated_replicas} Reps)"
        )
    kpis_html = (
        f"<div class='kpi-grid'>"
        f"<div class='kpi-card'><div class='kpi-label'>Active vs 24x7 Duty Cycle</div>"
        f"<div class='kpi-value'>{duty_val}%</div>"
        f"<span class='badge badge-blue'>{wave_lbl}</span>"
        f"<div class='kpi-caption'>Prevents 24x7 zero-padding P50=0 under-sizing</div></div>"
        f"<div class='kpi-card'><div class='kpi-label'>Upfront GPU Quota Lock</div>"
        f"<div class='kpi-value' id='kpi-locked-gpus'>"
        f"{sh_d.locked_regional_accelerators} GPUs</div>"
        f"<span class='badge badge-danger'>"
        f"{sh_d.stranded_quota_accelerators} Stranded vs Min</span>"
        f"<div class='kpi-caption'>max_replica_count ({sh_d.max_replica_count}) &times; "
        f"{sh_d.accelerators_per_replica} {accel_type}</div></div>"
        f"<div class='kpi-card'><div class='kpi-label'>Disaggregated P/D Pool</div>"
        f"<div class='kpi-value' id='kpi-disagg-reps'>"
        f"{disagg_d.prefill_replicas}P + {disagg_d.decode_replicas}D</div>"
        f"<span class='badge {disagg_badge_cls}' id='kpi-disagg-badge'>"
        f"{disagg_badge_txt}</span>"
        f"<div class='kpi-caption'>Isolates TTFT prefill bursts from TPOT decode</div></div>"
        f"<div class='kpi-card'><div class='kpi-label'>Non-Prod Scale-to-Zero</div>"
        f"<div class='kpi-value'>${sh_d.scale_to_zero_nonprod_savings_usd:,.0f}/mo</div>"
        f"<span class='badge badge-green'>ScaleToZeroSpec (108h/wk idle)</span>"
        f"<div class='kpi-caption'>Eliminates overnight/weekend idle GPU burn</div></div>"
        f"</div>"
    )
    sh_section = (
        "<div class='card'>"
        "<h2>Interactive Disaggregated Prefill / Decode GPU Pool Sizer "
        "(llm-d / GKE Inference Gateway)</h2>"
        "<p class='sub'>Separate compute-bound Prefill replicas (TTFT SLA) from HBM "
        "bandwidth/KV-bound Decode replicas (TPOT SLA).</p>"
        "<div class='controls-grid'>"
        "<div class='field'><label for='pd-qps'>Peak Arrival QPS</label>"
        f"<input id='pd-qps' type='number' value='{disagg_req.peak_qps:g}' min='0.1' max='500' "
        "step='1' oninput='recalcDisagg()' /></div>"
        "<div class='field'><label for='pd-in'>Avg Input Tokens</label>"
        f"<input id='pd-in' type='number' value='{disagg_req.avg_input_tokens}' min='256' "
        "max='128000' step='256' oninput='recalcDisagg()' /></div>"
        "<div class='field'><label for='pd-out'>Avg Output Tokens</label>"
        f"<input id='pd-out' type='number' value='{disagg_req.avg_output_tokens}' min='32' "
        "max='16384' step='50' oninput='recalcDisagg()' /></div>"
        "<div class='field'><label for='pd-ttft'>Target TTFT (ms)</label>"
        f"<input id='pd-ttft' type='number' value='{disagg_req.target_ttft_ms:g}' min='50' "
        "max='2000' step='25' oninput='recalcDisagg()' /></div>"
        "</div>"
        f"<div id='pd-output' class='info-banner'>{disagg_rec}</div>"
        "</div>"
    )
    return kpis_html, sh_section


def render_gateway_top_section(
    gw: GatewayConfigBundle,
    gw_req: GatewayConfigRequest,
    scenario: ShowcaseScenario,
) -> str:
    if scenario.gateway.presentation_mode == "central_proxy":
        proxy_lbl = html.escape(scenario.gateway.proxy_label)
        brand = html.escape(scenario.presentation.platform_brand)
        return (
            f"<div class='kpi-grid'>"
            f"<div class='kpi-card'><div class='kpi-label'>100% Gateway Mediation</div>"
            f"<div class='kpi-value'>Single Choke Point</div>"
            f"<span class='badge badge-blue'>{proxy_lbl}</span>"
            f"<div class='kpi-caption'>HttpRetryOptions(attempts=1) prevents 9x retry storms"
            f"</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Per-Desk Burndown Isolation</div>"
            f"<div class='kpi-value'>Two-Phase Quota</div>"
            f"<span class='badge badge-green'>1x/0.1x/4x &le;200K &bull; "
            f"2x/0.2x/7.5x &gt;200K</span>"
            f"<div class='kpi-caption'>Spills bursting tenants to 1.8x Priority before pool "
            f"drain</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Prefix-Cache Checklist</div>"
            f"<div class='kpi-value'>0.77% &rarr; 89.9%</div>"
            f"<span class='badge badge-green'>Preserve thought_signature</span>"
            f"<div class='kpi-caption'>&ge;6,144 Flash prefix, Turn-0 anchor &amp; regional "
            f"pinning</div></div>"
            f"<div class='kpi-card'><div class='kpi-label'>Stateful API Boundary</div>"
            f"<div class='kpi-value'>Option A / Option B</div>"
            f"<span class='badge badge-blue'>Neutral Ports, Adapters &amp; Contracts</span>"
            f"<div class='kpi-caption'>Identical OTel &amp; FOCUS 1.2 schema across both "
            f"options</div></div>"
            f"</div>"
            f"<div class='two-col'>"
            f"<div class='card'><h2>Phase 1 Non-Breaking <code>/v1/chat/completions</code> "
            f"Upstream Contract ({brand})</h2>"
            f"<p class='sub'>Compiled upstream rules for {proxy_lbl} without changing "
            f"downstream application endpoints.</p>"
            f"<table><thead><tr><th>Control Point</th><th>Gateway Contract</th>"
            f"<th>Impact</th></tr></thead><tbody>"
            f"<tr><td><strong>1P Interactive Spillover</strong></td>"
            f"<td>Omit <code>X-Vertex-AI-LLM-Request-Type</code> + "
            f"<code>X-Vertex-AI-LLM-Shared-Request-Type: priority</code></td>"
            f"<td>0% PT 429s + 0.014% Priority spillover vs 2.58% sheddable PayGo</td></tr>"
            f"<tr><td><strong>Multi-Turn Reasoning Cache</strong></td>"
            f"<td>Pass <code>thought_signature</code> untouched + regional pool affinity</td>"
            f"<td>Prevents 0.77% cache collapse; sustains ~89.9% multi-turn hit rate</td></tr>"
            f"<tr><td><strong>Batch / Eval Isolation</strong></td>"
            f"<td><code>X-Vertex-AI-LLM-Request-Type: shared</code> + <code>flex</code> "
            f"(or night <code>dedicated</code>)</td>"
            f"<td>Isolates eval &amp; optimization bursts from daytime desk PT</td></tr>"
            f"<tr><td><strong>1P vs 3P Claude Parity</strong></td>"
            f"<td>3P uses <code>30s/60s</code> buckets, <code>1x/0.1x/1.25x/2x/5x</code> "
            f"&amp; <code>standard</code> spillover</td>"
            f"<td>Reconciles 1P &amp; 3P attribution via zero-payload "
            f"<code>inference_request</code> logs</td></tr>"
            f"</tbody></table></div>"
            f"<div class='card'><h2>Non-Prescriptive Stateful Evolution "
            f"(Option A vs Option B via Ports, Adapters and Contracts)</h2>"
            f"<p class='sub'>Keeps internal ownership between Central AI Platform and "
            f"{proxy_lbl} open while preserving unified auditability.</p>"
            f"<table><thead><tr><th>Dimension</th>"
            f"<th>Option A: Thin Stateless Gateway + Platform Spine</th>"
            f"<th>Option B: Dual-Lane Stateful Central Gateway</th></tr></thead><tbody>"
            f"<tr><td><strong>Session &amp; Tool State</strong></td>"
            f"<td>AI Platform owns session history &amp; MCP loops; gateway stays ~2ms "
            f"stateless</td>"
            f"<td>Gateway adds <code>/v1beta1/interactions</code> (<code>store=false</code> ZDR "
            f"or CMEK)</td></tr>"
            f"<tr><td><strong>Token Budgets &amp; Spillover</strong></td>"
            f"<td>Gateway enforces pool rate limits; Platform tracks per-agent budgets</td>"
            f"<td>Gateway enforces per-desk burndown budgets &amp; Priority headers "
            f"directly</td></tr>"
            f"<tr><td><strong>Audit &amp; FOCUS 1.2 Output</strong></td>"
            f"<td colspan='2'>Identical W3C <code>traceparent</code> OTel spans + zero-payload "
            f"<code>inference_request</code> join for Cost per Verified Outcome (CPVO)</td></tr>"
            f"</tbody></table></div>"
            f"</div>"
        )

    audit = gw.litellm_audit
    if audit.scale_status == "migrate_dataplane_to_envoy":
        status_cls = "badge-danger"
        status_txt = "Migrate Data Plane to Envoy AI Gateway"
        banner_cls = "danger-banner"
    elif audit.scale_status == "tune_required":
        status_cls = "badge-warn"
        status_txt = "Tune Required (&ge;500 RPS)"
        banner_cls = "warn-banner"
    else:
        status_cls = "badge-green"
        status_txt = "Healthy Scale Envelope"
        banner_cls = "info-banner"
    banner_msg = html.escape(
        audit.bottleneck_warnings[-1] if audit.bottleneck_warnings else status_txt
    )

    litellm_cfg = html.escape(gw.litellm_config_yaml)
    litellm_cb = html.escape(gw.litellm_burndown_callback_py)
    envoy_cfg = html.escape(gw.envoy_ai_gateway_yaml)
    epp_cfg = html.escape(gw.gke_epp_inference_pool_yaml)
    return (
        "<div class='kpi-grid'>"
        "<div class='kpi-card'><div class='kpi-label'>LiteLLM Prisma Engines</div>"
        f"<div class='kpi-value' id='gw-prisma-val'>{audit.total_prisma_rust_engines} Engines</div>"
        f"<span class='badge {status_cls}' id='gw-status-badge'>{status_txt}</span>"
        f"<div class='kpi-caption' id='gw-pg-caption'>"
        f"PgBouncer pool={audit.recommended_pgbouncer_pool_size}, "
        f"Postgres max_conns&ge;{audit.recommended_postgres_max_connections}"
        "</div></div>"
        "<div class='kpi-card'><div class='kpi-label'>Anti-Blackhole Cooldown</div>"
        f"<div class='kpi-value' id='gw-cooldown-val'>"
        f"{audit.anti_blackhole_cooldown_seconds}s Cooldown</div>"
        f"<span class='badge badge-green'>Matched to {gw.refill_window_seconds}s PT Bucket</span>"
        "<div class='kpi-caption'>Prevents 1s/10s micro-burst 429 from blackholing PT</div></div>"
        "<div class='kpi-card'><div class='kpi-label'>Envoy AI Gateway CEL</div>"
        "<div class='kpi-value'>Two-Phase RLS</div>"
        "<span class='badge badge-blue'>cost.request=256 + SSE CEL</span>"
        "<div class='kpi-caption'>Enforces &le;200K &amp; &gt;200K per-BU burndown at &gt;2,000 RPS"
        "</div></div>"
        "<div class='kpi-card'><div class='kpi-label'>GKE Inference Gateway (epp)</div>"
        "<div class='kpi-value'>Weight 4:2:2:1</div>"
        "<span class='badge badge-green'>Prefix + KV + Queue + LoRA</span>"
        "<div class='kpi-caption'>Eliminates 1/N GPU prefix-cache dilution</div></div>"
        "</div>"
        "<div class='card'><h2>Interactive LiteLLM Proxy High-QPS Scale &amp; "
        "Bottleneck Auditor</h2>"
        "<p class='sub'>Simulate target RPS, streaming SSE concurrency, and pod/worker topology "
        "to size PgBouncer pools and detect the Envoy data-plane crossover.</p>"
        "<div class='controls-grid'>"
        "<div class='field'><label for='gw-rps'>Target Gateway RPS</label>"
        f"<input id='gw-rps' type='number' value='{gw_req.target_rps}' min='10' max='10000' "
        "step='50' oninput='recalcGatewayAudit()' /></div>"
        "<div class='field'><label for='gw-sse'>Concurrent SSE Streams</label>"
        f"<input id='gw-sse' type='number' value='{gw_req.concurrent_sse_streams}' min='0' "
        "max='5000' step='50' oninput='recalcGatewayAudit()' /></div>"
        "<div class='field'><label for='gw-pods'>LiteLLM Pods</label>"
        f"<input id='gw-pods' type='number' value='{gw_req.pods}' min='1' max='100' step='1' "
        "oninput='recalcGatewayAudit()' /></div>"
        "<div class='field'><label for='gw-workers'>Uvicorn Workers / Pod</label>"
        f"<input id='gw-workers' type='number' value='{gw_req.uvicorn_workers_per_pod}' min='1' "
        "max='16' step='1' oninput='recalcGatewayAudit()' /></div>"
        "</div>"
        f"<div id='gw-audit-msg' class='{banner_cls}'>{banner_msg}</div></div>"
        "<div class='two-col'>"
        "<div class='card'><h2>1. LiteLLM Proxy Config (<code>litellm_config.yaml</code>)</h2>"
        "<p class='sub'>Tag-based PT &rarr; Priority 1.8x spillover, Non-Prod 0.5x Flex "
        "isolation, and anti-blackholing cooldowns.</p>"
        f"<pre><code>{litellm_cfg}</code></pre></div>"
        "<div class='card'><h2>2. LiteLLM Weighted Burndown Hook "
        "(<code>litellm_burndown_callback.py</code>)</h2>"
        "<p class='sub'>Enforces stateful Redis weighted burndown quotas, dynamic PT-bypass on "
        "burst, and 0ms <code>traffic_type</code> SpendLogs tagging.</p>"
        f"<pre><code>{litellm_cb}</code></pre></div></div>"
        "<div class='two-col'>"
        "<div class='card'><h2>3. Envoy AI Gateway CEL (<code>AIGatewayRoute</code> + "
        "<code>BackendTrafficPolicy</code>)</h2>"
        "<p class='sub'>Two-phase pre-flight floor (<code>cost=256</code>) + post-response SSE "
        "<code>type: CEL</code> per-BU burndown deduction.</p>"
        f"<pre><code>{envoy_cfg}</code></pre></div>"
        "<div class='card'><h2>4. GKE Inference Gateway (<code>InferencePool</code> + "
        "<code>EndpointPickerConfig</code>)</h2>"
        "<p class='sub'>Prefix-cache affinity and KV-cache utilization pod scoring with "
        "Critical/Sheddable admission.</p>"
        f"<pre><code>{epp_cfg}</code></pre></div></div>"
    )


def render_finops_pool_cards(report: ShowcaseReport) -> tuple[str, str]:
    scenario = report.scenario
    fpool = report.folder_pooling
    if scenario.finops.pooling_mode == "1p_vs_3p_hub":
        brand = html.escape(scenario.presentation.platform_brand)
        kpi_card = (
            "<div class='kpi-card'>"
            "<div class='kpi-label'>1P Gemini vs 3P Claude Parity</div>"
            "<div class='kpi-value'>GA Priority + RHPT</div>"
            "<span class='badge badge-blue'>1P Full Parity &bull; 3P Hub Bridge</span>"
            f"<div class='kpi-caption'>{brand} 1P supports Priority PayGo &amp; Folder RHPT</div>"
            "</div>"
        )
        detail = (
            f"<div class='card'><h2>1P {brand} vs 3P Anthropic Claude PT Governance &amp; "
            "Self-Service Chargeback Reconciliation</h2>"
            "<p class='sub'>Transparent capability matrix across merged FCFS host pools and "
            "zero-payload <code>inference_request</code> log exports.</p>"
            "<table><thead><tr><th>Capability</th>"
            f"<th>1P {brand} (Argon / 3.8 Flash / 3.5 Flash-Lite)</th>"
            "<th>3P Anthropic Claude PT</th></tr></thead><tbody>"
            "<tr><td><strong>Burst Spillover Tier</strong></td>"
            "<td>GA <code>1.8x Priority PayGo</code> (Rank 2 CRITICAL, 0.014% 429s)</td>"
            "<td>Standard On-Demand spillover (3P Priority PayGo tracked separately)</td></tr>"
            "<tr><td><strong>Pool Scope &amp; Restructure</strong></td>"
            "<td>Project or Shared Folder <code>RHPT</code>; self-service Console swaps</td>"
            "<td>Project-scoped <code>30s/60s</code> dual buckets (0% burst credit)</td></tr>"
            "<tr><td><strong>Per-Desk PT Chargeback</strong></td>"
            "<td><code>usage_metadata.traffic_type</code> + "
            "<code>inference_request</code> logs</td>"
            "<td>Zero-payload <code>inference_request</code> log join + "
            "gateway burndown ledger</td>"
            "</tr></tbody></table></div>"
        )
        return kpi_card, detail

    kpi_card = (
        "<div class='kpi-card'>"
        "<div class='kpi-label'>Shared Folder RHPT Pooling</div>"
        f"<div class='kpi-value'>{fpool.siloed_total_gsus} &rarr; "
        f"{fpool.pooled_folder_gsus} GSUs</div>"
        f"<span class='badge badge-blue'>Saves ${fpool.monthly_savings_usd:,.0f}/mo "
        f"(${fpool.annual_savings_usd:,.0f}/yr)</span>"
        "<div class='kpi-caption'>Follow-the-sun EMEA + AMER + APAC spoke pooling</div>"
        "</div>"
    )
    return kpi_card, ""
