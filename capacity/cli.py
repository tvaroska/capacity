"""Typer CLI for `capacity`: sizing, micro-burst/KV physics, multi-gateway, and FOCUS 1.2."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from capacity.capacity import evaluate_gsu_optimization, recommend_capacity, require_capacity
from capacity.cli_formatters import (
    load_sparse_from_trace_file,
    parse_folder_map,
    print_chargeback_report,
    print_gsu_optimize_report,
    print_recommendations_table,
    print_self_hosted_audit,
)
from capacity.core import CloudMonitoringAdapter, GCPQueryError
from capacity.finops import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    compute_focus_chargeback,
    schedule_residual_capacity,
)
from capacity.forecasting import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    LocalTraceAdapter,
    TelemetryPort,
)
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import normalize_model_id
from capacity.models import (
    ChargebackRequest,
    GatewayConfigRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    fields_of,
)
from capacity.portal import build_showcase_summary_payload, render_showcase_portal_html
from capacity.self_hosted import (
    VllmTelemetrySnapshot,
    evaluate_self_hosted_endpoint,
    load_inference_server_file,
    resolve_self_hosted_profile,
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


# Option defaults are read from the request models the API validates with, so the two ports
# cannot drift apart. Every command then builds that request before it computes anything.
_OPTIMIZE = OptimizeRequest.model_fields
_SELF_HOSTED = SelfHostedAuditRequest.model_fields
_GATEWAY = GatewayConfigRequest.model_fields
_SCHEDULE = ResidualScheduleRequest.model_fields
_CHARGEBACK = ChargebackRequest.model_fields

DEFAULT_SYNTHETIC_TENANT_TPM: dict[str, list[float]] = {
    "claims-triage": [350_000.0] * 8 + [40_000.0] * 16,
    "underwriting-copilot": [250_000.0] * 8 + [30_000.0] * 16,
    "policy-audit": [150_000.0] * 8 + [15_000.0] * 16,
}


def _or_exit[T](fn: Callable[[], T]) -> T:
    """Turn bad values, bad files and unknown models into a one-line error, not a traceback."""
    try:
        return fn()
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'input'}: {err['msg']}"
            for err in exc.errors()
        )
        typer.echo(f"Error: {details}", err=True)
        raise typer.Exit(code=1) from exc
    except (ValueError, TypeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


def _write_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _load_tpm_list(path: Path) -> list[float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("--trace-file must contain a JSON array of numbers")
    return [float(x) for x in raw]


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
        _write_html(html_out, render_showcase_portal_html(static_export=True))

    payload = build_showcase_summary_payload()
    if output_format == OutputFormat.json:
        typer.echo(json.dumps(payload, indent=2))
        return

    kpis = payload["finops_kpis"]
    fpool = payload["folder_pooling"]
    scav = payload["scavenger"]
    disagg = payload["disaggregated_sizing"]
    audit = payload["gateway"]["litellm_audit"]

    typer.echo("=== Capacity v0.3.0 — End-to-End Executive Showcase Scorecard ===")
    typer.echo(
        f"1. FinOps for AI KPIs:    CPVO=${kpis['cpvo_usd']:.4f}/outcome | "
        f"CPWT_1M=${kpis['cpwt_1m_usd']:.4f} | TER={kpis['ter_pct']:.1f}% | "
        f"CSR={kpis['csr_pct']:.1f}% | ECUR_168h={kpis['ecur_168h_pct']:.1f}%"
    )
    typer.echo(
        f"2. Folder RHPT Pooling:   Siloed={fpool['siloed_total_gsus']} GSUs -> "
        f"Pooled={fpool['pooled_folder_gsus']} GSUs "
        f"(Saved={fpool['stranded_gsus_saved']} GSUs / ${fpool['monthly_savings_usd']:,.0f}/mo)"
    )
    typer.echo(
        f"3. Batch 0.5x Trap Saved: ${scav['double_payment_waste_avoided_usd']:,.2f}/mo "
        f"(Lifts PT utilization {scav['daytime_active_utilization_pct']:.0f}% -> "
        f"{scav['effective_utilization_after_scavenging_pct']:.1f}%)"
    )
    typer.echo(
        f"4. Disaggregated P/D:     {disagg['prefill_replicas']} Prefill + "
        f"{disagg['decode_replicas']} Decode Replicas "
        f"(vs {disagg['monolithic_replicas']} Monolithic, "
        f"saves {disagg['replica_delta']} replica[s])"
    )
    typer.echo(
        f"5. Multi-Gateway Audit:   LiteLLM ({audit['total_prisma_rust_engines']} "
        f"Prisma engines, cooldown={audit['anti_blackhole_cooldown_seconds']}s, "
        f"status={audit['scale_status']}) + Envoy CEL + GKE epp"
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
    percentile: Annotated[list[int] | None, typer.Option(help="Percentile (repeatable)")] = None,
    model: Annotated[list[str] | None, typer.Option(help="Filter by model (repeatable)")] = None,
    region: Annotated[list[str] | None, typer.Option(help="Filter by region (repeatable)")] = None,
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
        typer.Option(
            "--forecast-hours", min=0, max=168, help="Holt double-exponential smoothing horizon"
        ),
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
            raise _fail(f"percentile must be between 0 and 100, got {p}")
    if days < 1:
        raise _fail("days must be >= 1")

    env_proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
    proj_list = project or ([env_proj] if env_proj else [])
    default_trace_model = (
        model[0]
        if model
        else ("llama-3.3-70b-fp8" if mode == CapacityMode.self_hosted else "gemini-3.8-flash")
    )

    # With a trace or telemetry file, `--model` names the series; it is a filter otherwise.
    model_filter = model
    source: TelemetryPort
    if vllm_file is not None:
        snap = _or_exit(
            lambda: load_inference_server_file(vllm_file, default_model=default_trace_model)
        )
        base_dt = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
        minutes = {base_dt + timedelta(minutes=i): round(v) for i, v in enumerate(snap.tpm_series)}
        source = LocalTraceAdapter({snap.model: minutes}, max(days * 24 * 60, len(snap.tpm_series)))
        model_filter = None
    elif trace_file is not None:
        sparse, total_minutes = _or_exit(
            lambda: load_sparse_from_trace_file(
                trace_file, days, by_region, default_model=default_trace_model
            )
        )
        source = LocalTraceAdapter(sparse, total_minutes)
        model_filter = None
    elif demo:
        source = LocalTraceAdapter()
    elif proj_list:
        source = CloudMonitoringAdapter()
    else:
        raise _fail("provide `--project <id>` or pass `--demo` / `--trace-file` for offline mode.")

    folders = _or_exit(lambda: parse_folder_map(folder_map))
    try:
        resp = asyncio.run(
            recommend_capacity(
                source,
                project=proj_list or ["demo-project"],
                days=days,
                percentiles=pcts,
                models=model_filter,
                regions=region,
                active_only=active_only,
                by_region=by_region,
                folder_map=folders,
                forecast_steps=forecast_hours,
                mode=mode.value,
            )
        )
    except (GCPQueryError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    if html_out is not None:
        _write_html(
            html_out,
            render_showcase_portal_html(static_export=True, custom_recommendations=resp),
        )

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
    else:
        print_recommendations_table(resp, pcts)


@app.command()
def optimize(
    model: Annotated[str, typer.Option(help="Model or self-hosted profile ID")] = _OPTIMIZE[
        "model"
    ].default,
    mode: Annotated[
        CapacityMode, typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'")
    ] = CapacityMode.gsu,
    trace_file: Annotated[
        Path | None,
        typer.Option("--trace-file", help="Optional JSON file containing a list of minute TPMs"),
    ] = None,
    vllm_file: Annotated[
        Path | None,
        typer.Option("--vllm-file", help="Phase 2 vLLM/SGLang Prometheus or JSONL log file"),
    ] = None,
    gsu_price: Annotated[float, typer.Option(help="Monthly price per 1 GSU in USD")] = _OPTIMIZE[
        "gsu_monthly_price_usd"
    ].default,
    paygo_per_1m: Annotated[
        float, typer.Option(help="Base Standard PayGo price per 1M burndown tokens in USD")
    ] = _OPTIMIZE["paygo_per_1m_burndown_tokens_usd"].default,
    spillover_tier: Annotated[
        str, typer.Option(help="Spillover tier: priority (1.8x), standard (1.0x), flex (0.5x)")
    ] = _OPTIMIZE["spillover_tier"].default,
    fsp_discount_pct: Annotated[
        float, typer.Option(help="Flexible Savings Plan (FSP) discount percentage (0-50)")
    ] = _OPTIMIZE["fsp_discount_pct"].default,
    active_duty_pct: Annotated[
        float,
        typer.Option(
            "--active-duty-pct",
            help="Percentage of week represented by active-only trace (100 for 24x7 trace)",
        ),
    ] = _OPTIMIZE["active_duty_cycle_pct"].default,
    sixty_sec_tpm: Annotated[
        float | None,
        typer.Option(help="Measured 60-second TPM; the micro-burst audit needs it or a peak"),
    ] = None,
    peak_1s_tps: Annotated[
        float | None, typer.Option(help="Peak 1-second TPS burst for micro-burst window audit")
    ] = None,
    peak_10s_tps: Annotated[
        float | None, typer.Option(help="Peak 10-second TPS burst for <50 GSU window audit")
    ] = None,
    peak_30s_tps: Annotated[
        float | None, typer.Option(help="Peak 30-second TPS burst for 3P Claude dual-bucket audit")
    ] = None,
    input_tokens: Annotated[
        int, typer.Option(help="Input tokens per concurrency wave / request")
    ] = _OPTIMIZE["total_input_tokens"].default,
    output_tokens: Annotated[
        int, typer.Option(help="Output completion tokens per request")
    ] = _OPTIMIZE["output_tokens"].default,
    thoughts_tokens: Annotated[
        int, typer.Option(help="Reasoning / thought tokens per request")
    ] = _OPTIMIZE["thoughts_tokens"].default,
    cached_tokens: Annotated[
        int, typer.Option(help="Cached prefix tokens per request")
    ] = _OPTIMIZE["cached_tokens"].default,
    cache_write_5m_tokens: Annotated[
        int, typer.Option(help="3P Claude 5m ephemeral cache-write tokens per request (1.25x)")
    ] = _OPTIMIZE["cache_write_5m_tokens"].default,
    cache_write_1h_tokens: Annotated[
        int, typer.Option(help="3P Claude 1-hour ephemeral cache-write tokens per request (2.0x)")
    ] = _OPTIMIZE["cache_write_1h_tokens"].default,
    shared_prefix_ratio: Annotated[
        float, typer.Option(help="Fraction of input prompt that is shared static prefix (0-1)")
    ] = _OPTIMIZE["shared_prefix_ratio"].default,
    baseline_cache_hit_rate: Annotated[
        float, typer.Option(help="Sticky-routed prefix cache hit rate (0-1)")
    ] = _OPTIMIZE["baseline_cache_hit_rate"].default,
    endpoints: Annotated[
        int, typer.Option(help="Number of round-robin load-balanced endpoints/replicas")
    ] = _OPTIMIZE["num_round_robin_endpoints"].default,
    requests_per_second: Annotated[
        float | None,
        typer.Option(help="Request rate for prefix-cache GSU counts (default: from the trace)"),
    ] = None,
    min_replicas: Annotated[int, typer.Option(help="Self-hosted min_replica_count")] = _SELF_HOSTED[
        "min_replica_count"
    ].default,
    max_replicas: Annotated[int, typer.Option(help="Self-hosted max_replica_count")] = _SELF_HOSTED[
        "max_replica_count"
    ].default,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run Newsvendor hybrid commitment sizing, micro-burst/KV audit, and prefix-cache dilution."""
    snap: VllmTelemetrySnapshot | None = None
    series: list[float] | None = None
    if vllm_file is not None:
        snap = _or_exit(lambda: load_inference_server_file(vllm_file, default_model=model))
        series = snap.tpm_series
    elif trace_file is not None:
        series = _or_exit(lambda: _load_tpm_list(trace_file))

    # Without a file the request model's own sample series applies, as it does on the API.
    fields: dict[str, Any] = {} if series is None else {"tpm_series": series}

    if mode == CapacityMode.self_hosted:
        fields |= {
            "profile": snap.model if snap is not None else model,
            "min_replica_count": min_replicas,
            "max_replica_count": max_replicas,
            "total_input_tokens": input_tokens,
            "shared_prefix_ratio": shared_prefix_ratio,
            "baseline_cache_hit_rate": baseline_cache_hit_rate,
        }
        if snap is not None:
            fields["observed_vllm_kv_cache_usage_pct"] = snap.observed_kv_cache_usage_pct
            fields["observed_vllm_requests_waiting"] = snap.observed_requests_waiting
            if snap.observed_cache_hit_rate > 0.0:
                fields["baseline_cache_hit_rate"] = snap.observed_cache_hit_rate
        sh_req = _or_exit(lambda: SelfHostedAuditRequest(**fields))
        audit = _or_exit(lambda: evaluate_self_hosted_endpoint(**fields_of(sh_req)))
        if output_format == OutputFormat.json:
            typer.echo(audit.model_dump_json(indent=2))
        else:
            print_self_hosted_audit(audit)
        return

    fields |= {
        "model": model,
        "gsu_monthly_price_usd": gsu_price,
        "paygo_per_1m_burndown_tokens_usd": paygo_per_1m,
        "spillover_tier": spillover_tier,
        "fsp_discount_pct": fsp_discount_pct,
        "active_duty_cycle_pct": active_duty_pct,
        "sixty_sec_tpm": sixty_sec_tpm,
        "peak_1s_tps": peak_1s_tps,
        "peak_10s_tps": peak_10s_tps,
        "peak_30s_tps": peak_30s_tps,
        "total_input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thoughts_tokens": thoughts_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_5m_tokens": cache_write_5m_tokens,
        "cache_write_1h_tokens": cache_write_1h_tokens,
        "shared_prefix_ratio": shared_prefix_ratio,
        "baseline_cache_hit_rate": baseline_cache_hit_rate,
        "num_round_robin_endpoints": endpoints,
        "requests_per_second": requests_per_second,
    }
    req = _or_exit(lambda: OptimizeRequest(**fields))
    resp = _or_exit(lambda: evaluate_gsu_optimization(req))

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
        return

    print_gsu_optimize_report(resp, input_tokens, cached_tokens, output_tokens, thoughts_tokens)


