"""CLI table formatters and trace-file input helpers for `capacity.cli`."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from capacity.forecasting import minute_list_to_sparse
from capacity.models import (
    FocusChargebackReport,
    GatewayConfigBundle,
    OptimizeResponse,
    RecommendationResponse,
    ResidualScheduleReport,
    SeriesKey,
)
from capacity.scenario import ShowcaseReport
from capacity.self_hosted import SelfHostedEndpointAudit


def parse_folder_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    stripped = raw.strip()
    if stripped.startswith("{"):
        loaded = json.loads(stripped)
        return {str(k): str(v) for k, v in loaded.items()}
    mapping: dict[str, str] = {}
    for item in stripped.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            mapping[k.strip()] = v.strip()
    return mapping


def load_sparse_from_trace_file(
    trace_file: Path,
    days: int,
    by_region: bool,
    default_model: str = "gemini-3.8-flash",
) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
    raw_data = json.loads(trace_file.read_text(encoding="utf-8"))
    total_minutes = days * 24 * 60
    if isinstance(raw_data, list):
        skey = SeriesKey(model=default_model, region="us-central1" if by_region else None)
        return {skey: minute_list_to_sparse(raw_data)}, max(total_minutes, len(raw_data))
    if isinstance(raw_data, dict):
        sparse: dict[SeriesKey, dict[datetime, int]] = {}
        max_len = 0
        for raw_k, vals in raw_data.items():
            if isinstance(vals, list):
                k_str = str(raw_k)
                max_len = max(max_len, len(vals))
                parsed = SeriesKey.parse(k_str, by_region=by_region and "@" in k_str)
                bucket = sparse.setdefault(parsed, {})
                for dt, val in minute_list_to_sparse(vals).items():
                    bucket[dt] = bucket.get(dt, 0) + val
        return sparse, max(total_minutes, max_len)
    raise ValueError(f"Unsupported trace file structure in {trace_file}")


def print_demo_report(report: ShowcaseReport, html_out: Path | None) -> None:
    kpis = report.finops_kpis
    fpool = report.folder_pooling
    scav = report.scavenger
    disagg = report.disaggregated_sizing
    audit = report.gateway.litellm_audit

    typer.echo(f"=== Capacity v{report.version} — End-to-End Executive Showcase Scorecard ===")
    typer.echo(
        f"1. FinOps for AI KPIs:    CPVO=${kpis.cpvo_usd:.4f}/outcome | "
        f"CPWT_1M=${kpis.cpwt_1m_usd:.4f} | TER={kpis.ter_pct:.1f}% | "
        f"CSR={kpis.csr_pct:.1f}% | ECUR_168h={kpis.ecur_168h_pct:.1f}%"
    )
    typer.echo(
        f"2. Folder RHPT Pooling:   Siloed={fpool.siloed_total_gsus} GSUs -> "
        f"Pooled={fpool.pooled_folder_gsus} GSUs "
        f"(Saved={fpool.stranded_gsus_saved} GSUs / ${fpool.monthly_savings_usd:,.0f}/mo)"
    )
    typer.echo(
        f"3. Batch 0.5x Trap Saved: ${scav.double_payment_waste_avoided_usd:,.2f}/mo "
        f"(Lifts PT utilization {scav.daytime_active_utilization_pct:.0f}% -> "
        f"{scav.effective_utilization_after_scavenging_pct:.1f}%)"
    )
    if disagg is not None:
        typer.echo(
            f"4. Disaggregated P/D:     {disagg.prefill_replicas} Prefill + "
            f"{disagg.decode_replicas} Decode Replicas "
            f"(vs {disagg.monolithic_replicas} Monolithic, "
            f"saves {disagg.replica_delta} replica[s])"
        )
    else:
        typer.echo("4. Disaggregated P/D:     disabled (--no-self-hosted)")
    typer.echo(
        f"5. Multi-Gateway Audit:   LiteLLM ({audit.total_prisma_rust_engines} "
        f"Prisma engines, cooldown={audit.anti_blackhole_cooldown_seconds}s, "
        f"status={audit.scale_status}) + Envoy CEL + GKE epp"
    )
    if html_out is not None:
        typer.echo(f"6. HTML Scorecard Saved:  {html_out}")


def print_recommendations_table(
    response: RecommendationResponse,
    percentiles: list[int],
    forecast_hours: int = 24,
) -> None:
    if not response.models:
        typer.echo("No token throughput data found for the specified parameters.")
        return

    pct_keys = [f"P{p}" for p in percentiles]
    unit_label = "Reps" if response.mode == "self-hosted" else "GSU"
    headers = ["Model"]
    if any(m.folder for m in response.models):
        headers.append("Folder")
    if response.by_region:
        headers.append("Region")
    headers.extend([f"TPM/{unit_label}", "Duty%"])
    for k in pct_keys:
        headers.extend([f"{k} TPM", f"{k} {unit_label}"])
    fh_label = f"Forecast+{forecast_hours}h TPM" if forecast_hours > 0 else "Forecast+0h TPM"
    headers.extend([fh_label, f"Forecast {unit_label}"])

    rows: list[list[str]] = []
    for m in response.models:
        cap_str = f"{m.gsu_capacity_tpm:,}" if m.gsu_capacity_tpm else "?"
        row = [m.model]
        if any(mod.folder for mod in response.models):
            row.append(m.folder or "-")
        if response.by_region:
            row.append(m.region or "global")
        row.extend([cap_str, f"{m.duty_cycle_pct:.1f}%"])
        for k in pct_keys:
            d = m.percentiles[k]
            gsu_str = str(d.recommended_gsus) if m.gsu_capacity_tpm else "?"
            row.extend([f"{d.tpm:,}", gsu_str])
        row.extend(
            [
                f"{m.forecast_24h_peak_tpm:,}",
                str(m.forecast_24h_recommended_gsus) if m.gsu_capacity_tpm else "?",
            ]
        )
        rows.append(row)

    col_widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" if i == 0 else f"{{:>{w}}}" for i, w in enumerate(col_widths))
    typer.echo(fmt.format(*headers))
    typer.echo("  ".join("—" * w for w in col_widths))
    for row in rows:
        typer.echo(fmt.format(*row))


def print_self_hosted_audit(audit: SelfHostedEndpointAudit) -> None:
    typer.echo(
        f"=== Self-Hosted GPU Endpoint Audit ({audit.profile_id} @ {audit.machine_type}) ==="
    )
    typer.echo(
        f"Upfront GPU Quota Lock: {audit.locked_regional_accelerators}x {audit.accelerator_type} "
        f"(min={audit.min_replica_count} reps -> {audit.active_baseline_accelerators} GPUs, "
        f"stranded lock={audit.stranded_quota_accelerators} GPUs)"
    )
    typer.echo(
        f"Autoscaler Illusion:    Duty Cycle="
        f"{audit.observed_accelerator_duty_cycle_pct:.1f}% (<60%) "
        f"vs vLLM KV-Cache={audit.observed_vllm_kv_cache_usage_pct:.1f}% "
        f"(Wait Queue={audit.observed_vllm_requests_waiting:.0f}, "
        f"Illusion={audit.duty_cycle_illusion_detected})"
    )
    typer.echo(
        f"Newsvendor Replicas:    {audit.optimal_reserved_replicas} Reserved CUD Reps "
        f"(P99={audit.p99_peak_replicas} Reps, "
        f"Break-Even Util={audit.breakeven_replica_utilization_pct:.1f}%)"
    )
    typer.echo(
        f"Monthly Hybrid Cost:    ${audit.total_hybrid_monthly_cost_usd:,.2f} "
        f"(Reserved=${audit.reserved_base_monthly_cost_usd:,.2f} + "
        f"Elastic Burst=${audit.elastic_burst_monthly_cost_usd:,.2f}; "
        f"Non-Prod ScaleToZero Saves=${audit.scale_to_zero_nonprod_savings_usd:,.2f}/mo)"
    )
    pc = audit.prefix_cache
    typer.echo(
        f"Replica KV Dilution:    Sticky={pc.sticky_gsus} Reps vs "
        f"Round-Robin({pc.num_round_robin_endpoints})={pc.round_robin_gsus} Reps "
        f"at an assumed {pc.requests_per_second:g} request/s "
        f"(Effective Hit={pc.effective_cache_hit_rate * 100:.1f}%, "
        f"Savings={pc.gsu_savings_pct:.1f}%)"
    )


def print_gsu_optimize_report(
    resp: OptimizeResponse,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    thoughts_tokens: int,
) -> None:
    c = resp.commitment
    mb = resp.micro_burst
    pc = resp.prefix_cache
    typer.echo(f"=== Newsvendor Hybrid Commitment ({resp.normalized_model}) ===")
    typer.echo(
        f"Optimal PT Base: {c.optimal_gsus} GSUs "
        f"(Quantile={c.critical_quantile:.2f}, Break-Even Util={c.breakeven_utilization_pct:.1f}%)"
    )
    typer.echo(
        f"Monthly Cost:    ${c.total_hybrid_monthly_cost_usd:,.2f} "
        f"(PT=${c.pt_base_monthly_cost_usd:,.2f} + Spillover=${c.spillover_monthly_cost_usd:,.2f})"
    )
    typer.echo(
        f"Savings:         ${c.savings_vs_paygo_usd:,.2f}/mo vs 100% PayGo "
        f"({c.savings_vs_paygo_pct:.1f}%), "
        f"${c.savings_vs_p99_pt_usd:,.2f}/mo vs P99 PT ({c.savings_vs_p99_pt_pct:.1f}%)"
    )
    typer.echo(
        f"Request Weight:  {resp.weighted_burndown_tokens_per_request:,.0f} "
        f"weighted burndown tokens "
        f"(in={input_tokens:,}, cached={cached_tokens:,}, "
        f"out={output_tokens:,}, thoughts={thoughts_tokens:,})"
    )
    if mb is None:
        typer.echo(
            "Micro-Burst:     not evaluated (pass --sixty-sec-tpm or a measured --peak-*-tps)"
        )
    else:
        window_desc = (
            f"{mb.refill_window_seconds}s/{mb.long_window_seconds}s dual bucket"
            if mb.long_window_seconds is not None
            else f"{mb.refill_window_seconds}s window"
        )
        typer.echo(
            f"Micro-Burst:     {window_desc} | "
            f"60s Util={mb.apparent_60s_utilization_pct:.1f}% | "
            f"Peak Window Util={mb.peak_window_utilization_pct:.1f}% | "
            f"Spillover={mb.triggers_429_or_spillover} | "
            f"Illusion={mb.illusion_detected}"
        )
    typer.echo(
        f"Prefix Cache:    Sticky={pc.sticky_gsus} GSUs vs "
        f"Round-Robin({pc.num_round_robin_endpoints})={pc.round_robin_gsus} GSUs "
        f"(Effective Hit={pc.effective_cache_hit_rate * 100:.1f}%, "
        f"Wasted={pc.wasted_gsus} GSUs, Savings={pc.gsu_savings_pct:.1f}%)"
    )


def print_gateway_report(bundle: GatewayConfigBundle, target: str) -> None:
    audit = bundle.litellm_audit
    typer.echo(
        f"=== Multi-Gateway Configuration & Scale Audit "
        f"({bundle.normalized_model}, {bundle.provisioned_gsus} GSUs) ==="
    )
    typer.echo(
        f"LiteLLM Scale Audit: {audit.total_prisma_rust_engines} Prisma engines "
        f"({audit.pods} pods * {audit.uvicorn_workers_per_pod} workers) | "
        f"PgBouncer pool={audit.recommended_pgbouncer_pool_size} | "
        f"Cooldown={audit.anti_blackhole_cooldown_seconds}s | Status={audit.scale_status}"
    )
    if target in ("litellm", "all"):
        typer.echo("\n--- [1] LiteLLM Proxy (litellm_config.yaml) ---")
        typer.echo(bundle.litellm_config_yaml.rstrip())
        typer.echo(
            "\n--- [2] LiteLLM Weighted Burndown Callback (litellm_burndown_callback.py) ---"
        )
        typer.echo(bundle.litellm_burndown_callback_py.rstrip())
    if target in ("envoy", "all"):
        typer.echo("\n--- [3] Envoy AI Gateway CEL (AIGatewayRoute + BackendTrafficPolicy) ---")
        typer.echo(bundle.envoy_ai_gateway_yaml.rstrip())
    if target in ("epp", "all"):
        typer.echo("\n--- [4] GKE Inference Gateway (InferencePool + EndpointPickerConfig) ---")
        typer.echo(bundle.gke_epp_inference_pool_yaml.rstrip())


def print_schedule_report(report: ResidualScheduleReport, normalized_model: str) -> None:
    typer.echo(
        f"=== 3-Tier Residual Capacity Schedule "
        f"({normalized_model}, {report.provisioned_gsus} GSUs) ==="
    )
    typer.echo(
        f"Residual PT Budget ({report.horizon_hours}h): "
        f"{report.total_residual_pt_tokens:,.0f} burndown tokens"
    )
    typer.echo(
        f"Tier-1 Admitted (EDF PT):    {report.admitted_deadline_tokens:,.0f} tokens ($0 marginal)"
    )
    typer.echo(
        f"Tier-1 Spilled (0.5x Flex):  {report.spilled_deadline_tokens:,.0f} tokens "
        f"(${report.flex_spillover_cost_usd:,.2f})"
    )
    typer.echo(
        f"Tier-2 Evergreen Water-Fill: {report.evergreen_waterfilled_tokens:,.0f} tokens ($0)"
    )
    for j in report.jobs:
        typer.echo(
            f"  - {j.job_id}: status={j.status} | deadline=T+{j.deadline_hour}h | "
            f"density=${j.value_density_per_1m_tokens:.2f}/1M | cost=${j.marginal_cost_usd:.2f}"
        )


def print_chargeback_report(report: FocusChargebackReport) -> None:
    unit_name = "GPU Replicas" if report.mode == "self-hosted" else "GSUs"
    typer.echo(
        f"=== FOCUS 1.2 Two-Pool Chargeback "
        f"({report.provisioned_gsus} {unit_name} @ ${report.unit_price_usd:,.0f}/mo) ==="
    )
    typer.echo(f"Total Monthly Commitment:      ${report.total_commitment_cost_usd:,.2f}")
    typer.echo(
        f"Active Consumed Capacity Pool: ${report.active_consumed_cost_usd:,.2f} "
        f"({report.active_utilization_pct:.1f}% util)"
    )
    wf_gain = report.effective_utilization_with_waterfill_pct - report.active_utilization_pct
    eff_util = report.effective_utilization_with_waterfill_pct
    typer.echo(
        f"Reclaimed Batch Water-Fill:    ${report.reclaimed_waterfill_cost_usd:,.2f} "
        f"(+{wf_gain:.1f}% util -> {eff_util:.1f}% effective)"
    )
    typer.echo(f"Net Unallocated Idle Pool:     ${report.net_unallocated_idle_cost_usd:,.2f}\n")
    headers = [
        "Tenant",
        "Weighted %",
        "Raw Tok %",
        "FOCUS Active $",
        "Unweighted Raw $",
        "Naive Full-Pool $",
        "Idle Tax Avoided $",
    ]
    rows = [
        [
            t.tenant,
            f"{t.usage_share_pct:.1f}%",
            f"{t.unweighted_raw_token_share_pct:.1f}%",
            f"${t.active_allocated_cost_usd:,.2f}",
            f"${t.unweighted_raw_token_cost_usd:,.2f}",
            f"${t.naive_full_pool_cost_usd:,.2f}",
            f"${t.idle_penalty_avoided_usd:,.2f}",
        ]
        for t in report.tenants
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" if i == 0 else f"{{:>{w}}}" for i, w in enumerate(widths))
    typer.echo(fmt.format(*headers))
    typer.echo("  ".join("—" * w for w in widths))
    for r in rows:
        typer.echo(fmt.format(*r))
