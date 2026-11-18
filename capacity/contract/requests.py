"""Checked Pydantic v2 request models shared by CLI, API, and scenario evaluation."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from capacity.contract.common import (
    DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR,
    GCP_LOCATION_PATTERN,
    GCP_PROJECT_ID_PATTERN,
    K8S_NAME_PATTERN,
    MAX_SERIES_KEYS,
    MAX_SERIES_POINTS,
    MODEL_ID_PATTERN,
    BoundedTpmFloat,
    CapacityModeLiteral,
    FiniteNonNegFloat,
    FinitePosFloat,
    HourlyTpmSeries,
    SpilloverTier,
    StrictModel,
    reject_oversized_matrix,
)


class MicroBurstRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    provisioned_gsus: int = Field(default=10, ge=0, le=100_000)
    sixty_sec_tpm: BoundedTpmFloat
    peak_1s_tps: BoundedTpmFloat
    peak_10s_tps: BoundedTpmFloat | None = None
    peak_30s_tps: BoundedTpmFloat | None = None


class ModelTierPrefixCacheRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    total_input_tokens: int = Field(default=18_000, ge=1, le=2_000_000)
    static_prefix_tokens: int = Field(default=14_000, ge=0, le=2_000_000)
    output_tokens: int = Field(default=800, ge=0, le=200_000)
    thoughts_tokens: int = Field(default=400, ge=0, le=200_000)
    dynamic_header_offset_tokens: int | None = Field(default=120, ge=0, le=2_000_000)
    single_turn_request: bool = False
    dummy_turn_anchor_applied: bool = False
    strip_thought_signatures: bool = False
    num_replicas: int = Field(default=8, ge=1, le=1_024)


class OptimizeRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    tpm_series: list[BoundedTpmFloat] = Field(min_length=1, max_length=MAX_SERIES_POINTS)
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


class SelfHostedAuditRequest(StrictModel):
    profile: str = Field(default="llama-3.3-70b-fp8", pattern=MODEL_ID_PATTERN)
    tpm_series: list[BoundedTpmFloat] = Field(min_length=1, max_length=MAX_SERIES_POINTS)
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
    default_model: str = Field(default="llama-3.3-70b-fp8", pattern=MODEL_ID_PATTERN)


class ChargebackRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    mode: CapacityModeLiteral = "gsu"
    provisioned_gsus: int = Field(default=10, ge=1, le=100_000)
    gsu_monthly_price_usd: FinitePosFloat | None = Field(
        default=None,
        description="Monthly price per GSU or GPU replica (defaults to 2,000 GSU or profile price)",
    )
    hourly_consumed_tpm_by_tenant: dict[str, HourlyTpmSeries | BoundedTpmFloat] = Field(
        min_length=1,
        max_length=MAX_SERIES_KEYS,
    )
    tenant_raw_token_weights: dict[str, FinitePosFloat] | None = Field(
        default=None, max_length=MAX_SERIES_KEYS
    )
    async_batch_waterfill_tpm: BoundedTpmFloat = 0.0

    @model_validator(mode="after")
    def _bounded_work(self) -> Self:
        reject_oversized_matrix(self.hourly_consumed_tpm_by_tenant)
        return self


class BatchScavengerRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
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


class ResidualScheduleRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    provisioned_gsus: int = Field(default=20, ge=1, le=100_000)
    safety_headroom_ratio: float = Field(default=0.85, gt=0.1, le=1.0, allow_inf_nan=False)
    revenue_tpm_by_hour: list[BoundedTpmFloat] = Field(
        default_factory=lambda: list(DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR),
        min_length=1,
        max_length=168,
    )
    deadline_jobs: list[DeadlineJobSpec] = Field(max_length=200)
    evergreen_backfill_tokens: FiniteNonNegFloat = 250_000_000.0
    paygo_per_1m_burndown_tokens_usd: FinitePosFloat = 0.50

    @model_validator(mode="after")
    def _job_ids_are_unique(self) -> Self:
        seen: set[str] = set()
        for job in self.deadline_jobs:
            if job.job_id in seen:
                raise ValueError(
                    f"Duplicate job_id {job.job_id!r}: every deadline job needs its own id"
                )
            seen.add(job.job_id)
        return self


class FolderPoolingRequest(StrictModel):
    model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    percentile_target: int = Field(default=90, ge=1, le=100)
    gsu_monthly_price_usd: FinitePosFloat = 2_000.0
    hourly_tpm_by_project: dict[str, HourlyTpmSeries] = Field(
        min_length=1,
        max_length=MAX_SERIES_KEYS,
    )

    @model_validator(mode="after")
    def _bounded_work(self) -> Self:
        reject_oversized_matrix(self.hourly_tpm_by_project)
        return self


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


class DisaggregatedPoolRequest(StrictModel):
    profile: str = Field(default="llama-3.3-70b-fp8", pattern=MODEL_ID_PATTERN)
    peak_qps: float = Field(default=18.0, gt=0.0, le=100_000.0, allow_inf_nan=False)
    avg_input_tokens: int = Field(default=6_400, ge=1, le=2_000_000)
    avg_output_tokens: int = Field(default=450, ge=1, le=200_000)
    target_ttft_ms: float = Field(default=350.0, gt=0.0, le=60_000.0, allow_inf_nan=False)
    target_tpot_ms: float = Field(default=25.0, gt=0.0, le=10_000.0, allow_inf_nan=False)


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
