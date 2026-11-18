"""CLI `capacity collect` command."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from capacity.cli.common import OutputFormat, app, fail, or_exit
from capacity.cli.formatters import print_auth_check_report, print_collect_summary
from capacity.contract.common import SeriesKey
from capacity.contract.scenario import (
    FinOpsPoolingMode,
    GatewayPresentationMode,
    PlatformBrand,
    ShowcaseScenario,
)
from capacity.ingest.request_logs import load_inference_server_file
from capacity.showcase.evaluate import (
    audit_scenario_public_safety,
    compile_scenario_from_telemetry,
    load_orders_overlay,
)


def _run_collect(
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
) -> None:
    if check_auth:
        from capacity.telemetry.cloud_monitoring import check_gcp_project_access

        report = or_exit(lambda: asyncio.run(check_gcp_project_access(proj_list)))
        if json_output:
            typer.echo(report.model_dump_json(indent=2))
        else:
            print_auth_check_report(report)
        if not report.ok:
            raise typer.Exit(code=1)
        if out is None and logs_file is None:
            return

    if not proj_list and logs_file is None:
        raise fail("provide `--project <id>` and/or `--logs-file <path>` (or `--check-auth`).")

    sparse: dict[SeriesKey, dict[datetime, int]] | None = None
    total_mins: int | None = None
    inventory: dict[SeriesKey, dict[str, int]] | None = None
    if proj_list:
        from capacity.telemetry.cloud_monitoring import (
            CloudMonitoringAdapter,
            GCPQueryError,
            fetch_inventory_telemetry,
        )

        adapter = CloudMonitoringAdapter()
        try:
            sparse, total_mins = asyncio.run(
                adapter.fetch_series(project=proj_list, days=days, by_region=by_region)
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
    or_exit(
        lambda: _run_collect(
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
        )
    )
