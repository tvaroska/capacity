"""CLI table formatters and trace-file input helpers for `capacity.cli`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from capacity.core import (
    GCPQueryError,
    check_gcp_project_access,
    fetch_inventory_telemetry,
    fetch_throughput,
)
from capacity.models import FocusChargebackReport, OptimizeResponse, RecommendationResponse
from capacity.scenario import (
    FinOpsPoolingMode,
    GatewayPresentationMode,
    PlatformBrand,
    ShowcaseScenario,
    audit_scenario_public_safety,
    compile_scenario_from_telemetry,
    load_orders_overlay,
)
from capacity.self_hosted import SelfHostedEndpointAudit, load_inference_server_file


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
) -> tuple[dict[str, dict[datetime, int]], int]:
    raw_data = json.loads(trace_file.read_text(encoding="utf-8"))
    base_dt = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    total_minutes = days * 24 * 60
    if isinstance(raw_data, list):
        key = f"{default_model}@us-central1" if by_region else default_model
        series_map = {base_dt + timedelta(minutes=i): int(float(v)) for i, v in enumerate(raw_data)}
        return {key: series_map}, max(total_minutes, len(raw_data))
    if isinstance(raw_data, dict):
        sparse: dict[str, dict[datetime, int]] = {}
        for k, vals in raw_data.items():
            if isinstance(vals, list):
                sparse[str(k)] = {
                    base_dt + timedelta(minutes=i): int(float(v)) for i, v in enumerate(vals)
                }
        return sparse, total_minutes
    raise ValueError(f"Unsupported trace file structure in {trace_file}")


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


def print_chargeback_report(
    report: FocusChargebackReport, unit_name: str, unit_price: float
) -> None:
    typer.echo(
        f"=== FOCUS 1.2 Two-Pool Chargeback "
        f"({report.provisioned_gsus} {unit_name} @ ${unit_price:,.0f}/mo) ==="
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


def print_auth_check_report(report: dict[str, object]) -> None:
    auth_ok = bool(report.get("authenticated"))
    account = report.get("account") or "unauthenticated"
    typer.echo(
        f"=== GCP Monitoring Pre-Flight Check (authenticated={auth_ok}, caller={account}) ==="
    )
    projects = report.get("projects")
    if isinstance(projects, dict):
        for proj, info in projects.items():
            if isinstance(info, dict):
                status = "OK" if info.get("ok") else f"BLOCKED ({info.get('error', 'unknown')})"
                typer.echo(f"  - {proj}: {status}")
    instructions = report.get("instructions")
    if isinstance(instructions, list) and instructions:
        typer.echo("Remediation:")
        for item in instructions:
            typer.echo(f"  * {item}")


def print_collect_summary(
    out_path: Path,
    project_label: str,
    series_count: int,
    annotation_count: int,
) -> None:
    typer.echo(
        f"Compiled ShowcaseScenario ({project_label}): "
        f"{series_count} model series, {annotation_count} inventory annotations -> {out_path}"
    )


def execute_collect_command(
    *,
    proj_list: list[str],
    days: int,
    by_region: bool,
    logs_file: Path | None,
    orders_file: Path | None,
    project_label: str,
    gateway_mode: GatewayPresentationMode,
    pooling_mode: FinOpsPoolingMode,
    platform_brand: PlatformBrand,
    self_hosted: bool,
    check_auth: bool,
    strict_public_check: bool,
    out: Path | None,
    json_output: bool,
    or_exit: Callable[[Callable[[], object]], object],
    fail: Callable[[str], typer.Exit],
) -> None:
    if check_auth:
        report_obj = or_exit(lambda: asyncio.run(check_gcp_project_access(proj_list)))
        report = report_obj if isinstance(report_obj, dict) else {}
        if json_output:
            typer.echo(json.dumps(report, indent=2))
        else:
            print_auth_check_report(report)
        if not report.get("ok", False):
            raise typer.Exit(code=1)
        if out is None and logs_file is None:
            return

    if not proj_list and logs_file is None:
        raise fail("provide `--project <id>` and/or `--logs-file <path>` (or `--check-auth`).")

    sparse: dict[str, dict[datetime, int]] | None = None
    total_mins: int | None = None
    inventory: dict[str, dict[str, int]] | None = None
    if proj_list:
        try:
            sparse, total_mins = asyncio.run(
                fetch_throughput(project=proj_list, days=days, by_region=by_region)
            )
            inventory = asyncio.run(
                fetch_inventory_telemetry(project=proj_list, days=days, by_region=by_region)
            )
        except (GCPQueryError, ValueError) as exc:
            raise fail(str(exc)) from exc

    snap = (
        load_inference_server_file(logs_file, default_model="gemini-3.8-flash")
        if logs_file is not None
        else None
    )
    overlay = load_orders_overlay(orders_file) if orders_file is not None else None
    scenario: ShowcaseScenario = compile_scenario_from_telemetry(
        project_label=project_label,
        days=days,
        by_region=by_region,
        sparse_series=sparse,
        total_minutes=total_mins,
        inventory_telemetry=inventory,
        log_snapshot=snap,
        orders_overlay=overlay,
        gateway_mode=gateway_mode,
        pooling_mode=pooling_mode,
        platform_brand=platform_brand,
        self_hosted_enabled=self_hosted,
    )
    if strict_public_check:
        issues = audit_scenario_public_safety(scenario)
        if issues:
            raise fail("Scenario failed --strict-public-check:\n  " + "\n  ".join(issues))

    serialized = scenario.model_dump_json(indent=2)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(serialized + "\n", encoding="utf-8")
    if json_output or out is None:
        typer.echo(serialized)
    else:
        print_collect_summary(
            out,
            scenario.gsu_sizing.project_label,
            len(scenario.gsu_sizing.minute_tpm_series),
            len(scenario.gsu_sizing.inventory_annotations),
        )
