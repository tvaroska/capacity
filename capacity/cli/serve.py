"""CLI `capacity serve` command."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from capacity.cli.common import app, or_exit
from capacity.showcase.evaluate import load_showcase_scenario


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
        or_exit(
            lambda: load_showcase_scenario(scenario_file, strict_public_check=strict_public_check)
        )
        os.environ["CAPACITY_SCENARIO_FILE"] = str(scenario_file.resolve())

    uvicorn.run("capacity.api:app", host=host, port=port)
