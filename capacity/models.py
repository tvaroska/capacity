"""Pydantic wire models for capacity recommendations, micro-burst, Newsvendor, and FOCUS 1.2."""

from __future__ import annotations

from pydantic import BaseModel, Field

# These values are interpolated into generated YAML/Kubernetes manifests, so they must not be able
# to carry quotes, newlines, or YAML syntax.
GCP_PROJECT_ID_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
GCP_LOCATION_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.@/-]{0,127}$"
K8S_NAME_PATTERN = r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$"


class DictAccessModel(BaseModel):
    """Base Pydantic model supporting both attribute and dictionary-key lookups."""

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class PercentileDetail(DictAccessModel):
    tpm: int = Field(description="Tokens per minute at this percentile")
    recommended_gsus: int = Field(
        description="Recommended GSUs (or GPU replicas in self-hosted mode)"
    )


class ModelRecommendation(DictAccessModel):
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
            "Sustained tokens-per-minute capacity per 1 GSU (or 1 GPU replica), "
            "or None if unknown"
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
            "Primary percentile map (all_hours by default, "
            "or active_hours when active_only=True)"
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


class RecommendationResponse(DictAccessModel):
    project: str
    days: int
    active_only: bool = False
    by_region: bool = False
    mode: str = "gsu"
    models: list[ModelRecommendation]


class MicroBurstAudit(DictAccessModel):
    """Sub-minute token-bucket enforcement audit (1s vs 10s window vs 60s Cloud Monitoring view)."""

    provisioned_gsus: int
    refill_window_seconds: int = Field(
        description="1s window for >=50 GSUs; 10s window for <50 GSUs"
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
            "True when 60s utilization appears <70% yet sub-minute burst "
            "triggers spillover/429"
        )
    )
    gsus_required_for_zero_spillover: int


class PrefixCacheAudit(DictAccessModel):
    """Prefix-cache dilution audit comparing round-robin load balancing vs sticky routing."""

    total_input_tokens: int
    shared_prefix_ratio: float
    baseline_cache_hit_rate: float
    num_round_robin_endpoints: int
    effective_cache_hit_rate: float
    sticky_burndown_tokens: float
    round_robin_burndown_tokens: float
    burndown_inflation_multiplier: float
    sticky_gsus: int
    round_robin_gsus: int
    wasted_gsus: int
    gsu_savings_pct: float


class ModelTierPrefixCacheAudit(DictAccessModel):
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


class ModelTierPrefixCacheRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    total_input_tokens: int = Field(default=18_000, ge=1)
    static_prefix_tokens: int = Field(default=14_000, ge=0)
    output_tokens: int = Field(default=800, ge=0)
    thoughts_tokens: int = Field(default=400, ge=0)
    dynamic_header_offset_tokens: int | None = Field(default=120, ge=0)
    single_turn_request: bool = False
    dummy_turn_anchor_applied: bool = False
    strip_thought_signatures: bool = False
    num_replicas: int = Field(default=8, ge=1)


class HybridCommitmentRecommendation(DictAccessModel):
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


class OptimizeRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    tpm_series: list[float] = Field(
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
        ]
    )
    gsu_monthly_price_usd: float = Field(default=2_000.0, gt=0)
    paygo_per_1m_burndown_tokens_usd: float = Field(default=0.50, gt=0)
    spillover_tier: str = Field(default="priority")
    fsp_discount_pct: float = Field(default=0.0, ge=0.0, le=50.0)
    sixty_sec_tpm: float | None = None
    peak_1s_tps: float | None = None
    peak_10s_tps: float | None = None
    total_input_tokens: int = Field(default=20_000, ge=0)
    output_tokens: int = Field(default=1_000, ge=0)
    thoughts_tokens: int = Field(default=500, ge=0)
    cached_tokens: int = Field(default=16_000, ge=0)
    shared_prefix_ratio: float = Field(default=0.80, ge=0.0, le=1.0)
    baseline_cache_hit_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    num_round_robin_endpoints: int = Field(default=8, ge=1)


class OptimizeResponse(DictAccessModel):
    model: str
    normalized_model: str
    weighted_burndown_tokens_per_request: float = 0.0
    commitment: HybridCommitmentRecommendation
    micro_burst: MicroBurstAudit
    prefix_cache: PrefixCacheAudit


class TenantChargebackRow(DictAccessModel):
    tenant: str
    consumed_burndown_tokens: float
    usage_share_pct: float
    unweighted_raw_token_share_pct: float = 0.0
    active_allocated_cost_usd: float
    unweighted_raw_token_cost_usd: float = 0.0
    raw_token_distortion_usd: float = 0.0
    naive_full_pool_cost_usd: float
    idle_penalty_avoided_usd: float


class FocusChargebackReport(DictAccessModel):
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


class ChargebackRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=10, ge=1)
    gsu_monthly_price_usd: float = Field(default=2_000.0, gt=0)
    hourly_consumed_tpm_by_tenant: dict[str, list[float] | float]
    tenant_raw_token_weights: dict[str, float] | None = None
    async_batch_waterfill_tpm: float = Field(default=0.0, ge=0.0)


class BatchScavengerAudit(DictAccessModel):
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


class BatchScavengerRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=20, ge=1)
    gsu_monthly_price_usd: float = Field(default=2_000.0, gt=0)
    daytime_active_utilization_pct: float = Field(default=24.0, ge=0.0, le=100.0)
    batch_monthly_burndown_tokens: float = Field(default=45_000_000_000.0, ge=0.0)
    paygo_per_1m_burndown_tokens_usd: float = Field(default=0.50, gt=0.0)


class DeadlineJobSpec(DictAccessModel):
    job_id: str
    value_usd: float = Field(gt=0.0)
    burndown_tokens: float = Field(gt=0.0)
    deadline_hour: int = Field(ge=1)


class ScheduledDeadlineJob(DictAccessModel):
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


class ResidualScheduleReport(DictAccessModel):
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


class ResidualScheduleRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    provisioned_gsus: int = Field(default=20, ge=1)
    safety_headroom_ratio: float = Field(default=0.85, gt=0.1, le=1.0)
    revenue_tpm_by_hour: list[float] = Field(
        default_factory=lambda: [
            600_000.0,
            400_000.0,
            300_000.0,
            250_000.0,
            200_000.0,
            350_000.0,
            900_000.0,
            1_800_000.0,
        ]
    )
    deadline_jobs: list[DeadlineJobSpec] = Field(
        default_factory=lambda: [
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
    )
    evergreen_backfill_tokens: float = Field(default=250_000_000.0, ge=0.0)
    paygo_per_1m_burndown_tokens_usd: float = Field(default=0.50, gt=0.0)


class FolderPoolingReport(DictAccessModel):
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


class FolderPoolingRequest(DictAccessModel):
    model: str = "gemini-3.8-flash"
    percentile_target: int = Field(default=90, ge=1, le=100)
    gsu_monthly_price_usd: float = Field(default=2_000.0, gt=0.0)
    hourly_tpm_by_project: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
            "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
            "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
        }
    )


class FinOpsKpiScorecard(DictAccessModel):
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


class FinOpsKpiRequest(DictAccessModel):
    total_amortized_cost_usd: float = Field(default=24_500.0, gt=0.0)
    uncached_input_tokens: int = Field(default=1_200_000_000, ge=0)
    cached_input_tokens: int = Field(default=3_800_000_000, ge=0)
    delivered_output_tokens: int = Field(default=420_000_000, ge=0)
    thinking_tokens: int = Field(default=180_000_000, ge=0)
    retry_and_judge_tokens: int = Field(default=95_000_000, ge=0)
    verified_outcomes: int = Field(default=140_000, ge=1)
    hourly_reserved_burndown_capacity: float = Field(default=72_540_000.0, gt=0.0)
    weekly_consumed_burndown_tokens: float = Field(default=6_950_000_000.0, ge=0.0)


class DisaggregatedPoolSizing(DictAccessModel):
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


class DisaggregatedPoolRequest(DictAccessModel):
    profile: str = "llama-3.3-70b-fp8"
    peak_qps: float = Field(default=18.0, gt=0.0)
    avg_input_tokens: int = Field(default=6_400, ge=1)
    avg_output_tokens: int = Field(default=450, ge=1)
    target_ttft_ms: float = Field(default=350.0, gt=0.0)
    target_tpot_ms: float = Field(default=25.0, gt=0.0)


class GatewayHeaderPolicy(DictAccessModel):
    tier_id: int
    scenario: str
    request_type_header: str
    shared_request_type_header: str
    uta_rank: str
    billing_and_capacity_behavior: str


class LiteLLMScaleAudit(DictAccessModel):
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


class GatewayConfigBundle(DictAccessModel):
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


class GatewayConfigRequest(DictAccessModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    project_id: str = Field(default="prod-genai-pt-host", pattern=GCP_PROJECT_ID_PATTERN)
    location: str = Field(default="global", pattern=GCP_LOCATION_PATTERN)
    provisioned_gsus: int = Field(default=20, ge=1)
    target_rps: int = Field(default=650, ge=1)
    concurrent_sse_streams: int = Field(default=400, ge=0)
    pods: int = Field(default=12, ge=1)
    uvicorn_workers_per_pod: int = Field(default=4, ge=1)
    self_hosted_model: str = Field(default="llama-3.3-70b-fp8", pattern=K8S_NAME_PATTERN)
