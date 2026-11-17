"""Cloud Monitoring & log telemetry scenario collection command (`capacity collect`)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import typer

from capacity.core import (
    CloudMonitoringAdapter,
    GCPQueryError,
    check_gcp_project_access,
    fetch_inventory_telemetry,
)
from capacity.models import GcpAccessCheckReport, SeriesKey
from capacity.scenario import (
    FinOpsPoolingMode,
    GatewayPresentationMode,
    PlatformBrand,
    ShowcaseScenario,
    audit_scenario_public_safety,
    compile_scenario_from_telemetry,
    load_orders_overlay,
)
from capacity.telemetry_parsers import load_inference_server_file


def print_auth_check_report(report: GcpAccessCheckReport) -> None:
    account = report.account or "unauthenticated"
    typer.echo(
        f"=== GCP Monitoring Pre-Flight Check "
        f"(authenticated={report.authenticated}, caller={account}) ==="
    )
    for proj, info in report.projects.items():
        status = "OK" if info.ok else f"BLOCKED ({info.error or 'unknown'})"
        typer.echo(f"  - {proj}: {status}")
    if report.instructions:
        typer.echo("Remediation:")
        for item in report.instructions:
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
    or_exit: Callable[[Callable[[], GcpAccessCheckReport]], GcpAccessCheckReport],
    fail: Callable[[str], typer.Exit],
) -> None:
    if check_auth:
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
