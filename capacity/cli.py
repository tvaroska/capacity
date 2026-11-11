"""Typer CLI for `capacity`: sizing, micro-burst/KV physics, multi-gateway, and FOCUS 1.2."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import (
    build_response,
    load_gsu_config,
    normalize_model_id,
    require_capacity,
)
from capacity.core import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    GCPQueryError,
    build_demo_throughput,
    fetch_throughput,
)
from capacity.finops import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.gateway import generate_gateway_bundle
from capacity.models import (
    DeadlineJobSpec,
    OptimizeResponse,
    RecommendationResponse,
)
from capacity.portal import build_showcase_summary_payload, render_showcase_portal_html
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    load_inference_server_file,
    resolve_self_hosted_profile,
    size_disaggregated_prefill_decode,
)

load_dotenv()

app = typer.Typer(
    help=(
        "Gemini Platform GSU & Self-Hosted Model Capacity Planner, "
        "Micro-Burst Simulator, Multi-Gateway Config Generator, and FOCUS 1.2 FinOps Showcase."
    ),
    add_completion=False,
)


class OutputFormat(StrEnum):
    table = "table"
    json = "json"


class CapacityMode(StrEnum):
    gsu = "gsu"
    self_hosted = "self-hosted"


class GatewayTarget(StrEnum):
    litellm = "litellm"
    envoy = "envoy"
    epp = "epp"
    all = "all"


# Default synthetic 30-day TPM distribution (12 representative buckets in TPM)
DEFAULT_SYNTHETIC_TPM_SERIES: list[float] = [
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
]

# Default synthetic 24-hour multi-tenant TPM profile (8 active business hours + 16 off-peak hours)
DEFAULT_SYNTHETIC_TENANT_TPM: dict[str, list[float]] = {
    "claims-triage": [350_000.0] * 8 + [40_000.0] * 16,
    "underwriting-copilot": [250_000.0] * 8 + [30_000.0] * 16,
    "policy-audit": [150_000.0] * 8 + [15_000.0] * 16,
}


def _or_exit[T](fn: Callable[[], T]) -> T:
    """Turn bad model/profile/config inputs into a one-line CLI error instead of a traceback."""
    try:
        return fn()
    except ValueError as exc:  # includes UnknownModelError
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _parse_folder_map(raw: str | None) -> dict[str, str]:
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


def _load_sparse_from_trace_file(
    trace_file: Path,
    days: int,
    by_region: bool,
) -> tuple[dict[str, dict[datetime, int]], int]:
    raw_data = json.loads(trace_file.read_text(encoding="utf-8"))
    base_dt = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    total_minutes = days * 24 * 60
    if isinstance(raw_data, list):
        key = "gemini-3.8-flash@us-central1" if by_region else "gemini-3.8-flash"
        series_map = {
            base_dt + timedelta(minutes=i): int(float(v))
            for i, v in enumerate(raw_data)
        }
        return {key: series_map}, max(total_minutes, len(raw_data))
    if isinstance(raw_data, dict):
        sparse: dict[str, dict[datetime, int]] = {}
        for k, vals in raw_data.items():
            if isinstance(vals, list):
                sparse[str(k)] = {
                    base_dt + timedelta(minutes=i): int(float(v))
                    for i, v in enumerate(vals)
                }
        return sparse, total_minutes
    raise ValueError(f"Unsupported trace file structure in {trace_file}")


def _print_table(
    response: RecommendationResponse,
    percentiles: list[int],
) -> None:
    if not response.models:
        typer.echo("No token throughput data found for the specified parameters.")
        return

    pct_keys = [f"P{p}" for p in percentiles]
    unit_label = "Reps" if response.mode == "self-hosted" else "GSU"
    cap_header = f"TPM/{unit_label}"
    headers = ["Model"]
    if any(m.folder for m in response.models):
        headers.append("Folder")
    if response.by_region:
        headers.append("Region")
    headers.extend([cap_header, "Duty%"])
    for k in pct_keys:
        headers.extend([f"{k} TPM", f"{k} {unit_label}"])
    headers.extend(["Forecast+24h TPM", f"Forecast {unit_label}"])

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

    col_widths = [
        max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)
    ]
    fmt = "  ".join(
        f"{{:<{w}}}" if i == 0 else f"{{:>{w}}}" for i, w in enumerate(col_widths)
    )
    typer.echo(fmt.format(*headers))
    typer.echo("  ".join("—" * w for w in col_widths))
    for row in rows:
        typer.echo(fmt.format(*row))


@app.command()
def demo(
    html_out: Annotated[
        Path | None,
        typer.Option(
            "--html-out",
            help="Optional path to write the self-contained GM3 Light interactive HTML scorecard",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run the end-to-end Showcase demo across all 4 planes (zero cloud credentials needed)."""
    if html_out is not None:
        html_out.parent.mkdir(parents=True, exist_ok=True)
        html_out.write_text(render_showcase_portal_html(), encoding="utf-8")

    payload = build_showcase_summary_payload()
    if output_format == OutputFormat.json:
        typer.echo(json.dumps(payload, indent=2))
        return

    kpis = compute_finops_kpis()
    scav = evaluate_batch_scavenger_economics()
    fpool = evaluate_folder_pooling(
        hourly_tpm_by_project={
            "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
            "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
            "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
        }
    )
    disagg = size_disaggregated_prefill_decode()
    gw = generate_gateway_bundle()

    typer.echo("=== Capacity v0.3.0 — End-to-End Executive Showcase Scorecard ===")
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
    typer.echo(
        f"4. Disaggregated P/D:     {disagg.prefill_replicas} Prefill + "
        f"{disagg.decode_replicas} Decode Replicas "
        f"(vs {disagg.monolithic_replicas} Monolithic, saves {disagg.replica_delta} replica[s])"
    )
    typer.echo(
        f"5. Multi-Gateway Audit:   LiteLLM ({gw.litellm_audit.total_prisma_rust_engines} "
        f"Prisma engines, cooldown={gw.litellm_audit.anti_blackhole_cooldown_seconds}s, "
        f"status={gw.litellm_audit.scale_status}) + Envoy CEL + GKE epp"
    )
    if html_out is not None:
        typer.echo(f"6. HTML Scorecard Saved:  {html_out}")


