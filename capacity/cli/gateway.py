"""CLI `capacity gateway` command."""

from __future__ import annotations

from typing import Annotated

import typer

from capacity.cli.common import GatewayTarget, OutputFormat, app, or_exit
from capacity.cli.formatters import print_gateway_report
from capacity.contract.requests import GatewayConfigRequest
from capacity.gateway.bundle import generate_gateway_bundle

_GATEWAY = GatewayConfigRequest.model_fields


@app.command()
def gateway(
    target: Annotated[
        GatewayTarget,
        typer.Option("--target", "-t", help="Gateway target: litellm, envoy, epp, or all"),
    ] = GatewayTarget.all,
    model: Annotated[str, typer.Option(help="Managed model ID")] = _GATEWAY["model"].default,
    project_id: Annotated[str, typer.Option(help="GCP host project ID")] = _GATEWAY[
        "project_id"
    ].default,
    location: Annotated[str, typer.Option(help="Vertex AI location")] = _GATEWAY[
        "location"
    ].default,
    gsus: Annotated[int, typer.Option(help="Provisioned GSUs")] = _GATEWAY[
        "provisioned_gsus"
    ].default,
    rps: Annotated[int, typer.Option(help="Target gateway RPS")] = _GATEWAY["target_rps"].default,
    sse_streams: Annotated[int, typer.Option(help="Concurrent SSE streams")] = _GATEWAY[
        "concurrent_sse_streams"
    ].default,
    pods: Annotated[int, typer.Option(help="LiteLLM pod replicas")] = _GATEWAY["pods"].default,
    workers: Annotated[int, typer.Option(help="Uvicorn workers per pod")] = _GATEWAY[
        "uvicorn_workers_per_pod"
    ].default,
    self_hosted_model: Annotated[
        str, typer.Option(help="Self-hosted model for GKE epp")
    ] = _GATEWAY["self_hosted_model"].default,
    bu_shares: Annotated[
        int, typer.Option(help="Business units sharing the committed pool equally")
    ] = _GATEWAY["business_unit_shares"].default,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Output format")
    ] = OutputFormat.table,
) -> None:
    """Generate LiteLLM, Envoy AI Gateway (CEL), and GKE Inference Gateway (epp) configs."""
    req = or_exit(
        lambda: GatewayConfigRequest(
            model=model,
            project_id=project_id,
            location=location,
            provisioned_gsus=gsus,
            target_rps=rps,
            concurrent_sse_streams=sse_streams,
            pods=pods,
            uvicorn_workers_per_pod=workers,
            self_hosted_model=self_hosted_model,
            business_unit_shares=bu_shares,
        )
    )
    bundle = or_exit(lambda: generate_gateway_bundle(req))
    if output_format == OutputFormat.json:
        typer.echo(bundle.model_dump_json(indent=2))
        return

    print_gateway_report(bundle, target.value)
