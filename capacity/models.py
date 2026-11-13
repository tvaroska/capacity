"""Pydantic wire models for capacity recommendations, micro-burst, Newsvendor, and FOCUS 1.2."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Interpolated into generated YAML/Kubernetes manifests and Cloud Monitoring filters;
# reject quotes, newlines, spaces, or injection tokens at validation boundaries.
GCP_PROJECT_ID_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
GCP_LOCATION_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.@/_-]{0,127}$"
K8S_NAME_PATTERN = r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$"

BoundedTpmFloat = Annotated[float, Field(ge=0.0, le=1e11, allow_inf_nan=False)]
FiniteNonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FinitePosFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

# Size bounds: one request must not be able to make the service compute for seconds.
MAX_HOURLY_POINTS = 8_784  # hours in a leap year
MAX_SERIES_KEYS = 1_000  # tenants or projects in one request
MAX_SERIES_CELLS = 1_000_000  # keys times the longest series: the padded matrix
HourlyTpmSeries = Annotated[list[BoundedTpmFloat], Field(max_length=MAX_HOURLY_POINTS)]
SpilloverTier = Literal["priority", "standard", "flex"]

DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR: tuple[float, ...] = (
    600_000.0,
    400_000.0,
    300_000.0,
    250_000.0,
    200_000.0,
    350_000.0,
    900_000.0,
    1_800_000.0,
)


class StrictModel(BaseModel):
    """Base wire model: a field the schema does not define is an error, never ignored."""

    model_config = ConfigDict(extra="forbid")


def fields_of(req: BaseModel, *, without: tuple[str, ...] = ()) -> dict[str, Any]:
    """Request fields as keyword arguments for the domain function of the same operation.

    Request models name their fields after the parameters of the function they feed
    (`tests/test_request_contract.py` holds them to it), so a port hands a request on
    without restating each argument.
    """
    return {name: value for name, value in req if name not in without}


def _reject_oversized_matrix(series_by_key: dict[str, Any]) -> None:
    longest = max((len(v) for v in series_by_key.values() if isinstance(v, list)), default=1)
    cells = len(series_by_key) * max(1, longest)
    if cells > MAX_SERIES_CELLS:
        raise ValueError(
            f"{len(series_by_key)} series padded to {longest} points is {cells:,} cells; "
            f"the limit is {MAX_SERIES_CELLS:,}"
        )


class PercentileDetail(StrictModel):
    tpm: int = Field(description="Tokens per minute at this percentile")
    recommended_gsus: int = Field(
        description="Recommended GSUs (or GPU replicas in self-hosted mode)"
    )


class ModelRecommendation(StrictModel):
    model: str = Field(description="Model or self-hosted endpoint identifier")
    normalized_model: str = Field(
        default="",
        description="Normalized canonical model identifier",
    )
    region: str | None = Field(
        default=None,
        description="GCP region when --by-region / by_region=true is enabled",
    )
    folder: str | None = Field(
        default=None,
        description="Department or GCP folder when --folder-map is provided",
    )
    gsu_capacity_tpm: int | None = Field(
        description=(
            "Sustained tokens-per-minute capacity per 1 GSU (or 1 GPU replica), or None if unknown"
        )
    )
    active_minutes: int = Field(
        default=0,
        description="Minutes with non-zero token throughput inside the query window",
    )
    total_minutes: int = Field(
        default=0,
        description="Total minutes in the query window",
    )
    duty_cycle_pct: float = Field(
        default=0.0,
        description="Percentage of window minutes with active traffic",
    )
    forecast_24h_peak_tpm: int = Field(
        default=0,
        description="Holt double-exponential smoothing 24-step projected peak TPM",
    )
    forecast_24h_recommended_gsus: int = Field(
        default=0,
        description="Recommended GSUs (or replicas) for the Holt 24-step projected peak",
    )
    percentiles: dict[str, PercentileDetail] = Field(
        description=(
            "Primary percentile map (all_hours by default, or active_hours when active_only=True)"
        )
    )
    all_hours: dict[str, PercentileDetail] = Field(
        default_factory=dict,
        description="24x7 zero-padded percentile breakdown across all window minutes",
    )
    active_hours: dict[str, PercentileDetail] = Field(
        default_factory=dict,
        description="Active business-hours percentile breakdown across non-zero minutes only",
    )


class RecommendationResponse(StrictModel):
    project: str
    days: int
    active_only: bool = False
    by_region: bool = False
    mode: str = "gsu"
    models: list[ModelRecommendation]


class MicroBurstAudit(StrictModel):
    """Token-bucket enforcement audit (1s/10s 1P or 30s/60s 3P vs 60s Cloud Monitoring view)."""

    provisioned_gsus: int
    refill_window_seconds: int = Field(
        description="1s (>=50 GSUs) / 10s (<50 GSUs) on 1P Gemini; 30s short bucket on 3P Claude"
    )
    long_window_seconds: int | None = Field(
        default=None,
        description="Secondary 60s long-term bucket window on 3P dual-bucket enforcement",
    )
    enforcement_mode: str = Field(
        default="1p_subminute_bucket",
        description="'1p_subminute_bucket' (1s/10s + 5% credit) or '3p_dual_bucket_30s_60s'",
    )
    tps_per_gsu: float
    committed_tps_ceiling: float
    burst_credit_pct: float
    effective_window_tps_ceiling: float
    window_token_budget: float
    sixty_sec_average_tps: float
    apparent_60s_utilization_pct: float
    peak_window_tps: float
    peak_window_utilization_pct: float
    burst_spillover_tps: float
    triggers_429_or_spillover: bool
    illusion_detected: bool = Field(
        description=(
            "True when 60s utilization appears <70% yet sub-minute burst triggers spillover/429"
        )
    )
    gsus_required_for_zero_spillover: int


class PrefixCacheAudit(StrictModel):
    """Prefix-cache dilution audit comparing round-robin load balancing vs sticky routing."""

    total_input_tokens: int
    shared_prefix_ratio: float
    baseline_cache_hit_rate: float
    num_round_robin_endpoints: int
    requests_per_second: float
    effective_cache_hit_rate: float
    sticky_burndown_tokens: float
    round_robin_burndown_tokens: float
    burndown_inflation_multiplier: float
    sticky_gsus: int
    round_robin_gsus: int
    wasted_gsus: int
    gsu_savings_pct: float


class ModelTierPrefixCacheAudit(StrictModel):
    """Model-tier prefix-cache block alignment, Block-0 contamination, and routing audit."""

    model: str
    normalized_model: str
    checkpoint_granularity: str = Field(
        description="'block_4096' for flash/flash-lite; 'token_boundary' for argon/pro"
    )
    min_prefix_floor_tokens: int
    block_size_tokens: int
    total_input_tokens: int
    static_prefix_tokens: int
    dynamic_header_offset_tokens: int | None = None
    below_minimum_floor: bool
    block_zero_contaminated: bool
    aligned_cacheable_tokens: int
    trailing_uncached_prefix_tokens: int
    single_turn_request: bool
    dummy_turn_anchor_applied: bool
    strip_thought_signatures: bool
    routing_affinity_hit_rate: float
    effective_cache_hit_rate: float
    optimized_cache_hit_rate: float
    current_burndown_tokens: float
    optimized_burndown_tokens: float
    burndown_waste_pct: float
    prefill_spillover_threshold_pct: float
    fast_decode_speedup_multiplier: float
    fast_decode_cost_multiplier: float
    remediation_steps: list[str]


class ModelTierPrefixCacheRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    total_input_tokens: int = Field(default=18_000, ge=1, le=2_000_000)
    static_prefix_tokens: int = Field(default=14_000, ge=0, le=2_000_000)
    output_tokens: int = Field(default=800, ge=0, le=200_000)
    thoughts_tokens: int = Field(default=400, ge=0, le=200_000)
    dynamic_header_offset_tokens: int | None = Field(default=120, ge=0, le=2_000_000)
    single_turn_request: bool = False
    dummy_turn_anchor_applied: bool = False
    strip_thought_signatures: bool = False
    num_replicas: int = Field(default=8, ge=1, le=1_024)


class HybridCommitmentRecommendation(StrictModel):
    """Newsvendor quantile optimizer result for hybrid Base PT + Spillover sizing."""

    model: str
    tpm_per_gsu: int
    gsu_monthly_price_usd: float
    fsp_discount_pct: float
    effective_gsu_monthly_price_usd: float
    spillover_tier: str
    spillover_multiplier: float
    effective_spillover_per_1m_tokens_usd: float
    breakeven_utilization_pct: float
    critical_quantile: float
    optimal_gsus: int
    p50_gsus: int
    p90_gsus: int
    p99_gsus: int
    pt_base_monthly_cost_usd: float
    spillover_monthly_cost_usd: float
    total_hybrid_monthly_cost_usd: float
    paygo_100pct_monthly_cost_usd: float
    p99_pt_monthly_cost_usd: float
    savings_vs_paygo_usd: float
    savings_vs_paygo_pct: float
    savings_vs_p99_pt_usd: float
    savings_vs_p99_pt_pct: float


class OptimizeRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    tpm_series: list[BoundedTpmFloat] = Field(
        default_factory=lambda: [
            420_000.0,
            390_000.0,
            360_000.0,
            310_000.0,
            280_000.0,
            250_000.0,
            240_000.0,
            220_000.0,
            190_000.0,
            160_000.0,
            90_000.0,
            40_000.0,
        ],
        max_length=50_000,
    )
    gsu_monthly_price_usd: FinitePosFloat = 2_000.0
    paygo_per_1m_burndown_tokens_usd: FinitePosFloat = 0.50
    spillover_tier: SpilloverTier = "priority"
    fsp_discount_pct: float = Field(default=0.0, ge=0.0, le=50.0, allow_inf_nan=False)
    active_duty_cycle_pct: float = Field(
        default=100.0,
        gt=0.0,
        le=100.0,
        allow_inf_nan=False,
        description="Percentage of the 168h week represented by tpm_series (100 for 24x7 series)",
    )
    sixty_sec_tpm: BoundedTpmFloat | None = None
    peak_1s_tps: BoundedTpmFloat | None = None
    peak_10s_tps: BoundedTpmFloat | None = None
    peak_30s_tps: BoundedTpmFloat | None = None
    total_input_tokens: int = Field(default=20_000, ge=0, le=2_000_000)
    output_tokens: int = Field(default=1_000, ge=0, le=200_000)
    thoughts_tokens: int = Field(default=500, ge=0, le=200_000)
    cached_tokens: int = Field(default=16_000, ge=0, le=2_000_000)
    cache_write_5m_tokens: int = Field(default=0, ge=0, le=2_000_000)
    cache_write_1h_tokens: int = Field(default=0, ge=0, le=2_000_000)
    shared_prefix_ratio: float = Field(default=0.80, ge=0.0, le=1.0, allow_inf_nan=False)
    baseline_cache_hit_rate: float = Field(default=0.80, ge=0.0, le=1.0, allow_inf_nan=False)
    num_round_robin_endpoints: int = Field(default=8, ge=1, le=1_024)
    requests_per_second: float | None = Field(
        default=None,
        gt=0.0,
        le=100_000.0,
        allow_inf_nan=False,
        description="Rate for prefix-cache GSU counts; taken from the trace peak if omitted",
    )


class OptimizeResponse(StrictModel):
    model: str
    normalized_model: str
    weighted_burndown_tokens_per_request: float = 0.0
    commitment: HybridCommitmentRecommendation
    micro_burst: MicroBurstAudit | None = Field(
        default=None,
        description="Present only when the request carries a 60-second load or a measured peak",
    )
    prefix_cache: PrefixCacheAudit


class SelfHostedAuditRequest(StrictModel):
    profile: str = "llama-3.3-70b-fp8"
    tpm_series: list[BoundedTpmFloat] = Field(
        default_factory=lambda: [
            520_000.0,
            480_000.0,
            410_000.0,
            340_000.0,
            290_000.0,
            180_000.0,
        ],
        max_length=50_000,
    )
    min_replica_count: int = Field(default=2, ge=0, le=10_000)
    max_replica_count: int = Field(default=6, ge=1, le=10_000)
    observed_accelerator_duty_cycle_pct: float = Field(
        default=52.0, ge=0.0, le=100.0, allow_inf_nan=False
    )
    observed_vllm_kv_cache_usage_pct: float = Field(
        default=91.5, ge=0.0, le=100.0, allow_inf_nan=False
    )
    observed_vllm_requests_waiting: FiniteNonNegFloat = 14.0
    total_input_tokens: int = Field(default=20_000, ge=0, le=2_000_000)
    shared_prefix_ratio: float = Field(default=0.80, ge=0.0, le=1.0, allow_inf_nan=False)
    baseline_cache_hit_rate: float = Field(default=0.80, ge=0.0, le=1.0, allow_inf_nan=False)


class VllmParseRequest(StrictModel):
    raw_content: str = Field(max_length=5_000_000)
    default_model: str = "llama-3.3-70b-fp8"


class TenantChargebackRow(StrictModel):
    tenant: str
    consumed_burndown_tokens: float
    usage_share_pct: float
    unweighted_raw_token_share_pct: float = 0.0
    active_allocated_cost_usd: float
    unweighted_raw_token_cost_usd: float = 0.0
    raw_token_distortion_usd: float = 0.0
    naive_full_pool_cost_usd: float
    idle_penalty_avoided_usd: float


class FocusChargebackReport(StrictModel):
    """FOCUS 1.2 Two-Pool Chargeback Report (Active Consumed vs Unallocated Idle + Water-Fill)."""

    provisioned_gsus: int
    total_commitment_cost_usd: float
    active_utilization_pct: float
    effective_utilization_with_waterfill_pct: float
    active_consumed_cost_usd: float
    unallocated_idle_cost_before_waterfill_usd: float
    reclaimed_waterfill_cost_usd: float
    net_unallocated_idle_cost_usd: float
    waterfill_reclaimed_tokens: float
    tenants: list[TenantChargebackRow]


class ChargebackRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=10, ge=1, le=100_000)
    gsu_monthly_price_usd: FinitePosFloat = 2_000.0
    hourly_consumed_tpm_by_tenant: dict[str, HourlyTpmSeries | BoundedTpmFloat] = Field(
        max_length=MAX_SERIES_KEYS
    )
    tenant_raw_token_weights: dict[str, FinitePosFloat] | None = Field(
        default=None, max_length=MAX_SERIES_KEYS
    )
    async_batch_waterfill_tpm: BoundedTpmFloat = 0.0

    @model_validator(mode="after")
    def _bounded_work(self) -> Self:
        _reject_oversized_matrix(self.hourly_consumed_tpm_by_tenant)
        return self


class BatchScavengerAudit(StrictModel):
    """Batch API (0.5x) Double-Payment Trap vs $0 Online PT Scavenging Audit."""

    model: str
    provisioned_gsus: int
    pt_monthly_commitment_cost_usd: float
    daytime_active_utilization_pct: float
    idle_pt_monthly_burndown_tokens: float
    batch_monthly_burndown_tokens: float
    tokens_harvested_on_free_pt: float
    overflow_tokens_to_flex_paygo: float
    batch_api_extra_spend_usd: float
    online_dedicated_scavenger_extra_spend_usd: float
    online_hybrid_flex_scavenger_extra_spend_usd: float
    double_payment_waste_avoided_usd: float
    effective_utilization_after_scavenging_pct: float
    recommended_header_policy: str


class BatchScavengerRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=20, ge=1, le=100_000)
    gsu_monthly_price_usd: FinitePosFloat = 2_000.0
    daytime_active_utilization_pct: float = Field(
        default=24.0, ge=0.0, le=100.0, allow_inf_nan=False
    )
    batch_monthly_burndown_tokens: FiniteNonNegFloat = 45_000_000_000.0
    paygo_per_1m_burndown_tokens_usd: FinitePosFloat = 0.50


class DeadlineJobSpec(StrictModel):
    job_id: str
    value_usd: FinitePosFloat
    burndown_tokens: FinitePosFloat
    deadline_hour: int = Field(ge=1, le=168)


def make_default_deadline_jobs() -> list[DeadlineJobSpec]:
    """Return the canonical 3-job overnight residual scheduling portfolio."""
    return [
        DeadlineJobSpec(
            job_id="premarket-portfolio-briefings",
            value_usd=18_000.0,
            burndown_tokens=240_000_000.0,
            deadline_hour=6,
        ),
        DeadlineJobSpec(
            job_id="regulatory-risk-digest",
            value_usd=12_000.0,
            burndown_tokens=180_000_000.0,
            deadline_hour=5,
        ),
        DeadlineJobSpec(
            job_id="low-density-archive-sweep",
            value_usd=900.0,
            burndown_tokens=320_000_000.0,
            deadline_hour=6,
        ),
    ]


class ScheduledDeadlineJob(StrictModel):
    job_id: str
    value_usd: float
    burndown_tokens: float
    deadline_hour: int
    value_density_per_1m_tokens: float
    status: str = Field(
        description="'admitted_edf_pt' ($0 PT) or 'spilled_or_aborted' (0.5x Flex / early kill)"
    )
    routing_headers: dict[str, str]
    marginal_cost_usd: float


class ResidualScheduleReport(StrictModel):
    """3-Tier Residual Capacity Schedule (Revenue > Hard-Deadline EDF > Evergreen Water-Fill)."""

    provisioned_gsus: int
    safety_headroom_ratio: float
    horizon_hours: int
    total_residual_pt_tokens: float
    admitted_deadline_tokens: float
    spilled_deadline_tokens: float
    evergreen_waterfilled_tokens: float
    total_value_captured_usd: float
    flex_spillover_cost_usd: float
    jobs: list[ScheduledDeadlineJob]


class ResidualScheduleRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=20, ge=1, le=100_000)
    safety_headroom_ratio: float = Field(default=0.85, gt=0.1, le=1.0, allow_inf_nan=False)
    revenue_tpm_by_hour: list[BoundedTpmFloat] = Field(
        default_factory=lambda: list(DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR),
        min_length=1,
        max_length=168,
    )
    deadline_jobs: list[DeadlineJobSpec] = Field(
        default_factory=make_default_deadline_jobs,
        max_length=200,
    )
    evergreen_backfill_tokens: FiniteNonNegFloat = 250_000_000.0
    paygo_per_1m_burndown_tokens_usd: FinitePosFloat = 0.50

    @model_validator(mode="after")
    def _job_ids_are_unique(self) -> Self:
        # Admission is tracked by job_id: a repeated id would admit both jobs on one budget.
        seen: set[str] = set()
        for job in self.deadline_jobs:
            if job.job_id in seen:
                raise ValueError(
                    f"Duplicate job_id {job.job_id!r}: every deadline job needs its own id"
                )
            seen.add(job.job_id)
        return self


class FolderPoolingReport(StrictModel):
    """Shared Folder RHPT Pooling vs Siloed Per-Project PT Stranding Audit."""

    model: str
    percentile_target: int
    project_count: int
    siloed_gsus_by_project: dict[str, int]
    siloed_total_gsus: int
    pooled_folder_gsus: int
    stranded_gsus_saved: int
    coincidence_diversity_factor: float
    monthly_savings_usd: float
    annual_savings_usd: float
    governance_notes: list[str]


class FolderPoolingRequest(StrictModel):
    model: str = "gemini-3.8-flash"
    percentile_target: int = Field(default=90, ge=1, le=100)
    gsu_monthly_price_usd: FinitePosFloat = 2_000.0
    hourly_tpm_by_project: dict[str, HourlyTpmSeries] = Field(
        max_length=MAX_SERIES_KEYS,
        default_factory=lambda: {
            "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
            "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
            "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
        },
    )

    @model_validator(mode="after")
    def _bounded_work(self) -> Self:
        _reject_oversized_matrix(self.hourly_tpm_by_project)
        return self


class FinOpsKpiScorecard(StrictModel):
    """5 Standardized FinOps for AI Unit Economics KPIs (with bq-eval CPVO bridge)."""

    cpwt_1m_usd: float = Field(description="Cost per 1M Weighted Burndown Tokens (USD)")
    cpvo_usd: float = Field(description="Cost per Verified Outcome (USD) — bridges to bq-eval")
    ter_ratio: float = Field(description="Token Efficiency Ratio (0..1)")
    ter_pct: float = Field(description="Token Efficiency Ratio (%)")
    csr_pct: float = Field(description="Prefix Cache Hit Savings Ratio (%)")
    ecur_168h_pct: float = Field(description="Effective 168-Hour Commitment Utilization Rate (%)")
    total_amortized_cost_usd: float
    total_weighted_burndown_tokens: float
    verified_outcomes: int


class FinOpsKpiRequest(StrictModel):
    total_amortized_cost_usd: FinitePosFloat = 24_500.0
    uncached_input_tokens: int = Field(default=1_200_000_000, ge=0)
    cached_input_tokens: int = Field(default=3_800_000_000, ge=0)
    delivered_output_tokens: int = Field(default=420_000_000, ge=0)
    thinking_tokens: int = Field(default=180_000_000, ge=0)
    retry_and_judge_tokens: int = Field(default=95_000_000, ge=0)
    verified_outcomes: int = Field(default=140_000, ge=1)
    hourly_reserved_burndown_capacity: FinitePosFloat = 72_540_000.0
    weekly_consumed_burndown_tokens: FiniteNonNegFloat = 6_950_000_000.0


class DisaggregatedPoolSizing(StrictModel):
    """Disaggregated Prefill / Decode GPU/TPU Pool Sizing Report."""

    profile: str
    accelerator_type: str
    accelerators_per_replica: int
    peak_qps: float
    avg_input_tokens: int
    avg_output_tokens: int
    prefill_replicas: int
    decode_replicas: int
    total_disaggregated_replicas: int
    monolithic_replicas: int
    replica_delta: int
    disaggregated_monthly_cost_usd: float
    monolithic_monthly_cost_usd: float
    ttft_headroom_pct: float
    tpot_headroom_pct: float
    recommendation: str


class DisaggregatedPoolRequest(StrictModel):
    profile: str = "llama-3.3-70b-fp8"
    peak_qps: float = Field(default=18.0, gt=0.0, le=100_000.0, allow_inf_nan=False)
    avg_input_tokens: int = Field(default=6_400, ge=1, le=2_000_000)
    avg_output_tokens: int = Field(default=450, ge=1, le=200_000)
    target_ttft_ms: float = Field(default=350.0, gt=0.0, le=60_000.0, allow_inf_nan=False)
    target_tpot_ms: float = Field(default=25.0, gt=0.0, le=10_000.0, allow_inf_nan=False)


class GatewayHeaderPolicy(StrictModel):
    tier_id: int
    scenario: str
    request_type_header: str
    shared_request_type_header: str
    uta_rank: str
    billing_and_capacity_behavior: str


class LiteLLMScaleAudit(StrictModel):
    """High-QPS LiteLLM Proxy Scalability & Bottleneck Audit (>500-2,000 RPS)."""

    target_rps: int
    concurrent_sse_streams: int
    pods: int
    uvicorn_workers_per_pod: int
    total_prisma_rust_engines: int
    recommended_pgbouncer_pool_size: int
    recommended_postgres_max_connections: int
    use_redis_transaction_buffer: bool
    redis_buffer_time_seconds: int
    rust_tokenizer_enabled: bool
    anti_blackhole_cooldown_seconds: int
    anti_blackhole_allowed_fails: int
    scale_status: str = Field(
        description="'healthy', 'tune_required', or 'migrate_dataplane_to_envoy'"
    )
    bottleneck_warnings: list[str]


class GatewayConfigBundle(StrictModel):
    """Multi-Gateway Declarative Configuration & Audit Bundle (LiteLLM + Envoy + K8s epp)."""

    model: str
    normalized_model: str
    provisioned_gsus: int
    refill_window_seconds: int
    committed_burndown_tpm: int
    header_policies: list[GatewayHeaderPolicy]
    litellm_audit: LiteLLMScaleAudit
    litellm_config_yaml: str
    litellm_burndown_callback_py: str
    envoy_ai_gateway_yaml: str
    gke_epp_inference_pool_yaml: str


class GatewayConfigRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    project_id: str = Field(default="prod-genai-pt-host", pattern=GCP_PROJECT_ID_PATTERN)
    location: str = Field(default="global", pattern=GCP_LOCATION_PATTERN)
    provisioned_gsus: int = Field(default=20, ge=1, le=100_000)
    target_rps: int = Field(default=650, ge=1, le=1_000_000)
    concurrent_sse_streams: int = Field(default=400, ge=0, le=1_000_000)
    pods: int = Field(default=12, ge=1, le=10_000)
    uvicorn_workers_per_pod: int = Field(default=4, ge=1, le=128)
    self_hosted_model: str = Field(default="llama-3.3-70b-fp8", pattern=K8S_NAME_PATTERN)
    business_unit_shares: int = Field(default=4, ge=1, le=1_000)
