"""CLI `capacity chargeback` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from capacity.cli.common import CapacityMode, OutputFormat, app, fail, or_exit
from capacity.cli.formatters import print_chargeback_report
from capacity.contract.requests import ChargebackRequest, SelfHostedAuditRequest
from capacity.engine.chargeback import compute_focus_chargeback
from capacity.ingest.request_logs import load_inference_server_file
from capacity.showcase.sample_data import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    make_default_chargeback_tenants,
)

_CHARGEBACK = ChargebackRequest.model_fields
_SELF_HOSTED = SelfHostedAuditRequest.model_fields


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
        snap = or_exit(
            lambda: load_inference_server_file(
                vllm_file,
                default_model=eff_model,
                model_filter=model,
                require_single_model=(model is None),
            )
        )
        tenants = snap.hourly_consumed_tpm_by_tenant
    elif tenant_file is not None:
        raw = or_exit(lambda: json.loads(tenant_file.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise fail("--tenant-file must contain a JSON object")
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
        raise fail("provide `--tenant-file <path>`, `--vllm-file <path>`, or pass `--demo`.")

    req = or_exit(
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
    report = or_exit(lambda: compute_focus_chargeback(req))

    if output_format == OutputFormat.json:
        typer.echo(report.model_dump_json(indent=2))
        return

    print_chargeback_report(report)
