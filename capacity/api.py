"""FastAPI REST service & interactive GM3 Light Web Calculator for `capacity` (`v0.3.0`)."""

from __future__ import annotations

from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import (
    build_response,
    load_gsu_config,
    normalize_model_id,
    require_capacity,
)
from capacity.core import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    GCPQueryError,
    UnknownModelError,
    build_demo_throughput,
    fetch_throughput,
)
from capacity.finops import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.gateway import generate_gateway_bundle
from capacity.models import (
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
)
from capacity.portal import render_showcase_portal_html
from capacity.self_hosted import (
    SelfHostedEndpointAudit,
    VllmTelemetrySnapshot,
    evaluate_self_hosted_endpoint,
    parse_inference_server_telemetry,
    resolve_self_hosted_profile,
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


class SelfHostedAuditRequest(BaseModel):
    profile: str = "llama-3.3-70b-fp8"
    tpm_series: list[float] = Field(
        default_factory=lambda: [
            520_000.0,
            480_000.0,
            410_000.0,
            340_000.0,
            290_000.0,
            180_000.0,
        ]
    )
    min_replica_count: int = Field(default=2, ge=0)
    max_replica_count: int = Field(default=6, ge=1)
    observed_accelerator_duty_cycle_pct: float = Field(default=52.0, ge=0.0, le=100.0)
    observed_vllm_kv_cache_usage_pct: float = Field(default=91.5, ge=0.0, le=100.0)
    observed_vllm_requests_waiting: float = Field(default=14.0, ge=0.0)
    total_input_tokens: int = Field(default=20_000, ge=0)
    shared_prefix_ratio: float = Field(default=0.80, ge=0.0, le=1.0)
    baseline_cache_hit_rate: float = Field(default=0.80, ge=0.0, le=1.0)


class VllmParseRequest(BaseModel):
    raw_content: str
    default_model: str = "llama-3.3-70b-fp8"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=render_showcase_portal_html())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/recommendations", response_model=RecommendationResponse)
