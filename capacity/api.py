"""FastAPI REST service & interactive GM3 Light Web Calculator for `capacity`."""

from __future__ import annotations

import math
import os
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import Field

from capacity.contract.common import (
    GCP_LOCATION_PATTERN,
    GCP_PROJECT_ID_PATTERN,
    MODEL_ID_PATTERN,
    PACKAGE_VERSION,
    UnknownModelError,
)
from capacity.contract.requests import (
    BatchScavengerRequest,
    ChargebackRequest,
    DisaggregatedPoolRequest,
    FinOpsKpiRequest,
    FolderPoolingRequest,
    GatewayConfigRequest,
    ModelTierPrefixCacheRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    VllmParseRequest,
)
from capacity.contract.results import (
    BatchScavengerAudit,
    DisaggregatedPoolSizing,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    GatewayConfigBundle,
    ModelTierPrefixCacheAudit,
    OptimizeResponse,
    RecommendationResponse,
    ResidualScheduleReport,
    SelfHostedEndpointAudit,
    VllmTelemetrySnapshot,
)
from capacity.contract.scenario import ShowcaseReport, ShowcaseScenario
from capacity.engine.burst import evaluate_model_tier_prefix_cache
from capacity.engine.chargeback import compute_focus_chargeback
from capacity.engine.kpis import compute_finops_kpis
from capacity.engine.pooling import evaluate_folder_pooling
from capacity.engine.scheduler import (
    evaluate_batch_scavenger_economics,
    schedule_residual_capacity,
)
from capacity.engine.self_hosted import (
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)
from capacity.engine.sizing import evaluate_gsu_optimization
from capacity.engine.stats import DEFAULT_DAYS, DEFAULT_PERCENTILES
from capacity.gateway.bundle import generate_gateway_bundle
from capacity.ingest.request_logs import parse_inference_server_telemetry
from capacity.showcase.evaluate import evaluate_showcase_scenario, load_showcase_scenario
from capacity.showcase.renderer import render_showcase_portal_html
from capacity.telemetry.local import LocalTraceAdapter
from capacity.telemetry.port import TelemetryPort, recommend_capacity

app = FastAPI(
    title="Capacity - AI Capacity, Multi-Gateway & FOCUS 1.2 FinOps Showcase",
    description=(
        "Empirical active-hours sizing, sub-second micro-burst & model-tier prefix-cache physics, "
        "LiteLLM / Envoy AI Gateway / K8s epp config generation, Newsvendor hybrid commitment "
        "optimizer, 3-Tier Residual Capacity Scheduler, and FOCUS 1.2 Two-Pool Chargeback."
    ),
    version=PACKAGE_VERSION,
)


def _sanitize_nonfinite(node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, float) and not math.isfinite(node):
        return repr(node)
    if isinstance(node, list):
        return [_sanitize_nonfinite(item) for item in node]
    if isinstance(node, dict):
        return {k: _sanitize_nonfinite(v) for k, v in node.items()}
    return node


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_nonfinite(jsonable_encoder(exc.errors()))},
    )


