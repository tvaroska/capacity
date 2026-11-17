"""Typer CLI for `capacity`: sizing, micro-burst/KV physics, multi-gateway, and FOCUS 1.2."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from capacity.capacity import evaluate_gsu_optimization, recommend_capacity
from capacity.cli_formatters import (
    load_sparse_from_trace_file,
    parse_folder_map,
    print_chargeback_report,
    print_demo_report,
    print_gateway_report,
    print_gsu_optimize_report,
    print_recommendations_table,
    print_schedule_report,
    print_self_hosted_audit,
)
from capacity.finops import compute_focus_chargeback, schedule_residual_capacity
from capacity.forecasting import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    LocalTraceAdapter,
    TelemetryPort,
    minute_list_to_sparse,
)
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import normalize_model_id
from capacity.models import (
    ChargebackRequest,
    GatewayConfigRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    SeriesKey,
)
from capacity.portal import render_recommendations_html, render_showcase_portal_html
from capacity.scenario import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    DEMO_OPTIMIZE_SERIES,
    DEMO_SELF_HOSTED_SERIES,
    FinOpsPoolingMode,
    GatewayPresentationMode,
    PlatformBrand,
    evaluate_showcase_scenario,
    load_showcase_scenario,
    make_default_chargeback_tenants,
    make_default_deadline_jobs,
)
from capacity.self_hosted import evaluate_self_hosted_endpoint
from capacity.telemetry_parsers import VllmTelemetrySnapshot, load_inference_server_file

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


_OPTIMIZE = OptimizeRequest.model_fields
_SELF_HOSTED = SelfHostedAuditRequest.model_fields
_GATEWAY = GatewayConfigRequest.model_fields
_SCHEDULE = ResidualScheduleRequest.model_fields
_CHARGEBACK = ChargebackRequest.model_fields


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
    scenario_file: Annotated[
        Path | None,
        typer.Option(
            "--scenario-file",
            help="Optional JSON/YAML ShowcaseScenario contract for custom fleet/gateway scorecards",
        ),
    ] = None,
    strict_public_check: Annotated[
        bool,
        typer.Option(
            "--strict-public-check",
            help="Fail closed if --scenario-file contains unredacted cloud/order/ticket IDs",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run the end-to-end Showcase demo across all 4 planes (zero cloud credentials needed)."""
    scenario = (
        _or_exit(
            lambda: load_showcase_scenario(scenario_file, strict_public_check=strict_public_check)
        )
        if scenario_file is not None
        else None
    )
    report = _or_exit(lambda: evaluate_showcase_scenario(scenario))
    if html_out is not None:
        _write_html(html_out, render_showcase_portal_html(static_export=True, report=report))

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_demo_report(report, html_out)


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
            help="Optional path to write the self-contained GM3 Light sizing HTML scorecard",
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

    model_filter = model
    source: TelemetryPort
    if vllm_file is not None:
        snap = _or_exit(
            lambda: load_inference_server_file(vllm_file, default_model=default_trace_model)
        )
        minutes = _or_exit(lambda: minute_list_to_sparse(snap.tpm_series))
        source = LocalTraceAdapter(
            {SeriesKey(model=snap.model): minutes},
            max(days * 24 * 60, len(snap.tpm_series)),
        )
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
        from capacity.core import CloudMonitoringAdapter

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
    except (RuntimeError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    if html_out is not None:
        _write_html(html_out, render_recommendations_html(resp, static_export=True))

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
    else:
        print_recommendations_table(resp, pcts, forecast_hours=forecast_hours)


@app.command()
def optimize(
    model: Annotated[
        str | None,
        typer.Option(help="Model or self-hosted profile ID (defaults per --mode)"),
    ] = None,
    mode: Annotated[
        CapacityMode, typer.Option("--mode", help="Capacity mode: 'gsu' or 'self-hosted'")
    ] = CapacityMode.gsu,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Run offline with built-in sample throughput series"),
    ] = False,
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
    eff_model = (
        model
        if model is not None
        else (
            _SELF_HOSTED["profile"].default
            if mode == CapacityMode.self_hosted
            else _OPTIMIZE["model"].default
        )
    )
    snap: VllmTelemetrySnapshot | None = None
    series: list[float]
    if vllm_file is not None:
        snap = _or_exit(lambda: load_inference_server_file(vllm_file, default_model=eff_model))
        series = snap.tpm_series
    elif trace_file is not None:
        series = _or_exit(lambda: _load_tpm_list(trace_file))
    elif demo:
        series = (
            list(DEMO_SELF_HOSTED_SERIES)
            if mode == CapacityMode.self_hosted
            else list(DEMO_OPTIMIZE_SERIES)
        )
    else:
        raise _fail("provide `--trace-file <path>`, `--vllm-file <path>`, or pass `--demo`.")

    if mode == CapacityMode.self_hosted:
        sh_fields: dict[str, Any] = {
            "profile": snap.model if (snap is not None and model is None) else eff_model,
            "tpm_series": series,
            "min_replica_count": min_replicas,
            "max_replica_count": max_replicas,
            "total_input_tokens": input_tokens,
            "shared_prefix_ratio": shared_prefix_ratio,
            "baseline_cache_hit_rate": baseline_cache_hit_rate,
        }
        if snap is not None:
            sh_fields["observed_vllm_kv_cache_usage_pct"] = snap.observed_kv_cache_usage_pct
            sh_fields["observed_vllm_requests_waiting"] = snap.observed_requests_waiting
            if snap.observed_cache_hit_rate > 0.0:
                sh_fields["baseline_cache_hit_rate"] = snap.observed_cache_hit_rate
        sh_req = _or_exit(lambda: SelfHostedAuditRequest(**sh_fields))
        audit = _or_exit(lambda: evaluate_self_hosted_endpoint(sh_req))
        if output_format == OutputFormat.json:
            typer.echo(audit.model_dump_json(indent=2))
        else:
            print_self_hosted_audit(audit)
        return

    req = _or_exit(
        lambda: OptimizeRequest(
            model=eff_model,
            tpm_series=series,
            gsu_monthly_price_usd=gsu_price,
            paygo_per_1m_burndown_tokens_usd=paygo_per_1m,
            spillover_tier=spillover_tier,  # type: ignore[arg-type]
            fsp_discount_pct=fsp_discount_pct,
            active_duty_cycle_pct=active_duty_pct,
            sixty_sec_tpm=sixty_sec_tpm,
            peak_1s_tps=peak_1s_tps,
            peak_10s_tps=peak_10s_tps,
            peak_30s_tps=peak_30s_tps,
            total_input_tokens=input_tokens,
            output_tokens=output_tokens,
            thoughts_tokens=thoughts_tokens,
            cached_tokens=cached_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            shared_prefix_ratio=shared_prefix_ratio,
            baseline_cache_hit_rate=baseline_cache_hit_rate,
            num_round_robin_endpoints=endpoints,
            requests_per_second=requests_per_second,
        )
    )
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
    bundle = _or_exit(lambda: generate_gateway_bundle(req))
    if output_format == OutputFormat.json:
        typer.echo(bundle.model_dump_json(indent=2))
        return

    print_gateway_report(bundle, target.value)


@app.command()
def schedule(
    model: Annotated[str, typer.Option(help="Model ID")] = _SCHEDULE["model"].default,
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = _SCHEDULE[
        "provisioned_gsus"
    ].default,
    evergreen_tokens: Annotated[
        float, typer.Option(help="Evergreen water-fill token backlog")
    ] = _SCHEDULE["evergreen_backfill_tokens"].default,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Run offline with built-in sample overnight deadline jobs"),
    ] = False,
    jobs_file: Annotated[
        Path | None,
        typer.Option("--jobs-file", help="Optional JSON array of DeadlineJobSpec items"),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Run the 3-Tier Residual Capacity Scheduler (Revenue > Hard-Deadline EDF > Evergreen)."""
    raw_jobs: Any
    if jobs_file is not None:
        raw_jobs = _or_exit(lambda: json.loads(jobs_file.read_text(encoding="utf-8")))
        if not isinstance(raw_jobs, list):
            raise _fail("--jobs-file must contain a JSON array of job specs")
    elif demo:
        raw_jobs = make_default_deadline_jobs()
    else:
        raise _fail("provide `--jobs-file <path>` or pass `--demo`.")

    req = _or_exit(
        lambda: ResidualScheduleRequest(
            model=model,
            provisioned_gsus=gsus,
            evergreen_backfill_tokens=evergreen_tokens,
            deadline_jobs=raw_jobs,
        )
    )
    report = _or_exit(lambda: schedule_residual_capacity(req))
    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_schedule_report(report, normalize_model_id(model))


@app.command()
def chargeback(
    model: Annotated[
        str | None, typer.Option(help="Model or self-hosted profile ID (defaults per --mode)")
    ] = None,
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
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Run offline with built-in sample multi-tenant series"),
    ] = False,
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
    eff_model = (
        model
        if model is not None
        else (
            _SELF_HOSTED["profile"].default
            if mode == CapacityMode.self_hosted
            else _CHARGEBACK["model"].default
        )
    )
    tenants: Any
    raw_weights: dict[str, Any] | None = None
    if vllm_file is not None:
        snap = _or_exit(lambda: load_inference_server_file(vllm_file, default_model=eff_model))
        tenants = snap.hourly_consumed_tpm_by_tenant
    elif tenant_file is not None:
        raw = _or_exit(lambda: json.loads(tenant_file.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise _fail("--tenant-file must contain a JSON object")
        if isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
            if isinstance(raw.get("raw_token_weights"), dict):
                raw_weights = dict(raw["raw_token_weights"])
        else:
            tenants = raw
    elif demo:
        tenants = make_default_chargeback_tenants()
        raw_weights = dict(DEFAULT_TENANT_RAW_TOKEN_WEIGHTS)
    else:
        raise _fail("provide `--tenant-file <path>`, `--vllm-file <path>`, or pass `--demo`.")

    req = _or_exit(
        lambda: ChargebackRequest(
            model=eff_model,
            mode="self-hosted" if mode == CapacityMode.self_hosted else "gsu",
            provisioned_gsus=gsus,
            gsu_monthly_price_usd=gsu_price,
            hourly_consumed_tpm_by_tenant=tenants,
            tenant_raw_token_weights=raw_weights,
            async_batch_waterfill_tpm=waterfill_tpm,
        )
    )
    report = _or_exit(lambda: compute_focus_chargeback(req))

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_chargeback_report(report)


@app.command()
def collect(
    project: Annotated[
        list[str] | None,
        typer.Option(help="GCP project ID to collect Cloud Monitoring metrics from (repeatable)"),
    ] = None,
    days: Annotated[int, typer.Option(help="Complete UTC days of history", min=1, max=42)] = 7,
    by_region: Annotated[
        bool, typer.Option("--by-region/--no-by-region", help="Split series by GCP region")
    ] = True,
    logs_file: Annotated[
        Path | None,
        typer.Option(
            "--logs-file",
            help="Optional Observability 2.0 inference_request or OTel gen_ai.* JSONL log export",
        ),
    ] = None,
    orders_file: Annotated[
        Path | None,
        typer.Option("--orders-file", help="Optional YAML/JSON commercial PT order metadata"),
    ] = None,
    project_label: Annotated[
        str, typer.Option("--project-label", help="Display label for compiled scenario")
    ] = "enterprise-ai-hub",
    gateway_mode: Annotated[
        GatewayPresentationMode,
        typer.Option("--gateway-mode", help="Tab 3 mode: oss_stack or central_proxy"),
    ] = "oss_stack",
    pooling_mode: Annotated[
        FinOpsPoolingMode,
        typer.Option("--pooling-mode", help="Tab 4 mode: folder_rhpt or 1p_vs_3p_hub"),
    ] = "folder_rhpt",
    platform_brand: Annotated[
        PlatformBrand,
        typer.Option("--platform-brand", help="Heading brand: 'Gemini Platform' or 'Vertex AI'"),
    ] = "Gemini Platform",
    self_hosted: Annotated[
        bool, typer.Option("--self-hosted/--no-self-hosted", help="Include self-hosted GPU plane")
    ] = True,
    check_auth: Annotated[
        bool,
        typer.Option("--check-auth", help="Run read-only ADC + Monitoring API pre-flight check"),
    ] = False,
    strict_public_check: Annotated[
        bool,
        typer.Option("--strict-public-check", help="Fail closed on unredacted IDs before writing"),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output path for compiled ShowcaseScenario JSON"),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Collect customer Cloud Monitoring metrics and/or inference logs into a ShowcaseScenario."""
    from capacity.cli_collect import execute_collect_command

    _or_exit(
        lambda: execute_collect_command(
            proj_list=project or [],
            days=days,
            by_region=by_region,
            logs_file=logs_file,
            orders_file=orders_file,
            project_label=project_label,
            gateway_mode=gateway_mode,
            pooling_mode=pooling_mode,
            platform_brand=platform_brand,
            self_hosted=self_hosted,
            check_auth=check_auth,
            strict_public_check=strict_public_check,
            out=out,
            json_output=output_format == OutputFormat.json,
            or_exit=_or_exit,
            fail=_fail,
        )
    )


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Bind host (use 0.0.0.0 to listen on every interface)")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port")] = 8080,
    scenario_file: Annotated[
        Path | None,
        typer.Option("--scenario-file", help="Optional ShowcaseScenario JSON/YAML to serve at /"),
    ] = None,
    strict_public_check: Annotated[
        bool,
        typer.Option("--strict-public-check", help="Validate --scenario-file against ID leaks"),
    ] = False,
) -> None:
    """Start the FastAPI web server and interactive GM3 Light capacity calculator."""
    import uvicorn

    load_dotenv()
    if scenario_file is not None:
        _or_exit(
            lambda: load_showcase_scenario(scenario_file, strict_public_check=strict_public_check)
        )
        os.environ["CAPACITY_SCENARIO_FILE"] = str(scenario_file.resolve())

    uvicorn.run("capacity.api:app", host=host, port=port)


def main() -> None:
    load_dotenv()
    app()


if __name__ == "__main__":
    main()
