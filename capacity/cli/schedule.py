"""CLI `capacity schedule` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from capacity.cli.common import OutputFormat, app, fail, or_exit
from capacity.cli.formatters import print_schedule_report
from capacity.contract.requests import ResidualScheduleRequest
from capacity.engine.catalog import normalize_model_id
from capacity.engine.scheduler import schedule_residual_capacity
from capacity.showcase.sample_data import make_default_deadline_jobs

_SCHEDULE = ResidualScheduleRequest.model_fields


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
        raw_jobs = or_exit(lambda: json.loads(jobs_file.read_text(encoding="utf-8")))
        if not isinstance(raw_jobs, list):
            raise fail("--jobs-file must contain a JSON array of job specs")
    elif demo:
        raw_jobs = make_default_deadline_jobs()
    else:
        raise fail("provide `--jobs-file <path>` or pass `--demo`.")

    req = or_exit(
        lambda: ResidualScheduleRequest(
            model=model,
            provisioned_gsus=gsus,
            evergreen_backfill_tokens=evergreen_tokens,
            deadline_jobs=raw_jobs,
        )
    )
    report = or_exit(lambda: schedule_residual_capacity(req))
    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_schedule_report(report, normalize_model_id(model))
