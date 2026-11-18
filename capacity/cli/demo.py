"""CLI `capacity demo` command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from capacity.cli.common import OutputFormat, app, or_exit, write_html
from capacity.cli.formatters import print_demo_report
from capacity.showcase.evaluate import evaluate_showcase_scenario, load_showcase_scenario
from capacity.showcase.renderer import render_showcase_portal_html


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
        or_exit(
            lambda: load_showcase_scenario(scenario_file, strict_public_check=strict_public_check)
        )
        if scenario_file is not None
        else None
    )
    report = or_exit(lambda: evaluate_showcase_scenario(scenario))
    if html_out is not None:
        write_html(html_out, render_showcase_portal_html(report, static_export=True))

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_demo_report(report, html_out)
