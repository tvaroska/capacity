"""FastAPI REST service & interactive GM3 Light Web Portal for `capacity`."""

from __future__ import annotations

from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import (
    _find_capacity,
    build_response,
    load_gsu_config,
    normalize_model_id,
)
from capacity.core import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    GCPQueryError,
    build_demo_throughput,
    fetch_throughput,
)
from capacity.finops import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    compute_focus_chargeback,
    optimize_hybrid_commitment,
)
from capacity.models import (
    ChargebackRequest,
    FocusChargebackReport,
    OptimizeRequest,
    OptimizeResponse,
    RecommendationResponse,
)
from capacity.self_hosted import (
    SelfHostedEndpointAudit,
    VllmTelemetrySnapshot,
    evaluate_self_hosted_endpoint,
    parse_inference_server_telemetry,
    resolve_self_hosted_profile,
)

load_dotenv()

app = FastAPI(
    title="Capacity - AI Capacity & FinOps Engine",
    description=(
        "Empirical active-hours sizing, sub-second micro-burst & KV-cache physics, "
        "Newsvendor hybrid commitment optimizer, and FOCUS 1.2 Two-Pool Chargeback "
        "across Managed Gemini Platform GSUs and Self-Hosted GPU/TPU Replicas."
    ),
    version="0.2.0",
)


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


_FAVICON_URI = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%231a73e8'/%3E"
    "%3Cpath d='M16 44V28h8v16zm12 0V18h8v26zm12 0V32h8v12z' fill='%23ffffff'/%3E"
    "%3Ccircle cx='44' cy='20' r='6' fill='%23fbbc04'/%3E%3C/svg%3E"
)

_PORTAL_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Capacity - AI Capacity & FOCUS 1.2 FinOps Portal</title>
  <link rel="icon" type="image/svg+xml" href="{_FAVICON_URI}" />
  <style>
    :root {{
      --bg: #f8f9fa;
      --surface: #ffffff;
      --border: #dadce0;
      --text: #202124;
      --muted: #5f6368;
      --primary: #1a73e8;
      --success: #137333;
      --warn: #b06000;
    }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .container {{
      max-width: 1120px;
      margin: 24px auto;
      padding: 0 20px;
      display: grid;
      gap: 20px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
    }}
    h1 {{ font-size: 18px; margin: 0; color: var(--primary); }}
    h2 {{ font-size: 15px; margin: 0 0 12px 0; }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }}
    .metric {{ font-size: 26px; font-weight: 700; color: var(--primary); margin-top: 4px; }}
    .sub {{ font-size: 12px; color: var(--muted); }}
    code {{
      background: #e8f0fe;
      color: #174ea6;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Capacity - Managed GSU & Self-Hosted GPU FinOps Engine (v0.2.0)</h1>
    <span class="sub">Interactive REST Portal · <a href="/docs">/docs (OpenAPI)</a></span>
  </header>
  <div class="container">
    <div class="card">
      <h2>4-Stage Capacity & FOCUS 1.2 FinOps Pipeline</h2>
      <div class="grid-3">
        <div>
          <div class="sub">1. Empirical Sizing + Holt Forecast</div>
          <div class="metric">Active-Hours P50-P99</div>
          <p class="sub">
            Prevents 8x5 diurnal zero-padding collapse across Managed GSUs
            and Vertex Self-Hosted GPU replicas.
          </p>
        </div>
        <div>
          <div class="sub">2. Micro-Burst & 1/N KV Physics</div>
          <div class="metric">1s / 10s Token Bucket</div>
          <p class="sub">
            Catches the &lt;70% 60s monitoring illusion, &gt;200K context cliffs,
            and 1/N round-robin prefix dilution.
          </p>
        </div>
        <div>
          <div class="sub">3. Newsvendor + FOCUS 1.2 Ledger</div>
          <div class="metric">Two-Pool Chargeback</div>
          <p class="sub">
            Separates Weighted Active Consumed Cost from Unallocated Idle Cost
            plus off-peak batch water-filling.
          </p>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>REST Endpoints</h2>
      <p class="sub">
        <code>GET /api/v1/recommendations?project=demo&amp;demo=true&amp;active_only=true</code> ·
        <code>POST /api/v1/optimize</code> ·
        <code>POST /api/v1/self-hosted/audit</code> ·
        <code>POST /api/v1/vllm/parse</code> ·
        <code>POST /api/v1/chargeback</code>
      </p>
    </div>
  </div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=_PORTAL_HTML)


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
    mode: Annotated[str, Query()] = "gsu",
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
    tpm_per_gsu = _find_capacity(norm_model, cfg) or 120_900
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


@app.post("/api/v1/vllm/parse", response_model=VllmTelemetrySnapshot)
async def post_vllm_parse(req: VllmParseRequest) -> VllmTelemetrySnapshot:
    return parse_inference_server_telemetry(
        req.raw_content, default_model=req.default_model
    )


@app.post("/api/v1/chargeback", response_model=FocusChargebackReport)
async def post_chargeback(req: ChargebackRequest) -> FocusChargebackReport:
    norm_model = normalize_model_id(req.model)
    cfg = load_gsu_config()
    tpm_per_gsu = _find_capacity(norm_model, cfg) or 120_900

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
