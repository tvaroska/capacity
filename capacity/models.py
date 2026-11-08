"""Pydantic wire models for capacity recommendations, micro-burst, Newsvendor, and FOCUS 1.2."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
