"""Typed Pydantic v2 response and audit models returned by `capacity` operations."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from capacity.contract.common import CapacityModeLiteral, StrictModel


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
        description="Holt double-exponential smoothing projected peak TPM",
    )
    forecast_24h_recommended_gsus: int = Field(
        default=0,
        description="Recommended GSUs (or replicas) for the Holt projected peak",
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
    mode: CapacityModeLiteral = "gsu"
    forecast_hours: int = Field(default=24, ge=0, le=168)
    requested_percentiles: list[int] = Field(default_factory=lambda: [50, 75, 90, 95])
    models: list[ModelRecommendation]


class GcpProjectAccessStatus(StrictModel):
    monitoring_api_enabled: bool = False
    has_viewer_role: bool = False
    ok: bool = False
    error: str | None = None


class GcpAccessCheckReport(StrictModel):
    authenticated: bool = False
    account: str | None = None
    ok: bool = False
    error: str | None = None
    projects: dict[str, GcpProjectAccessStatus] = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)


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


class SelfHostedProfile(StrictModel):
    """Hardware and serving throughput profile for a self-hosted open-weight model replica."""

    profile_id: str
    model_family: str
    machine_type: str
    accelerator_type: str
    accelerators_per_replica: int = Field(ge=1)
    tps_per_replica: float = Field(
        gt=0.0, description="Sustained weighted burndown tokens/sec per replica"
    )
    max_kv_cache_tokens: int = Field(
        gt=0, description="Usable KV-cache token capacity per replica in HBM"
    )
    reserved_replica_monthly_usd: float = Field(
        gt=0.0, description="Monthly cost per 1Y CUD / reserved replica"
    )
    on_demand_replica_hourly_usd: float = Field(
        gt=0.0, description="Hourly cost per elastic burst replica"
    )

    @property
    def tpm_per_replica(self) -> int:
        return round(self.tps_per_replica * 60.0)


class SelfHostedEndpointAudit(StrictModel):
    """Comprehensive capacity, KV-cache, quota-lock, and Newsvendor audit for GPU replicas."""

    profile_id: str
    machine_type: str
    accelerator_type: str
    accelerators_per_replica: int
    tps_per_replica: float
    tpm_per_replica: int
    min_replica_count: int
    max_replica_count: int
    locked_regional_accelerators: int
    active_baseline_accelerators: int
    stranded_quota_accelerators: int
    observed_accelerator_duty_cycle_pct: float
    observed_vllm_kv_cache_usage_pct: float
    observed_vllm_requests_waiting: float
    duty_cycle_illusion_detected: bool
    kv_preemption_risk: bool
    recommended_autoscaler_metric: str
    optimal_reserved_replicas: int
    p99_peak_replicas: int
    breakeven_replica_utilization_pct: float
    reserved_base_monthly_cost_usd: float
    elastic_burst_monthly_cost_usd: float
    total_hybrid_monthly_cost_usd: float
    all_on_demand_monthly_cost_usd: float
    p99_static_reserved_monthly_cost_usd: float
    scale_to_zero_nonprod_savings_usd: float
    prefix_cache: PrefixCacheAudit


class VllmTelemetrySnapshot(StrictModel):
    """Normalized telemetry extracted from Prometheus scrapes, OTel spans, or inference logs."""

    source_format: str
    model: str
    tpm_series: list[float]
    window_start_utc: datetime | None = None
    prompt_tokens_total: int
    cached_tokens_total: int
    cache_write_5m_tokens_total: int = 0
    cache_write_1h_tokens_total: int = 0
    completion_tokens_total: int
    thoughts_tokens_total: int = 0
    weighted_burndown_tokens_total: float
    observed_kv_cache_usage_pct: float
    observed_requests_waiting: float
    observed_cache_hit_rate: float
    hourly_consumed_tpm_by_tenant: dict[str, list[float]]
    total_requests: int = 0
    spillover_requests: int = 0
    http_429_count: int = 0
    peak_1s_burndown_tps: float = 0.0
    peak_10s_burndown_tps: float = 0.0
    peak_30s_burndown_tps: float = 0.0
    minute_tpm_by_model_region: dict[str, list[float]] = Field(default_factory=dict)
    requests_by_model_region: dict[str, int] = Field(default_factory=dict)
    spillover_by_model_region: dict[str, int] = Field(default_factory=dict)
    http_429_by_model_region: dict[str, int] = Field(default_factory=dict)


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
    unit_price_usd: float = 2_000.0
    mode: CapacityModeLiteral = "gsu"
    total_commitment_cost_usd: float
    active_utilization_pct: float
    effective_utilization_with_waterfill_pct: float
    active_consumed_cost_usd: float
    unallocated_idle_cost_before_waterfill_usd: float
    reclaimed_waterfill_cost_usd: float
    net_unallocated_idle_cost_usd: float
    waterfill_reclaimed_tokens: float
    tenants: list[TenantChargebackRow]


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
