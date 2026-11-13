"""FastAPI REST service & interactive GM3 Light Web Calculator for `capacity` (`v0.3.0`)."""

from __future__ import annotations

from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import Field

from capacity.burst import evaluate_model_tier_prefix_cache
from capacity.capacity import evaluate_gsu_optimization, recommend_capacity, require_capacity
from capacity.core import CloudMonitoringAdapter, GCPQueryError
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    schedule_residual_capacity,
)
from capacity.forecasting import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    LocalTraceAdapter,
    TelemetryPort,
    UnknownModelError,
)
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import normalize_model_id
from capacity.models import (
    GCP_LOCATION_PATTERN,
    GCP_PROJECT_ID_PATTERN,
    MODEL_ID_PATTERN,
    BatchScavengerAudit,
    BatchScavengerRequest,
    ChargebackRequest,
    DisaggregatedPoolRequest,
    DisaggregatedPoolSizing,
    FinOpsKpiRequest,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    FolderPoolingRequest,
    GatewayConfigBundle,
    GatewayConfigRequest,
    ModelTierPrefixCacheAudit,
    ModelTierPrefixCacheRequest,
    OptimizeRequest,
    OptimizeResponse,
    RecommendationResponse,
    ResidualScheduleReport,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    VllmParseRequest,
    fields_of,
)
from capacity.portal import render_showcase_portal_html
from capacity.self_hosted import (
    SelfHostedEndpointAudit,
    VllmTelemetrySnapshot,
    evaluate_self_hosted_endpoint,
    parse_inference_server_telemetry,
    size_disaggregated_prefill_decode,
)

load_dotenv()

app = FastAPI(
    title="Capacity - AI Capacity, Multi-Gateway & FOCUS 1.2 FinOps Showcase",
    description=(
        "Empirical active-hours sizing, sub-second micro-burst & model-tier prefix-cache physics, "
        "LiteLLM / Envoy AI Gateway / K8s epp config generation, Newsvendor hybrid commitment "
        "optimizer, 3-Tier Residual Capacity Scheduler, and FOCUS 1.2 Two-Pool Chargeback."
    ),
    version="0.3.0",
)


@app.exception_handler(UnknownModelError)
async def unknown_model_handler(_request: Request, exc: UnknownModelError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# The sizing handlers are plain `def` on purpose: FastAPI runs those in its thread pool, so a
# slow request cannot stall the event loop that answers `/healthz` and every other caller.


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=render_showcase_portal_html(static_export=False))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
    source: TelemetryPort = LocalTraceAdapter() if demo else CloudMonitoringAdapter()
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
    except GCPQueryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
def post_optimize(req: OptimizeRequest) -> OptimizeResponse:
    return evaluate_gsu_optimization(req)


@app.post("/api/v1/prefix-cache/model-tier", response_model=ModelTierPrefixCacheAudit)
def post_model_tier_prefix_cache(req: ModelTierPrefixCacheRequest) -> ModelTierPrefixCacheAudit:
    # An unknown model fails here as on every other route (ADR-001), not with Flash numbers.
    require_capacity(req.model)
    return evaluate_model_tier_prefix_cache(**fields_of(req))


@app.post("/api/v1/self-hosted/audit", response_model=SelfHostedEndpointAudit)
def post_self_hosted_audit(req: SelfHostedAuditRequest) -> SelfHostedEndpointAudit:
    return evaluate_self_hosted_endpoint(**fields_of(req))


@app.post("/api/v1/self-hosted/disaggregated", response_model=DisaggregatedPoolSizing)
def post_disaggregated_sizing(req: DisaggregatedPoolRequest) -> DisaggregatedPoolSizing:
    return size_disaggregated_prefill_decode(**fields_of(req))


@app.post("/api/v1/vllm/parse", response_model=VllmTelemetrySnapshot)
def post_vllm_parse(req: VllmParseRequest) -> VllmTelemetrySnapshot:
    try:
        return parse_inference_server_telemetry(**fields_of(req))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unreadable telemetry: {exc}") from exc


@app.post("/api/v1/gateway/config", response_model=GatewayConfigBundle)
def post_gateway_config(req: GatewayConfigRequest) -> GatewayConfigBundle:
    return generate_gateway_bundle(**fields_of(req))


@app.post("/api/v1/chargeback", response_model=FocusChargebackReport)
def post_chargeback(req: ChargebackRequest) -> FocusChargebackReport:
    return compute_focus_chargeback(
        tpm_per_gsu=require_capacity(req.model), **fields_of(req, without=("model",))
    )


@app.post("/api/v1/finops/scavenger", response_model=BatchScavengerAudit)
def post_batch_scavenger(req: BatchScavengerRequest) -> BatchScavengerAudit:
    return evaluate_batch_scavenger_economics(
        model=normalize_model_id(req.model),
        tpm_per_gsu=require_capacity(req.model),
        **fields_of(req, without=("model",)),
    )


@app.post("/api/v1/finops/schedule", response_model=ResidualScheduleReport)
def post_residual_schedule(req: ResidualScheduleRequest) -> ResidualScheduleReport:
    return schedule_residual_capacity(
        tpm_per_gsu=require_capacity(req.model), **fields_of(req, without=("model",))
    )


@app.post("/api/v1/finops/folder-pooling", response_model=FolderPoolingReport)
def post_folder_pooling(req: FolderPoolingRequest) -> FolderPoolingReport:
    return evaluate_folder_pooling(
        model=normalize_model_id(req.model),
        tpm_per_gsu=require_capacity(req.model),
        **fields_of(req, without=("model",)),
    )


@app.post("/api/v1/finops/kpis", response_model=FinOpsKpiScorecard)
def post_finops_kpis(req: FinOpsKpiRequest) -> FinOpsKpiScorecard:
    return compute_finops_kpis(**fields_of(req))
