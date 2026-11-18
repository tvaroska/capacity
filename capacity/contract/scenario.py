"""Scenario schema (`--scenario-file`) and typed whole-scenario report contract."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from capacity.contract.common import (
    MAX_SERIES_CELLS,
    MAX_SERIES_KEYS,
    MAX_SERIES_POINTS,
    MODEL_ID_PATTERN,
    PACKAGE_VERSION,
    BoundedTpmFloat,
    LifecycleTrack,
    StrictModel,
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
)
from capacity.contract.results import (
    BatchScavengerAudit,
    DisaggregatedPoolSizing,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    GatewayConfigBundle,
    HybridCommitmentRecommendation,
    MicroBurstAudit,
    ModelTierPrefixCacheAudit,
    PrefixCacheAudit,
    RecommendationResponse,
    ResidualScheduleReport,
    SelfHostedEndpointAudit,
)

PlatformBrand = Literal["Gemini Platform", "Vertex AI"]
RenewalMode = Literal["fixed_term", "auto_renew", "mixed", "none"]
GatewayPresentationMode = Literal["oss_stack", "central_proxy"]
FinOpsPoolingMode = Literal["folder_rhpt", "1p_vs_3p_hub"]

MAX_MINUTE_POINTS = MAX_SERIES_POINTS
MinuteTpmSeries = Annotated[
    list[BoundedTpmFloat], Field(min_length=1, max_length=MAX_MINUTE_POINTS)
]


class ScenarioPresentation(StrictModel):
    title: str = Field(
        default_factory=lambda: f"Capacity Showcase Workbench (v{PACKAGE_VERSION})",
        min_length=3,
        max_length=140,
    )
    subtitle: str = Field(
        default=(
            "Managed Gemini Platform GSUs + Self-Hosted GPU/vLLM • "
            "Sub-Second Burst & Prefix Cache • LiteLLM / Envoy / K8s epp • FOCUS 1.2 FinOps"
        ),
        min_length=3,
        max_length=240,
    )
    platform_brand: PlatformBrand = "Gemini Platform"
    primary_badge: str = Field(default="GM3 Light Tier-2 Showcase", max_length=80)
    secondary_badge: str = Field(default="FOCUS 1.2 & bq-eval CPVO Ready", max_length=80)
    wave_badge_label: str = Field(default="8x5 Business Wave", max_length=80)


class ModelInventoryAnnotation(StrictModel):
    contracted_gsus: int | None = Field(default=None, ge=0, le=100_000)
    scope_label: str | None = Field(default=None, max_length=80)
    spillover_requests: int | None = Field(default=None, ge=0)
    http_429_count: int | None = Field(default=None, ge=0)
    lifecycle_track: LifecycleTrack | None = None
    renewal_mode: RenewalMode | None = None
    expiry_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    expiring_fixed_gsus: int | None = Field(default=None, ge=0, le=100_000)


class ScenarioGsuSizing(StrictModel):
    project_label: str = Field(default="demo-enterprise-ai", min_length=1, max_length=80)
    days: int = Field(default=7, ge=1, le=365)
    by_region: bool = True
    active_only: bool = True
    forecast_hours: int = Field(default=24, ge=0, le=168)
    minute_tpm_series: dict[str, MinuteTpmSeries] = Field(min_length=1, max_length=MAX_SERIES_KEYS)
    gsu_capacity_overrides: dict[str, Annotated[int, Field(ge=1, le=100_000_000)]] = Field(
        default_factory=dict, max_length=200
    )
    inventory_annotations: dict[str, ModelInventoryAnnotation] = Field(
        default_factory=dict, max_length=MAX_SERIES_KEYS
    )

    @model_validator(mode="after")
    def _bounded_matrix(self) -> Self:
        longest = max((len(v) for v in self.minute_tpm_series.values()), default=1)
        cells = len(self.minute_tpm_series) * max(1, longest)
        if cells > MAX_SERIES_CELLS:
            raise ValueError(
                f"{len(self.minute_tpm_series)} series padded to {longest} minutes is "
                f"{cells:,} cells; the limit is {MAX_SERIES_CELLS:,}"
            )
        return self


class ScenarioSelfHosted(StrictModel):
    enabled: bool = True
    audit: SelfHostedAuditRequest | None = None
    disaggregated: DisaggregatedPoolRequest = Field(default_factory=DisaggregatedPoolRequest)


class ScenarioBurstAndCache(StrictModel):
    default_model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    provisioned_gsus: int = Field(default=10, ge=1, le=100_000)
    sixty_sec_tpm: BoundedTpmFloat = 750_000.0
    peak_1s_tps: BoundedTpmFloat = 25_000.0
    peak_10s_tps: BoundedTpmFloat | None = None
    peak_30s_tps: BoundedTpmFloat | None = None
    prefix_dilution: OptimizeRequest | None = None
    flash_tier_audit: ModelTierPrefixCacheRequest = Field(
        default_factory=lambda: ModelTierPrefixCacheRequest(
            model="gemini-3.8-flash", single_turn_request=True
        )
    )
    pro_tier_audit: ModelTierPrefixCacheRequest = Field(
        default_factory=lambda: ModelTierPrefixCacheRequest(
            model="gemini-4-argon",
            dynamic_header_offset_tokens=None,
            single_turn_request=False,
            dummy_turn_anchor_applied=True,
        )
    )


class ScenarioGateway(StrictModel):
    presentation_mode: GatewayPresentationMode = "oss_stack"
    proxy_label: str = Field(default="Central Enterprise LLM Gateway", min_length=2, max_length=80)
    config: GatewayConfigRequest = Field(default_factory=GatewayConfigRequest)


class ScenarioFinOps(StrictModel):
    pooling_mode: FinOpsPoolingMode = "folder_rhpt"
    commitment: OptimizeRequest | None = None
    chargeback: ChargebackRequest | None = None
    scavenger: BatchScavengerRequest = Field(default_factory=BatchScavengerRequest)
    residual_schedule: ResidualScheduleRequest | None = None
    folder_pooling: FolderPoolingRequest | None = None
    kpis: FinOpsKpiRequest = Field(default_factory=FinOpsKpiRequest)


class ShowcaseScenario(StrictModel):
    schema_version: Literal["capacity.scenario.v1"] = "capacity.scenario.v1"
    presentation: ScenarioPresentation = Field(default_factory=ScenarioPresentation)
    gsu_sizing: ScenarioGsuSizing
    self_hosted: ScenarioSelfHosted = Field(default_factory=ScenarioSelfHosted)
    burst_and_cache: ScenarioBurstAndCache = Field(default_factory=ScenarioBurstAndCache)
    gateway: ScenarioGateway = Field(default_factory=ScenarioGateway)
    finops: ScenarioFinOps = Field(default_factory=ScenarioFinOps)


class ShowcaseReport(StrictModel):
    """Typed whole-scenario evaluation result shared by CLI, API, and HTML workbench."""

    version: str = PACKAGE_VERSION
    scenario_mode: bool = False
    scenario: ShowcaseScenario
    simulator_options: list[tuple[str, str]] = Field(default_factory=list)
    selected_simulator_model: str = "gemini-3.8-flash"
    gsu_recommendations: RecommendationResponse
    self_hosted_audit: SelfHostedEndpointAudit | None = None
    disaggregated_sizing: DisaggregatedPoolSizing | None = None
    micro_burst: MicroBurstAudit
    prefix_cache_rr: PrefixCacheAudit
    prefix_cache_flash: ModelTierPrefixCacheAudit
    prefix_cache_argon: ModelTierPrefixCacheAudit
    gateway: GatewayConfigBundle
    commitment: HybridCommitmentRecommendation
    chargeback: FocusChargebackReport
    scavenger: BatchScavengerAudit
    residual_schedule: ResidualScheduleReport
    folder_pooling: FolderPoolingReport
    finops_kpis: FinOpsKpiScorecard
