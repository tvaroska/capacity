from __future__ import annotations

import asyncio
from enum import Enum
from typing import Annotated, Optional

import typer

from .capacity import build_response
from .core import DEFAULT_DAYS, DEFAULT_PERCENTILES, fetch_throughput

app = typer.Typer(help="Vertex AI Capacity Planner")


class OutputFormat(str, Enum):
    table = "table"
    json = "json"


@app.command()
def recommend(
    project: Annotated[str, typer.Option(help="GCP project ID")],
    days: Annotated[int, typer.Option(help="Days of history")] = DEFAULT_DAYS,
    percentile: Annotated[
        Optional[list[int]], typer.Option(help="Percentile (repeatable)")
    ] = None,
    model: Annotated[
        Optional[list[str]], typer.Option(help="Filter by model (repeatable)")
    ] = None,
    region: Annotated[
        Optional[list[str]], typer.Option(help="Filter by region (repeatable)")
    ] = None,
    format: Annotated[
        OutputFormat, typer.Option(help="Output format")
    ] = OutputFormat.table,
):
    """Recommend GSU allocation based on historical throughput."""
    pcts = percentile or list(DEFAULT_PERCENTILES)

    try:
        sparse, total_minutes = asyncio.run(
            fetch_throughput(project, days=days, models=model, regions=region)
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if not sparse:
        typer.echo("No data returned.", err=True)
        raise typer.Exit(1)

    response = build_response(project, days, sparse, total_minutes, pcts)

    if format == OutputFormat.json:
        typer.echo(response.model_dump_json(indent=2))
        return

    pct_labels = [f"P{p}" for p in pcts]
    header_parts = ["Model"]
    for label in pct_labels:
        header_parts.extend([f"{label} TPM", f"{label} GSUs"])

    rows: list[list[str]] = []
    for m in response.models:
        row: list[str] = [m.model]
        for label in pct_labels:
            detail = m.percentiles.get(label)
            if detail:
                gsus_str = str(detail.recommended_gsus) if detail.recommended_gsus is not None else "?"
                row.extend([str(detail.tpm), gsus_str])
            else:
                row.extend(["—", "—"])
        rows.append(row)

    col_widths = [len(h) for h in header_parts]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))

    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    typer.echo(fmt.format(*header_parts))
    typer.echo(fmt.format(*["—" * w for w in col_widths]))
    for row in rows:
        typer.echo(fmt.format(*row))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port")] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on changes")] = False,
):
    """Start the FastAPI server."""
    import uvicorn

    uvicorn.run("capacity.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