@app.command()
def gateway(
    target: Annotated[
        GatewayTarget,
        typer.Option("--target", "-t", help="Gateway target: litellm, envoy, epp, or all"),
    ] = GatewayTarget.all,
    model: Annotated[str, typer.Option(help="Managed model ID")] = _GATEWAY["model"].default,
    project_id: Annotated[str, typer.Option(help="GCP host project ID")] = _GATEWAY[
        "project_id"
    ].default,
    location: Annotated[str, typer.Option(help="Vertex AI location")] = _GATEWAY[
        "location"
    ].default,
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = _GATEWAY[
        "provisioned_gsus"
    ].default,
    rps: Annotated[int, typer.Option(help="Target gateway RPS")] = _GATEWAY["target_rps"].default,
    sse_streams: Annotated[int, typer.Option(help="Concurrent SSE streams")] = _GATEWAY[
        "concurrent_sse_streams"
    ].default,
    pods: Annotated[int, typer.Option(help="LiteLLM pod replicas")] = _GATEWAY["pods"].default,
    workers: Annotated[int, typer.Option(help="Uvicorn workers per pod")] = _GATEWAY[
        "uvicorn_workers_per_pod"
    ].default,
    self_hosted_model: Annotated[
        str, typer.Option(help="Self-hosted model for GKE epp")
    ] = _GATEWAY["self_hosted_model"].default,
    bu_shares: Annotated[
        int, typer.Option(help="Business units sharing the committed pool equally")
    ] = _GATEWAY["business_unit_shares"].default,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Generate LiteLLM, Envoy AI Gateway (CEL), and GKE Inference Gateway (epp) configs."""
    req = _or_exit(
        lambda: GatewayConfigRequest(
            model=model,
            project_id=project_id,
            location=location,
            provisioned_gsus=gsus,
            target_rps=rps,
            concurrent_sse_streams=sse_streams,
            pods=pods,
            uvicorn_workers_per_pod=workers,
            self_hosted_model=self_hosted_model,
            business_unit_shares=bu_shares,
        )
    )
    bundle = _or_exit(lambda: generate_gateway_bundle(**fields_of(req)))
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
    model: Annotated[str, typer.Option(help="Model ID")] = _SCHEDULE["model"].default,
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = _SCHEDULE[
        "provisioned_gsus"
    ].default,
    evergreen_tokens: Annotated[
        float, typer.Option(help="Evergreen water-fill token backlog")
    ] = _SCHEDULE["evergreen_backfill_tokens"].default,
    jobs_file: Annotated[
        Path | None,
        typer.Option("--jobs-file", help="Optional JSON array of DeadlineJobSpec items"),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run the 3-Tier Residual Capacity Scheduler (Revenue > Hard-Deadline EDF > Evergreen)."""
    tpm_per_gsu = _or_exit(lambda: require_capacity(model))
    fields: dict[str, Any] = {
        "model": model,
        "provisioned_gsus": gsus,
        "evergreen_backfill_tokens": evergreen_tokens,
    }
    if jobs_file is not None:
        raw_jobs = _or_exit(lambda: json.loads(jobs_file.read_text(encoding="utf-8")))
        if not isinstance(raw_jobs, list):
            raise _fail("--jobs-file must contain a JSON array of job specs")
        fields["deadline_jobs"] = raw_jobs
    req = _or_exit(lambda: ResidualScheduleRequest(**fields))

    report = schedule_residual_capacity(
        tpm_per_gsu=tpm_per_gsu, **fields_of(req, without=("model",))
    )
    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    typer.echo(
        f"=== 3-Tier Residual Capacity Schedule "
        f"({normalize_model_id(model)}, {report.provisioned_gsus} GSUs) ==="
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
    model: Annotated[str, typer.Option(help="Model or self-hosted profile ID")] = _CHARGEBACK[
        "model"
    ].default,
    mode: Annotated[
        CapacityMode, typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'")
    ] = CapacityMode.gsu,
    gsus: Annotated[
        int, typer.Option(help="Provisioned GSUs (or reserved GPU replicas in self-hosted mode)")
    ] = _CHARGEBACK["provisioned_gsus"].default,
    gsu_price: Annotated[
        float | None,
        typer.Option(
            help=(
                "Monthly price per 1 GSU or GPU replica in USD "
                "(default: 2,000 per GSU, or the profile's reserved replica price)"
            )
        ),
    ] = None,
    waterfill_tpm: Annotated[
        float, typer.Option(help="Off-peak async batch water-filling demand in burndown TPM")
    ] = _CHARGEBACK["async_batch_waterfill_tpm"].default,
    tenant_file: Annotated[
        Path | None,
        typer.Option(
            "--tenant-file",
            help="Optional JSON mapping tenant -> hourly TPMs (or {tenants, raw_token_weights})",
        ),
    ] = None,
    vllm_file: Annotated[
        Path | None,
        typer.Option("--vllm-file", help="Optional Phase 2 vLLM/OTel JSONL log file"),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Compute FinOps FOCUS 1.2 Two-Pool Chargeback (Active Consumed vs Unallocated Idle)."""
    if mode == CapacityMode.self_hosted:
        prof = _or_exit(lambda: resolve_self_hosted_profile(model))
        tpm_per_unit = prof.tpm_per_replica
        default_price = prof.reserved_replica_monthly_usd
        unit_name = "GPU Replicas"
    else:
        tpm_per_unit = _or_exit(lambda: require_capacity(model))
        default_price = _CHARGEBACK["gsu_monthly_price_usd"].default
        unit_name = "GSUs"
    unit_price = gsu_price if gsu_price is not None else default_price

    tenants: Any = DEFAULT_SYNTHETIC_TENANT_TPM
    raw_weights: dict[str, Any] | None = None
    if vllm_file is not None:
        snap = _or_exit(lambda: load_inference_server_file(vllm_file, default_model=model))
        tenants = snap.hourly_consumed_tpm_by_tenant
    elif tenant_file is not None:
        raw = _or_exit(lambda: json.loads(tenant_file.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise _fail("--tenant-file must contain a JSON object")
        if isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
            if isinstance(raw.get("raw_token_weights"), dict):
                raw_weights = {**DEFAULT_TENANT_RAW_TOKEN_WEIGHTS, **raw["raw_token_weights"]}
        else:
            tenants = raw

    req = _or_exit(
        lambda: ChargebackRequest(
            model=model,
            provisioned_gsus=gsus,
            gsu_monthly_price_usd=unit_price,
            hourly_consumed_tpm_by_tenant=tenants,
            tenant_raw_token_weights=raw_weights,
            async_batch_waterfill_tpm=waterfill_tpm,
        )
    )
    report = compute_focus_chargeback(
        tpm_per_gsu=tpm_per_unit, **fields_of(req, without=("model",))
    )

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_chargeback_report(report, unit_name, unit_price)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Bind host (use 0.0.0.0 to listen on every interface)")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port")] = 8080,
) -> None:
    """Start the FastAPI web server and interactive GM3 Light capacity calculator."""
    import uvicorn

    uvicorn.run("capacity.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
