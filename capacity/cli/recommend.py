"""CLI `capacity recommend` command."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from capacity.cli.common import CapacityMode, OutputFormat, app, fail, or_exit, write_html
from capacity.cli.formatters import print_recommendations_table
from capacity.contract.common import SeriesKey
from capacity.engine.stats import DEFAULT_DAYS, DEFAULT_PERCENTILES, minute_list_to_sparse
from capacity.ingest.request_logs import load_inference_server_file
from capacity.ingest.trace_files import load_sparse_from_trace_file, parse_folder_map
from capacity.showcase.renderer import render_recommendations_html
from capacity.telemetry.local import LocalTraceAdapter
from capacity.telemetry.port import TelemetryPort, recommend_capacity


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
            raise fail(f"percentile must be between 0 and 100, got {p}")
    if days < 1:
        raise fail("days must be >= 1")

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
        snap = or_exit(
            lambda: load_inference_server_file(vllm_file, default_model=default_trace_model)
        )
        minutes = or_exit(lambda: minute_list_to_sparse(snap.tpm_series))
        source = LocalTraceAdapter(
            {SeriesKey(model=snap.model): minutes},
            max(days * 24 * 60, len(snap.tpm_series)),
        )
        model_filter = None
    elif trace_file is not None:
        sparse, total_minutes = or_exit(
            lambda: load_sparse_from_trace_file(
                trace_file, days, by_region, default_model=default_trace_model
            )
        )
        source = LocalTraceAdapter(sparse, total_minutes)
        model_filter = None
    elif demo:
        source = LocalTraceAdapter()
    elif proj_list:
        from capacity.telemetry.cloud_monitoring import CloudMonitoringAdapter

        source = CloudMonitoringAdapter()
    else:
        raise fail("provide `--project <id>` or pass `--demo` / `--trace-file` for offline mode.")

    folders = or_exit(lambda: parse_folder_map(folder_map))
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
        raise fail(str(exc)) from exc

    if html_out is not None:
        write_html(html_out, render_recommendations_html(resp, static_export=True))

    if output_format == OutputFormat.json:
        typer.echo(resp.model_dump_json(indent=2))
    else:
        print_recommendations_table(resp, pcts, forecast_hours=forecast_hours)
