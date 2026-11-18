"""CLI `capacity optimize` command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from capacity.cli.common import CapacityMode, OutputFormat, app, fail, or_exit
from capacity.cli.formatters import print_gsu_optimize_report, print_self_hosted_audit
from capacity.contract.requests import OptimizeRequest, SelfHostedAuditRequest
from capacity.contract.results import VllmTelemetrySnapshot
from capacity.engine.self_hosted import evaluate_self_hosted_endpoint
from capacity.engine.sizing import evaluate_gsu_optimization
from capacity.ingest.request_logs import load_inference_server_file
from capacity.ingest.trace_files import load_tpm_list
from capacity.showcase.sample_data import DEMO_OPTIMIZE_SERIES, DEMO_SELF_HOSTED_SERIES

_OPTIMIZE = OptimizeRequest.model_fields
_SELF_HOSTED = SelfHostedAuditRequest.model_fields


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
        snap = or_exit(lambda: load_inference_server_file(vllm_file, default_model=eff_model))
        series = snap.tpm_series
    elif trace_file is not None:
        series = or_exit(lambda: load_tpm_list(trace_file))
    elif demo:
        series = (
            list(DEMO_SELF_HOSTED_SERIES)
            if mode == CapacityMode.self_hosted
            else list(DEMO_OPTIMIZE_SERIES)
        )
    else:
        raise fail("provide `--trace-file <path>`, `--vllm-file <path>`, or pass `--demo`.")

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
        sh_req = or_exit(lambda: SelfHostedAuditRequest(**sh_fields))
        audit = or_exit(lambda: evaluate_self_hosted_endpoint(sh_req))
        if output_format == OutputFormat.json:
            typer.echo(audit.model_dump_json(indent=2))
        else:
            print_self_hosted_audit(audit)
        return

    req = or_exit(
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
    resp = or_exit(lambda: evaluate_gsu_optimization(req))

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
        return

    print_gsu_optimize_report(resp, input_tokens, cached_tokens, output_tokens, thoughts_tokens)