@app.command()
def recommend(
    project: Annotated[
        list[str] | None,
        typer.Option(
            help="GCP project ID (repeatable, or set GOOGLE_CLOUD_PROJECT)",
            envvar="GOOGLE_CLOUD_PROJECT",
        ),
    ] = None,
    days: Annotated[int, typer.Option(help="Days of history")] = DEFAULT_DAYS,
    percentile: Annotated[
        list[int] | None, typer.Option(help="Percentile (repeatable)")
    ] = None,
    model: Annotated[
        list[str] | None, typer.Option(help="Filter by model (repeatable)")
    ] = None,
    region: Annotated[
        list[str] | None, typer.Option(help="Filter by region (repeatable)")
    ] = None,
    active_only: Annotated[
        bool,
        typer.Option(
            "--active-only/--all-hours",
            help="Use active business-hours minutes only instead of 24x7 zero-padded minutes",
        ),
    ] = False,
    by_region: Annotated[
        bool,
        typer.Option(
            "--by-region",
            help="Break down throughput and recommendations per GCP region",
        ),
    ] = False,
    mode: Annotated[
        CapacityMode,
        typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'"),
    ] = CapacityMode.gsu,
    demo: Annotated[
        bool,
        typer.Option(
            "--demo",
            help="Run offline with built-in 8x5 multi-region enterprise telemetry trace",
        ),
    ] = False,
    trace_file: Annotated[
        Path | None,
        typer.Option("--trace-file", help="Offline JSON trace file of minute TPM values"),
    ] = None,
    vllm_file: Annotated[
        Path | None,
        typer.Option(
            "--vllm-file",
            help="Phase 2 vLLM/SGLang Prometheus /metrics snapshot or JSONL request log file",
        ),
    ] = None,
    folder_map: Annotated[
        str | None,
        typer.Option(
            "--folder-map",
            help="Optional project-to-folder map (e.g. 'proj-a=claims,proj-b=underwriting')",
        ),
    ] = None,
    forecast_hours: Annotated[
        int,
        typer.Option("--forecast-hours", help="Holt double-exponential smoothing horizon"),
    ] = 24,
    html_out: Annotated[
        Path | None,
        typer.Option(
            "--html-out",
            help="Optional path to write the self-contained GM3 Light interactive HTML scorecard",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Query token throughput (or run offline `--demo`) and recommend GSUs or GPU replicas."""
    pcts = sorted(percentile) if percentile else DEFAULT_PERCENTILES
    for p in pcts:
        if not 0 <= p <= 100:
            typer.echo(
                f"Error: percentile must be between 0 and 100, got {p}", err=True
            )
            raise typer.Exit(code=1)

    if days < 1:
        typer.echo("Error: days must be >= 1", err=True)
        raise typer.Exit(code=1)

    env_proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
    proj_list = project or ([env_proj] if env_proj else [])
    proj_label = ",".join(proj_list) if proj_list else "demo-project"

    if vllm_file is not None:
        snap = load_inference_server_file(vllm_file)
        base_dt = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
        sparse = {
            snap.model: {
                base_dt + timedelta(minutes=i): round(v)
                for i, v in enumerate(snap.tpm_series)
            }
        }
        total_minutes = max(days * 24 * 60, len(snap.tpm_series))
    elif trace_file is not None:
        sparse, total_minutes = _load_sparse_from_trace_file(trace_file, days, by_region)
    elif demo:
        sparse, total_minutes = build_demo_throughput(
            days=days, by_region=by_region, mode=mode.value
        )
    else:
        if not proj_list:
            typer.echo(
                "Error: provide `--project <id>` or pass `--demo` / `--trace-file` "
                "for offline mode.",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            sparse, total_minutes = asyncio.run(
                fetch_throughput(
                    proj_list,
                    days,
                    model,
                    region,
                    by_region=by_region,
                    folder_map=_parse_folder_map(folder_map),
                    mode=mode.value,
                )
            )
        except GCPQueryError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    resp = build_response(
        proj_label,
        days,
        sparse,
        total_minutes,
        pcts,
        active_only=active_only,
        by_region=by_region,
        forecast_steps=forecast_hours,
        mode=mode.value,
    )

    if html_out is not None:
        html_out.parent.mkdir(parents=True, exist_ok=True)
        html_out.write_text(render_showcase_portal_html(), encoding="utf-8")

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
    else:
        _print_table(resp, pcts)


@app.command()
def optimize(
    model: Annotated[
        str, typer.Option(help="Model or self-hosted profile ID")
    ] = "gemini-3.8-flash",
    mode: Annotated[
        CapacityMode,
        typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'"),
    ] = CapacityMode.gsu,
    trace_file: Annotated[
        Path | None,
        typer.Option(
            "--trace-file",
            help="Optional JSON file containing a list of per-minute TPM floats",
        ),
    ] = None,
    vllm_file: Annotated[
        Path | None,
        typer.Option(
            "--vllm-file",
            help="Phase 2 vLLM/SGLang Prometheus /metrics or JSONL log file",
        ),
    ] = None,
    gsu_price: Annotated[
        float, typer.Option(help="Monthly price per 1 GSU in USD")
    ] = 2_000.0,
    paygo_per_1m: Annotated[
        float, typer.Option(help="Base Standard PayGo price per 1M burndown tokens in USD")
    ] = 0.50,
    spillover_tier: Annotated[
        str, typer.Option(help="Spillover tier: priority (1.8x), standard (1.0x), flex (0.5x)")
    ] = "priority",
    fsp_discount_pct: Annotated[
        float, typer.Option(help="Flexible Savings Plan (FSP) discount percentage (0-50)")
    ] = 0.0,
    sixty_sec_tpm: Annotated[
        float | None,
        typer.Option(help="60-second average TPM for micro-burst window audit"),
    ] = None,
    peak_1s_tps: Annotated[
        float | None,
        typer.Option(help="Peak 1-second TPS burst for micro-burst window audit"),
    ] = None,
    peak_10s_tps: Annotated[
        float | None,
        typer.Option(help="Peak 10-second TPS burst for <50 GSU micro-burst window audit"),
    ] = None,
    input_tokens: Annotated[
        int, typer.Option(help="Input tokens per concurrency wave / request")
    ] = 20_000,
    output_tokens: Annotated[
        int, typer.Option(help="Output completion tokens per request")
    ] = 1_000,
    thoughts_tokens: Annotated[
        int, typer.Option(help="Reasoning / thought tokens per request")
    ] = 500,
    cached_tokens: Annotated[
        int, typer.Option(help="Cached prefix tokens per request")
    ] = 16_000,
    shared_prefix_ratio: Annotated[
        float, typer.Option(help="Fraction of input prompt that is shared static prefix (0-1)")
    ] = 0.80,
    baseline_cache_hit_rate: Annotated[
        float, typer.Option(help="Sticky-routed prefix cache hit rate (0-1)")
    ] = 0.80,
    endpoints: Annotated[
        int, typer.Option(help="Number of round-robin load-balanced endpoints/replicas")
    ] = 8,
    min_replicas: Annotated[
        int, typer.Option(help="Self-hosted min_replica_count")
    ] = 2,
    max_replicas: Annotated[
        int, typer.Option(help="Self-hosted max_replica_count")
    ] = 8,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run Newsvendor hybrid commitment sizing, micro-burst/KV audit, and prefix-cache dilution."""
    if vllm_file is not None:
        snap = load_inference_server_file(vllm_file, default_model=model)
        series = snap.tpm_series
    elif trace_file is not None:
        raw = json.loads(trace_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            typer.echo("Error: --trace-file must contain a JSON array of numbers", err=True)
            raise typer.Exit(code=1)
        series = [float(x) for x in raw]
    else:
        series = list(DEFAULT_SYNTHETIC_TPM_SERIES)

    if mode == CapacityMode.self_hosted:
        prof = _or_exit(lambda: resolve_self_hosted_profile(model))
        audit = evaluate_self_hosted_endpoint(
            tpm_series=series,
            profile=prof,
            min_replica_count=min_replicas,
            max_replica_count=max_replicas,
            total_input_tokens=input_tokens,
            shared_prefix_ratio=shared_prefix_ratio,
            baseline_cache_hit_rate=baseline_cache_hit_rate,
        )
        if output_format == OutputFormat.json:
            typer.echo(audit.model_dump_json(indent=2))
            return
        typer.echo(
            f"=== Self-Hosted GPU Endpoint Audit ({audit.profile_id} @ {audit.machine_type}) ==="
        )
        typer.echo(
            f"Upfront GPU Quota Lock: {audit.locked_regional_accelerators}x "
            f"{audit.accelerator_type} "
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
            f"(Effective Hit={pc.effective_cache_hit_rate * 100:.1f}%, "
            f"Savings={pc.gsu_savings_pct:.1f}%)"
        )
        return

    norm_model = normalize_model_id(model)
    cfg = load_gsu_config()
    tpm_per_gsu = _or_exit(lambda: require_capacity(norm_model, cfg))
    tps_per_gsu = tpm_per_gsu / 60.0

    req_weighted_burndown = compute_weighted_burndown_tokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thoughts_tokens=thoughts_tokens,
        cached_tokens=cached_tokens,
    )

    commitment = optimize_hybrid_commitment(
        tpm_series=series,
        model=norm_model,
        tpm_per_gsu=tpm_per_gsu,
        gsu_monthly_price_usd=gsu_price,
        paygo_per_1m_burndown_tokens_usd=paygo_per_1m,
        spillover_tier=spillover_tier,
        fsp_discount_pct=fsp_discount_pct,
    )

    prov_gsus = max(1, commitment.optimal_gsus)
    committed_tpm = prov_gsus * tpm_per_gsu
    default_60s = committed_tpm * 0.62
    sixty_s = sixty_sec_tpm if sixty_sec_tpm is not None else default_60s
    default_burst_tps = (sixty_s / 60.0) * 1.90
    peak_1s = peak_1s_tps if peak_1s_tps is not None else default_burst_tps

    micro_burst = evaluate_micro_burst_window(
        sixty_sec_tpm=sixty_s,
        peak_1s_tps=peak_1s,
        provisioned_gsus=prov_gsus,
        tps_per_gsu=tps_per_gsu,
        peak_10s_tps=peak_10s_tps,
    )

    prefix_cache = evaluate_prefix_cache_dilution(
        total_input_tokens=input_tokens,
        shared_prefix_ratio=shared_prefix_ratio,
        baseline_cache_hit_rate=baseline_cache_hit_rate,
        num_round_robin_endpoints=endpoints,
        tps_per_gsu=tps_per_gsu,
    )

    resp = OptimizeResponse(
        model=model,
        normalized_model=norm_model,
        weighted_burndown_tokens_per_request=round(req_weighted_burndown, 2),
        commitment=commitment,
        micro_burst=micro_burst,
        prefix_cache=prefix_cache,
    )

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
        return

    typer.echo(f"=== Newsvendor Hybrid Commitment ({norm_model}) ===")
    typer.echo(
        f"Optimal PT Base: {commitment.optimal_gsus} GSUs "
        f"(Quantile={commitment.critical_quantile:.2f}, "
        f"Break-Even Util={commitment.breakeven_utilization_pct:.1f}%)"
    )
    typer.echo(
        f"Monthly Cost:    ${commitment.total_hybrid_monthly_cost_usd:,.2f} "
        f"(PT=${commitment.pt_base_monthly_cost_usd:,.2f} + "
        f"Spillover=${commitment.spillover_monthly_cost_usd:,.2f})"
    )
    typer.echo(
        f"Savings:         ${commitment.savings_vs_paygo_usd:,.2f}/mo vs 100% PayGo "
        f"({commitment.savings_vs_paygo_pct:.1f}%), "
        f"${commitment.savings_vs_p99_pt_usd:,.2f}/mo vs P99 PT "
        f"({commitment.savings_vs_p99_pt_pct:.1f}%)"
    )
    typer.echo(
        f"Request Weight:  {resp.weighted_burndown_tokens_per_request:,.0f} "
        f"weighted burndown tokens "
        f"(in={input_tokens:,}, cached={cached_tokens:,}, "
        f"out={output_tokens:,}, thoughts={thoughts_tokens:,})"
    )
    typer.echo(
        f"Micro-Burst:     {micro_burst.refill_window_seconds}s window | "
        f"60s Util={micro_burst.apparent_60s_utilization_pct:.1f}% | "
        f"Peak Window Util={micro_burst.peak_window_utilization_pct:.1f}% | "
        f"Spillover={micro_burst.triggers_429_or_spillover} | "
        f"Illusion={micro_burst.illusion_detected}"
    )
    typer.echo(
        f"Prefix Cache:    Sticky={prefix_cache.sticky_gsus} GSUs vs "
        f"Round-Robin({prefix_cache.num_round_robin_endpoints})="
        f"{prefix_cache.round_robin_gsus} GSUs "
        f"(Effective Hit={prefix_cache.effective_cache_hit_rate * 100:.1f}%, "
        f"Wasted={prefix_cache.wasted_gsus} GSUs, Savings={prefix_cache.gsu_savings_pct:.1f}%)"
    )


@app.command()
def gateway(
    target: Annotated[
        GatewayTarget,
        typer.Option("--target", "-t", help="Gateway target: litellm, envoy, epp, or all"),
    ] = GatewayTarget.all,
    model: Annotated[str, typer.Option(help="Managed model ID")] = "gemini-3.8-flash",
    project_id: Annotated[str, typer.Option(help="GCP host project ID")] = "prod-genai-pt-host",
    location: Annotated[str, typer.Option(help="Vertex AI location")] = "global",
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = 20,
    rps: Annotated[int, typer.Option(help="Target gateway RPS")] = 650,
    sse_streams: Annotated[int, typer.Option(help="Concurrent SSE streams")] = 400,
    pods: Annotated[int, typer.Option(help="LiteLLM pod replicas")] = 12,
    workers: Annotated[int, typer.Option(help="Uvicorn workers per pod")] = 4,
    self_hosted_model: Annotated[
        str, typer.Option(help="Self-hosted model for GKE epp")
    ] = "llama-3.3-70b-fp8",
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Generate LiteLLM, Envoy AI Gateway (CEL), and GKE Inference Gateway (epp) configs."""
    bundle = _or_exit(
        lambda: generate_gateway_bundle(
            model=model,
            project_id=project_id,
            location=location,
            provisioned_gsus=gsus,
            target_rps=rps,
            concurrent_sse_streams=sse_streams,
            pods=pods,
            uvicorn_workers_per_pod=workers,
            self_hosted_model=self_hosted_model,
        )
    )
    if output_format == OutputFormat.json:
        typer.echo(bundle.model_dump_json(indent=2))
        return

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
    if target in (GatewayTarget.litellm, GatewayTarget.all):
        typer.echo("\n--- [1] LiteLLM Proxy (litellm_config.yaml) ---")
        typer.echo(bundle.litellm_config_yaml.rstrip())
        typer.echo(
            "\n--- [2] LiteLLM Weighted Burndown Callback (litellm_burndown_callback.py) ---"
        )
        typer.echo(bundle.litellm_burndown_callback_py.rstrip())
    if target in (GatewayTarget.envoy, GatewayTarget.all):
        typer.echo("\n--- [3] Envoy AI Gateway CEL (AIGatewayRoute + BackendTrafficPolicy) ---")
        typer.echo(bundle.envoy_ai_gateway_yaml.rstrip())
    if target in (GatewayTarget.epp, GatewayTarget.all):
        typer.echo("\n--- [4] GKE Inference Gateway (InferencePool + EndpointPickerConfig) ---")
        typer.echo(bundle.gke_epp_inference_pool_yaml.rstrip())


@app.command()
def schedule(
    model: Annotated[str, typer.Option(help="Model ID")] = "gemini-3.8-flash",
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = 20,
    evergreen_tokens: Annotated[
        float, typer.Option(help="Evergreen water-fill token backlog")
    ] = 250_000_000.0,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run the 3-Tier Residual Capacity Scheduler (Revenue > Hard-Deadline EDF > Evergreen)."""
    norm_model = normalize_model_id(model)
    cfg = load_gsu_config()
    tpm_per_gsu = _or_exit(lambda: require_capacity(norm_model, cfg))

    report = schedule_residual_capacity(
        provisioned_gsus=gsus,
        tpm_per_gsu=tpm_per_gsu,
        deadline_jobs=[
            DeadlineJobSpec(
                job_id="premarket-portfolio-briefings",
                value_usd=18_000.0,
                burndown_tokens=240_000_000.0,
                deadline_hour=6,
            ),
            DeadlineJobSpec(
                job_id="regulatory-risk-digest",
                value_usd=12_000.0,
                burndown_tokens=180_000_000.0,
                deadline_hour=5,
            ),
            DeadlineJobSpec(
                job_id="low-density-archive-sweep",
                value_usd=900.0,
                burndown_tokens=320_000_000.0,
                deadline_hour=6,
            ),
        ],
        evergreen_backfill_tokens=evergreen_tokens,
    )
    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    typer.echo(
        f"=== 3-Tier Residual Capacity Schedule ({norm_model}, {report.provisioned_gsus} GSUs) ==="
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


@app.command()
def chargeback(
    model: Annotated[
        str, typer.Option(help="Model or self-hosted profile ID")
    ] = "gemini-3.8-flash",
    mode: Annotated[
        CapacityMode,
        typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'"),
    ] = CapacityMode.gsu,
    gsus: Annotated[
        int, typer.Option(help="Provisioned GSUs (or reserved GPU replicas in self-hosted mode)")
    ] = 10,
    gsu_price: Annotated[
        float, typer.Option(help="Monthly price per 1 GSU (or 1 GPU replica) in USD")
    ] = 2_000.0,
    waterfill_tpm: Annotated[
        float,
        typer.Option(
            help="Off-peak asynchronous batch water-filling demand in burndown TPM"
        ),
    ] = 300_000.0,
    tenant_file: Annotated[
        Path | None,
        typer.Option(
            "--tenant-file",
            help="Optional JSON file mapping tenant name -> list of hourly burndown TPM floats",
        ),
    ] = None,
    vllm_file: Annotated[
        Path | None,
        typer.Option(
            "--vllm-file",
            help="Optional Phase 2 vLLM/SGLang JSONL log file with tenant token usage",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Compute FinOps FOCUS 1.2 Two-Pool Chargeback (Active Consumed vs Unallocated Idle)."""
    if mode == CapacityMode.self_hosted:
        prof = _or_exit(lambda: resolve_self_hosted_profile(model))
        tpm_per_unit = prof.tpm_per_replica
        unit_price = prof.reserved_replica_monthly_usd if gsu_price == 2_000.0 else gsu_price
        unit_name = "GPU Replicas"
    else:
        norm_model = normalize_model_id(model)
        cfg = load_gsu_config()
        tpm_per_unit = _or_exit(lambda: require_capacity(norm_model, cfg))
        unit_price = gsu_price
        unit_name = "GSUs"

    if vllm_file is not None:
        snap = load_inference_server_file(vllm_file, default_model=model)
        tenants: dict[str, list[float] | float] = dict(snap.hourly_consumed_tpm_by_tenant)
    elif tenant_file is not None:
        raw = json.loads(tenant_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            typer.echo("Error: --tenant-file must contain a JSON object", err=True)
            raise typer.Exit(code=1)
        tenants = {
            str(k): [float(x) for x in v] if isinstance(v, list) else float(v)
            for k, v in raw.items()
        }
    else:
        tenants = dict(DEFAULT_SYNTHETIC_TENANT_TPM)

    report = compute_focus_chargeback(
        hourly_consumed_tpm_by_tenant=tenants,
        provisioned_gsus=gsus,
        tpm_per_gsu=tpm_per_unit,
        gsu_monthly_price_usd=unit_price,
        async_batch_waterfill_tpm=waterfill_tpm,
        tenant_raw_token_weights=DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    )

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    typer.echo(
        f"=== FOCUS 1.2 Two-Pool Chargeback "
        f"({report.provisioned_gsus} {unit_name} @ ${unit_price:,.0f}/mo) ==="
    )
    typer.echo(
        f"Total Monthly Commitment:      ${report.total_commitment_cost_usd:,.2f}"
    )
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
    typer.echo(
        f"Net Unallocated Idle Pool:     ${report.net_unallocated_idle_cost_usd:,.2f}"
    )
    typer.echo("")
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
    fmt = "  ".join(
        f"{{:<{w}}}" if i == 0 else f"{{:>{w}}}" for i, w in enumerate(widths)
    )
    typer.echo(fmt.format(*headers))
    typer.echo("  ".join("—" * w for w in widths))
    for r in rows:
        typer.echo(fmt.format(*r))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind host")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option(help="Bind port")] = 8080,
) -> None:
    """Start the FastAPI web server and interactive GM3 Light capacity calculator."""
    import uvicorn

    uvicorn.run("capacity.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