async def get_recommendations(
    project: Annotated[str, Query(description="GCP project ID")] = "demo-project",
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_DAYS,
    percentile: Annotated[
        list[Annotated[int, Field(ge=0, le=100)]] | None, Query()
    ] = None,
    model: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
    by_region: Annotated[bool, Query()] = False,
    mode: Annotated[Literal["gsu", "self-hosted"], Query()] = "gsu",
    demo: Annotated[bool, Query()] = False,
    forecast_hours: Annotated[int, Query(ge=0, le=168)] = 24,
) -> RecommendationResponse:
    pct_list = sorted(percentile) if percentile else list(DEFAULT_PERCENTILES)
    if demo:
        sparse, total_minutes = build_demo_throughput(
            days=days, by_region=by_region, mode=mode
        )
    else:
        try:
            sparse, total_minutes = await fetch_throughput(
                project,
                days,
                model or None,
                region or None,
                by_region=by_region,
                mode=mode,
            )
        except GCPQueryError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return build_response(
        project,
        days,
        sparse,
        total_minutes,
        pct_list,
        active_only=active_only,
        by_region=by_region,
        forecast_steps=forecast_hours,
        mode=mode,
    )


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
async def post_optimize(req: OptimizeRequest) -> OptimizeResponse:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = require_capacity(norm_model, cfg)
    tps_per_gsu = tpm_per_gsu / 60.0

    req_weighted_burndown = compute_weighted_burndown_tokens(
        input_tokens=req.total_input_tokens,
        output_tokens=req.output_tokens,
        thoughts_tokens=req.thoughts_tokens,
        cached_tokens=req.cached_tokens,
    )

    try:
        commitment = optimize_hybrid_commitment(
            tpm_series=req.tpm_series,
            model=norm_model,
            tpm_per_gsu=tpm_per_gsu,
            gsu_monthly_price_usd=req.gsu_monthly_price_usd,
            paygo_per_1m_burndown_tokens_usd=req.paygo_per_1m_burndown_tokens_usd,
            spillover_tier=req.spillover_tier,
            fsp_discount_pct=req.fsp_discount_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    prov_gsus = max(1, commitment.optimal_gsus)
    committed_tpm = prov_gsus * tpm_per_gsu
    default_60s = committed_tpm * 0.62
    sixty_s = req.sixty_sec_tpm if req.sixty_sec_tpm is not None else default_60s
    default_1s = (sixty_s / 60.0) * 1.90
    peak_1s = req.peak_1s_tps if req.peak_1s_tps is not None else default_1s

    micro_burst = evaluate_micro_burst_window(
        sixty_sec_tpm=sixty_s,
        peak_1s_tps=peak_1s,
        provisioned_gsus=prov_gsus,
        tps_per_gsu=tps_per_gsu,
        peak_10s_tps=req.peak_10s_tps,
    )

    prefix_cache = evaluate_prefix_cache_dilution(
        total_input_tokens=req.total_input_tokens,
        shared_prefix_ratio=req.shared_prefix_ratio,
        baseline_cache_hit_rate=req.baseline_cache_hit_rate,
        num_round_robin_endpoints=req.num_round_robin_endpoints,
        tps_per_gsu=tps_per_gsu,
    )

    return OptimizeResponse(
        model=req.model,
        normalized_model=norm_model,
        weighted_burndown_tokens_per_request=round(req_weighted_burndown, 2),
        commitment=commitment,
        micro_burst=micro_burst,
        prefix_cache=prefix_cache,
    )


@app.post("/api/v1/prefix-cache/model-tier", response_model=ModelTierPrefixCacheAudit)
async def post_model_tier_prefix_cache(
    req: ModelTierPrefixCacheRequest,
) -> ModelTierPrefixCacheAudit:
    return evaluate_model_tier_prefix_cache(
        model=req.model,
        total_input_tokens=req.total_input_tokens,
        static_prefix_tokens=req.static_prefix_tokens,
        output_tokens=req.output_tokens,
        thoughts_tokens=req.thoughts_tokens,
        dynamic_header_offset_tokens=req.dynamic_header_offset_tokens,
        single_turn_request=req.single_turn_request,
        dummy_turn_anchor_applied=req.dummy_turn_anchor_applied,
        strip_thought_signatures=req.strip_thought_signatures,
        num_replicas=req.num_replicas,
    )


@app.post("/api/v1/self-hosted/audit", response_model=SelfHostedEndpointAudit)
async def post_self_hosted_audit(
    req: SelfHostedAuditRequest,
) -> SelfHostedEndpointAudit:
    prof = resolve_self_hosted_profile(req.profile)
    return evaluate_self_hosted_endpoint(
        tpm_series=req.tpm_series,
        profile=prof,
        min_replica_count=req.min_replica_count,
        max_replica_count=req.max_replica_count,
        observed_accelerator_duty_cycle_pct=req.observed_accelerator_duty_cycle_pct,
        observed_vllm_kv_cache_usage_pct=req.observed_vllm_kv_cache_usage_pct,
        observed_vllm_requests_waiting=req.observed_vllm_requests_waiting,
        total_input_tokens=req.total_input_tokens,
        shared_prefix_ratio=req.shared_prefix_ratio,
        baseline_cache_hit_rate=req.baseline_cache_hit_rate,
    )


@app.post("/api/v1/self-hosted/disaggregated", response_model=DisaggregatedPoolSizing)
async def post_disaggregated_sizing(
    req: DisaggregatedPoolRequest,
) -> DisaggregatedPoolSizing:
    return size_disaggregated_prefill_decode(
        profile=req.profile,
        peak_qps=req.peak_qps,
        avg_input_tokens=req.avg_input_tokens,
        avg_output_tokens=req.avg_output_tokens,
        target_ttft_ms=req.target_ttft_ms,
        target_tpot_ms=req.target_tpot_ms,
    )


@app.post("/api/v1/vllm/parse", response_model=VllmTelemetrySnapshot)
async def post_vllm_parse(req: VllmParseRequest) -> VllmTelemetrySnapshot:
    return parse_inference_server_telemetry(
        req.raw_content, default_model=req.default_model
    )


@app.post("/api/v1/gateway/config", response_model=GatewayConfigBundle)
async def post_gateway_config(req: GatewayConfigRequest) -> GatewayConfigBundle:
    return generate_gateway_bundle(
        model=req.model,
        project_id=req.project_id,
        location=req.location,
        provisioned_gsus=req.provisioned_gsus,
        target_rps=req.target_rps,
        concurrent_sse_streams=req.concurrent_sse_streams,
        pods=req.pods,
        uvicorn_workers_per_pod=req.uvicorn_workers_per_pod,
        self_hosted_model=req.self_hosted_model,
    )


@app.post("/api/v1/chargeback", response_model=FocusChargebackReport)
async def post_chargeback(req: ChargebackRequest) -> FocusChargebackReport:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = require_capacity(norm_model, cfg)

    return compute_focus_chargeback(
        hourly_consumed_tpm_by_tenant=req.hourly_consumed_tpm_by_tenant,
        provisioned_gsus=req.provisioned_gsus,
        tpm_per_gsu=tpm_per_gsu,
        gsu_monthly_price_usd=req.gsu_monthly_price_usd,
        async_batch_waterfill_tpm=req.async_batch_waterfill_tpm,
        tenant_raw_token_weights=(
            req.tenant_raw_token_weights
            if req.tenant_raw_token_weights is not None
            else DEFAULT_TENANT_RAW_TOKEN_WEIGHTS
        ),
    )


@app.post("/api/v1/finops/scavenger", response_model=BatchScavengerAudit)
async def post_batch_scavenger(req: BatchScavengerRequest) -> BatchScavengerAudit:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = require_capacity(norm_model, cfg)
    return evaluate_batch_scavenger_economics(
        model=norm_model,
        provisioned_gsus=req.provisioned_gsus,
        tpm_per_gsu=tpm_per_gsu,
        gsu_monthly_price_usd=req.gsu_monthly_price_usd,
        daytime_active_utilization_pct=req.daytime_active_utilization_pct,
        batch_monthly_burndown_tokens=req.batch_monthly_burndown_tokens,
        paygo_per_1m_burndown_tokens_usd=req.paygo_per_1m_burndown_tokens_usd,
    )


@app.post("/api/v1/finops/schedule", response_model=ResidualScheduleReport)
async def post_residual_schedule(req: ResidualScheduleRequest) -> ResidualScheduleReport:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = require_capacity(norm_model, cfg)
    return schedule_residual_capacity(
        provisioned_gsus=req.provisioned_gsus,
        tpm_per_gsu=tpm_per_gsu,
        safety_headroom_ratio=req.safety_headroom_ratio,
        revenue_tpm_by_hour=req.revenue_tpm_by_hour,
        deadline_jobs=req.deadline_jobs,
        evergreen_backfill_tokens=req.evergreen_backfill_tokens,
        paygo_per_1m_burndown_tokens_usd=req.paygo_per_1m_burndown_tokens_usd,
    )


@app.post("/api/v1/finops/folder-pooling", response_model=FolderPoolingReport)
async def post_folder_pooling(req: FolderPoolingRequest) -> FolderPoolingReport:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = require_capacity(norm_model, cfg)
    return evaluate_folder_pooling(
        hourly_tpm_by_project=req.hourly_tpm_by_project,
        model=norm_model,
        tpm_per_gsu=tpm_per_gsu,
        percentile_target=req.percentile_target,
        gsu_monthly_price_usd=req.gsu_monthly_price_usd,
    )


@app.post("/api/v1/finops/kpis", response_model=FinOpsKpiScorecard)
async def post_finops_kpis(req: FinOpsKpiRequest) -> FinOpsKpiScorecard:
    return compute_finops_kpis(
        total_amortized_cost_usd=req.total_amortized_cost_usd,
        uncached_input_tokens=req.uncached_input_tokens,
        cached_input_tokens=req.cached_input_tokens,
        delivered_output_tokens=req.delivered_output_tokens,
        thinking_tokens=req.thinking_tokens,
        retry_and_judge_tokens=req.retry_and_judge_tokens,
        verified_outcomes=req.verified_outcomes,
        hourly_reserved_burndown_capacity=req.hourly_reserved_burndown_capacity,
        weekly_consumed_burndown_tokens=req.weekly_consumed_burndown_tokens,
    )