@app.exception_handler(UnknownModelError)
async def unknown_model_handler(_request: Request, exc: UnknownModelError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# The sizing handlers are plain `def` on purpose: FastAPI runs those in its thread pool, so a
# slow request cannot stall the event loop that answers `/healthz` and every other caller.


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    scenario_path = os.environ.get("CAPACITY_SCENARIO_FILE")
    scenario = load_showcase_scenario(scenario_path) if scenario_path else None
    report = evaluate_showcase_scenario(scenario)
    return HTMLResponse(content=render_showcase_portal_html(report, static_export=False))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/scenario/demo", response_model=ShowcaseReport)
def get_demo_scenario_report() -> ShowcaseReport:
    return evaluate_showcase_scenario(None)


@app.post("/api/v1/scenario/evaluate", response_model=ShowcaseReport)
def post_evaluate_scenario(scenario: ShowcaseScenario) -> ShowcaseReport:
    return evaluate_showcase_scenario(scenario)


@app.get("/api/v1/recommendations", response_model=RecommendationResponse)
async def get_recommendations(
    project: Annotated[
        str, Query(description="GCP project ID", pattern=GCP_PROJECT_ID_PATTERN)
    ] = "demo-project",
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_DAYS,
    percentile: Annotated[list[Annotated[int, Field(ge=0, le=100)]] | None, Query()] = None,
    model: Annotated[list[Annotated[str, Field(pattern=MODEL_ID_PATTERN)]] | None, Query()] = None,
    region: Annotated[
        list[Annotated[str, Field(pattern=GCP_LOCATION_PATTERN)]] | None, Query()
    ] = None,
    active_only: Annotated[bool, Query()] = False,
    by_region: Annotated[bool, Query()] = False,
    mode: Annotated[Literal["gsu", "self-hosted"], Query()] = "gsu",
    demo: Annotated[bool, Query()] = False,
    forecast_hours: Annotated[int, Query(ge=0, le=168)] = 24,
) -> RecommendationResponse:
    source: TelemetryPort
    if demo:
        source = LocalTraceAdapter()
    else:
        from capacity.telemetry.cloud_monitoring import CloudMonitoringAdapter

        source = CloudMonitoringAdapter()
    try:
        return await recommend_capacity(
            source,
            project=project,
            days=days,
            percentiles=sorted(percentile) if percentile else list(DEFAULT_PERCENTILES),
            models=model or None,
            regions=region or None,
            active_only=active_only,
            by_region=by_region,
            forecast_steps=forecast_hours,
            mode=mode,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
def post_optimize(req: OptimizeRequest) -> OptimizeResponse:
    return evaluate_gsu_optimization(req)


@app.post("/api/v1/prefix-cache/model-tier", response_model=ModelTierPrefixCacheAudit)
def post_model_tier_prefix_cache(req: ModelTierPrefixCacheRequest) -> ModelTierPrefixCacheAudit:
    return evaluate_model_tier_prefix_cache(req)


@app.post("/api/v1/self-hosted/audit", response_model=SelfHostedEndpointAudit)
def post_self_hosted_audit(req: SelfHostedAuditRequest) -> SelfHostedEndpointAudit:
    return evaluate_self_hosted_endpoint(req)


@app.post("/api/v1/self-hosted/disaggregated", response_model=DisaggregatedPoolSizing)
def post_disaggregated_sizing(req: DisaggregatedPoolRequest) -> DisaggregatedPoolSizing:
    return size_disaggregated_prefill_decode(req)


@app.post("/api/v1/vllm/parse", response_model=VllmTelemetrySnapshot)
def post_vllm_parse(req: VllmParseRequest) -> VllmTelemetrySnapshot:
    try:
        return parse_inference_server_telemetry(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unreadable telemetry: {exc}") from exc


@app.post("/api/v1/gateway/config", response_model=GatewayConfigBundle)
def post_gateway_config(req: GatewayConfigRequest) -> GatewayConfigBundle:
    return generate_gateway_bundle(req)


@app.post("/api/v1/chargeback", response_model=FocusChargebackReport)
def post_chargeback(req: ChargebackRequest) -> FocusChargebackReport:
    return compute_focus_chargeback(req)


@app.post("/api/v1/finops/scavenger", response_model=BatchScavengerAudit)
def post_batch_scavenger(req: BatchScavengerRequest) -> BatchScavengerAudit:
    return evaluate_batch_scavenger_economics(req)


@app.post("/api/v1/finops/schedule", response_model=ResidualScheduleReport)
def post_residual_schedule(req: ResidualScheduleRequest) -> ResidualScheduleReport:
    return schedule_residual_capacity(req)


@app.post("/api/v1/finops/folder-pooling", response_model=FolderPoolingReport)
def post_folder_pooling(req: FolderPoolingRequest) -> FolderPoolingReport:
    return evaluate_folder_pooling(req)


@app.post("/api/v1/finops/kpis", response_model=FinOpsKpiScorecard)
def post_finops_kpis(req: FinOpsKpiRequest) -> FinOpsKpiScorecard:
    return compute_finops_kpis(req)
