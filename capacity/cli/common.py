"""Shared Typer application instance, CLI enums, and error-boundary helpers."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import typer
from pydantic import ValidationError

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


def or_exit[T](fn: Callable[[], T]) -> T:
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


def fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


def write_html(path: Path, html_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
