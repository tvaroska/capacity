"""Scenario contract (`--scenario-file`), sample datasets, and typed scenario evaluator."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from capacity.burst import (
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import ASSUMED_PEAK_TO_MEAN_RATIO, build_response
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.forecasting import (
    DEFAULT_TRACE_EPOCH_UTC,
    SparseSeriesMap,
    build_demo_throughput,
    minute_list_to_sparse,
)
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import (
    LifecycleTrack,
    ModelCatalog,
    build_model_catalog,
    infer_lifecycle_track,
    is_partner_claude,
    normalize_model_id,
)
from capacity.models import (
    MAX_SERIES_CELLS,
    MAX_SERIES_KEYS,
    MAX_SERIES_POINTS,
    MODEL_ID_PATTERN,
    PACKAGE_VERSION,
    BatchScavengerAudit,
    BatchScavengerRequest,
    BoundedTpmFloat,
    ChargebackRequest,
    DeadlineJobSpec,
    DisaggregatedPoolRequest,
    DisaggregatedPoolSizing,
    FinOpsKpiRequest,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    FolderPoolingRequest,
    GatewayConfigBundle,
    GatewayConfigRequest,
    HybridCommitmentRecommendation,
    MicroBurstAudit,
    MicroBurstRequest,
    ModelTierPrefixCacheAudit,
    ModelTierPrefixCacheRequest,
    OptimizeRequest,
    PrefixCacheAudit,
    RecommendationResponse,
    ResidualScheduleReport,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    SeriesKey,
    StrictModel,
)
from capacity.self_hosted import (
    SelfHostedEndpointAudit,
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)
from capacity.telemetry_parsers import VllmTelemetrySnapshot

PlatformBrand = Literal["Gemini Platform", "Vertex AI"]
RenewalMode = Literal["fixed_term", "auto_renew", "mixed", "none"]
GatewayPresentationMode = Literal["oss_stack", "central_proxy"]
FinOpsPoolingMode = Literal["folder_rhpt", "1p_vs_3p_hub"]

MAX_MINUTE_POINTS = MAX_SERIES_POINTS
MinuteTpmSeries = Annotated[
    list[BoundedTpmFloat], Field(min_length=1, max_length=MAX_MINUTE_POINTS)
]

# Canonical sample datasets kept in one place for `capacity demo` and `--demo` CLI switches.
DEMO_OPTIMIZE_SERIES: tuple[float, ...] = (
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
)
DEMO_SELF_HOSTED_SERIES: tuple[float, ...] = (
    520_000.0,
    480_000.0,
    410_000.0,
    340_000.0,
    290_000.0,
    180_000.0,
)


DEFAULT_TENANT_RAW_TOKEN_WEIGHTS: dict[str, float] = {
    "claims-triage": 1.85,
    "underwriting-copilot": 1.10,
    "policy-audit": 0.45,
}


def make_default_chargeback_tenants() -> dict[str, list[float] | float]:
    return {
        "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
        "underwriting-copilot": [302_250.0] * 8 + [60_450.0] * 16,
        "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
    }


def make_default_folder_pooling_projects() -> dict[str, list[float]]:
    return {
        "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
        "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
        "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
    }


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


_PUBLIC_SAFETY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("10-14 digit cloud project or order identifier", re.compile(r"\b\d{10,14}\b")),
    ("internal ticket reference", re.compile(r"\bb/\d{6,}\b")),
    (
        "internal shortlink",
        re.compile(r"\bgo/(?!mod\b|sum\b|work\b|version\b|test\b|build\b|run\b)[a-zA-Z0-9_-]{3,}"),
    ),
    ("CRM account or opportunity identifier", re.compile(r"\b(?:001|006|aBJ)[A-Za-z0-9]{12,15}\b")),
    ("internal release-channel suffix", re.compile(r"-(?:qcd|rc\d*)\b", re.IGNORECASE)),
    ("local workstation path", re.compile(r"/usr/" + r"local/|" + r"file:" + r"///")),
)


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
    audit: SelfHostedAuditRequest = Field(
        default_factory=lambda: SelfHostedAuditRequest(tpm_series=list(DEMO_SELF_HOSTED_SERIES))
    )
    disaggregated: DisaggregatedPoolRequest = Field(default_factory=DisaggregatedPoolRequest)


class ScenarioBurstAndCache(StrictModel):
    default_model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    provisioned_gsus: int = Field(default=10, ge=1, le=100_000)
    sixty_sec_tpm: BoundedTpmFloat = 750_000.0
    peak_1s_tps: BoundedTpmFloat = 25_000.0
    peak_10s_tps: BoundedTpmFloat | None = None
    peak_30s_tps: BoundedTpmFloat | None = None
    prefix_dilution: OptimizeRequest = Field(
        default_factory=lambda: OptimizeRequest(tpm_series=list(DEMO_OPTIMIZE_SERIES))
    )
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
    commitment: OptimizeRequest = Field(
        default_factory=lambda: OptimizeRequest(
            tpm_series=list(DEMO_OPTIMIZE_SERIES), fsp_discount_pct=15.0
        )
    )
    chargeback: ChargebackRequest = Field(
        default_factory=lambda: ChargebackRequest(
            hourly_consumed_tpm_by_tenant=make_default_chargeback_tenants(),
            tenant_raw_token_weights=dict(DEFAULT_TENANT_RAW_TOKEN_WEIGHTS),
            async_batch_waterfill_tpm=362_700.0,
        )
    )
    scavenger: BatchScavengerRequest = Field(default_factory=BatchScavengerRequest)
    residual_schedule: ResidualScheduleRequest = Field(
        default_factory=lambda: ResidualScheduleRequest(deadline_jobs=make_default_deadline_jobs())
    )
    folder_pooling: FolderPoolingRequest = Field(
        default_factory=lambda: FolderPoolingRequest(
            hourly_tpm_by_project=make_default_folder_pooling_projects()
        )
    )
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


def build_default_showcase_scenario() -> ShowcaseScenario:
    """Build the canonical default `ShowcaseScenario` used by `capacity demo`."""
    sparse, total_minutes = build_demo_throughput(days=7, by_region=True, mode="gsu")
    minute_series: dict[str, list[float]] = {}
    for skey in sorted(sparse):
        arr = [0.0] * total_minutes
        for ts, val in sparse[skey].items():
            idx = int((ts - DEFAULT_TRACE_EPOCH_UTC).total_seconds() // 60)
            if 0 <= idx < total_minutes:
                arr[idx] = float(val)
        minute_series[skey.format(by_region=True)] = arr
    return ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="demo-enterprise-ai",
            days=7,
            by_region=True,
            active_only=True,
            forecast_hours=24,
            minute_tpm_series=minute_series,
        )
    )


def evaluate_showcase_scenario(scenario: ShowcaseScenario | None = None) -> ShowcaseReport:
    """Evaluate a `ShowcaseScenario` (or the default demo) into a typed `ShowcaseReport`."""
    is_custom_scenario = scenario is not None
    eff = scenario if scenario is not None else build_default_showcase_scenario()
    gs = eff.gsu_sizing
    catalog: ModelCatalog = build_model_catalog(overrides=gs.gsu_capacity_overrides or None)

    sparse: dict[SeriesKey, dict[datetime, int]] = {}
    max_len = 0
    for raw_key, arr in gs.minute_tpm_series.items():
        max_len = max(max_len, len(arr))
        skey = SeriesKey.parse(raw_key, by_region=gs.by_region and "@" in raw_key)
        sparse[skey] = minute_list_to_sparse(arr)
    total_mins = max(gs.days * 24 * 60, max_len)

    gsu_recs = build_response(
        project=gs.project_label,
        days=gs.days,
        sparse_series=sparse,
        total_minutes=total_mins,
        percentiles=[50, 75, 90, 95],
        active_only=gs.active_only,
        by_region=gs.by_region,
        forecast_steps=gs.forecast_hours,
        mode="gsu",
        catalog=catalog,
    )

    sh_audit = (
        evaluate_self_hosted_endpoint(eff.self_hosted.audit) if eff.self_hosted.enabled else None
    )
    disagg = (
        size_disaggregated_prefill_decode(eff.self_hosted.disaggregated)
        if eff.self_hosted.enabled
        else None
    )

    bc = eff.burst_and_cache
    b_rec = catalog.require(bc.default_model)
    b_spec = b_rec.family_spec
    burst = evaluate_micro_burst_window(
        MicroBurstRequest(
            model=bc.default_model,
            provisioned_gsus=bc.provisioned_gsus,
            sixty_sec_tpm=bc.sixty_sec_tpm,
            peak_1s_tps=bc.peak_1s_tps,
            peak_10s_tps=bc.peak_10s_tps,
            peak_30s_tps=bc.peak_30s_tps,
        ),
        catalog=catalog,
    )
    pd_req = bc.prefix_dilution
    prefix_rr = evaluate_prefix_cache_dilution(
        total_input_tokens=pd_req.total_input_tokens,
        shared_prefix_ratio=pd_req.shared_prefix_ratio,
        baseline_cache_hit_rate=pd_req.baseline_cache_hit_rate,
        num_round_robin_endpoints=pd_req.num_round_robin_endpoints,
        tps_per_gsu=b_rec.gsu_capacity_tpm / 60.0,
        cached_token_weight=b_spec.standard_cached_read_weight,
        uncached_token_weight=b_spec.standard_input_weight,
        requests_per_second=pd_req.requests_per_second or 1.0,
    )
    prefix_flash = evaluate_model_tier_prefix_cache(bc.flash_tier_audit, catalog=catalog)
    prefix_argon = evaluate_model_tier_prefix_cache(bc.pro_tier_audit, catalog=catalog)
    gateway = generate_gateway_bundle(eff.gateway.config, catalog=catalog)

    fo = eff.finops
    commitment = optimize_hybrid_commitment(fo.commitment, catalog=catalog)
    chargeback = compute_focus_chargeback(fo.chargeback, catalog=catalog)
    scavenger = evaluate_batch_scavenger_economics(fo.scavenger, catalog=catalog)
    schedule = schedule_residual_capacity(fo.residual_schedule, catalog=catalog)
    folder_pooling = evaluate_folder_pooling(fo.folder_pooling, catalog=catalog)
    kpis = compute_finops_kpis(fo.kpis)

    return ShowcaseReport(
        version=PACKAGE_VERSION,
        scenario_mode=is_custom_scenario,
        scenario=eff,
        gsu_recommendations=gsu_recs,
        self_hosted_audit=sh_audit,
        disaggregated_sizing=disagg,
        micro_burst=burst,
        prefix_cache_rr=prefix_rr,
        prefix_cache_flash=prefix_flash,
        prefix_cache_argon=prefix_argon,
        gateway=gateway,
        commitment=commitment,
        chargeback=chargeback,
        scavenger=scavenger,
        residual_schedule=schedule,
        folder_pooling=folder_pooling,
        finops_kpis=kpis,
    )


def _walk_strings(node: object, path: str = "scenario") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((path, node))
    elif isinstance(node, Mapping):
        for k, v in node.items():
            key_str = str(k)
            found.append((f"{path}.<key>", key_str))
            found.extend(_walk_strings(v, f"{path}.{key_str}"))
    elif isinstance(node, (list, tuple)):
        for idx, item in enumerate(node):
            found.extend(_walk_strings(item, f"{path}[{idx}]"))
    return found


def audit_scenario_public_safety(scenario: ShowcaseScenario) -> list[str]:
    """Check all string keys and string values for unredacted cloud/internal identifiers."""
    violations: list[str] = []
    payload = scenario.model_dump()
    for field_path, text in _walk_strings(payload):
        for label, pattern in _PUBLIC_SAFETY_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                violations.append(f"{field_path}: {label} ({match.group(0)!r})")
    return violations


def _load_structured_file(path: Path | str) -> object:
    raw = Path(path).read_text(encoding="utf-8")
    stripped = raw.lstrip()
    if stripped.startswith(("{", "[")):
        return json.loads(raw)
    return yaml.safe_load(raw)


def load_showcase_scenario(
    path: Path | str, *, strict_public_check: bool = False
) -> ShowcaseScenario:
    """Load and validate a JSON or YAML `ShowcaseScenario` file."""
    loaded = _load_structured_file(path)
    if not isinstance(loaded, Mapping):
        raise ValueError("Scenario file must contain a top-level JSON/YAML mapping")
    scenario = ShowcaseScenario.model_validate(dict(loaded))
    if strict_public_check:
        issues = audit_scenario_public_safety(scenario)
        if issues:
            raise ValueError(
                "Scenario failed --strict-public-check redaction gate:\n  " + "\n  ".join(issues)
            )
    return scenario


def load_orders_overlay(path: Path | str) -> dict[str, ModelInventoryAnnotation]:
    """Load an optional customer-maintained commercial PT orders YAML/JSON overlay."""
    loaded = _load_structured_file(path)
    if not isinstance(loaded, Mapping):
        raise ValueError("Orders overlay must be a JSON/YAML mapping of model[@region] -> metadata")
    raw_orders = loaded.get("orders", loaded)
    if not isinstance(raw_orders, Mapping):
        raise ValueError("Orders overlay 'orders' entry must be a mapping")
    return {
        str(k): ModelInventoryAnnotation.model_validate(v)
        for k, v in raw_orders.items()
        if isinstance(v, Mapping)
    }


type InventoryMap = Mapping[SeriesKey, Mapping[str, int]] | Mapping[str, Mapping[str, int]]


def _normalize_sparse_map(
    sparse_series: SparseSeriesMap | None,
    *,
    by_region: bool,
) -> dict[str, dict[datetime, int]]:
    if not sparse_series:
        return {}
    out: dict[str, dict[datetime, int]] = {}
    for raw_k, pts in sparse_series.items():
        if isinstance(raw_k, SeriesKey):
            k_str = raw_k.format(by_region=by_region)
        else:
            k_str = SeriesKey.parse(raw_k, by_region=by_region and "@" in raw_k).format(
                by_region=by_region
            )
        bucket = out.setdefault(k_str, {})
        for ts, val in pts.items():
            bucket[ts] = bucket.get(ts, 0) + int(val)
    return out


def _normalize_inventory_map(
    inventory_telemetry: InventoryMap | None,
    *,
    by_region: bool,
) -> dict[str, Mapping[str, int]]:
    if not inventory_telemetry:
        return {}
    out: dict[str, Mapping[str, int]] = {}
    for raw_k, inv in inventory_telemetry.items():
        k_str = (
            raw_k.format(by_region=by_region)
            if isinstance(raw_k, SeriesKey)
            else SeriesKey.parse(raw_k, by_region=by_region and "@" in raw_k).format(
                by_region=by_region
            )
        )
        out[k_str] = inv
    return out


def compile_scenario_from_telemetry(
    *,
    project_label: str,
    days: int = 7,
    by_region: bool = True,
    sparse_series: SparseSeriesMap | None = None,
    total_minutes: int | None = None,
    inventory_telemetry: InventoryMap | None = None,
    log_snapshot: VllmTelemetrySnapshot | None = None,
    orders_overlay: Mapping[str, ModelInventoryAnnotation] | None = None,
    gateway_mode: GatewayPresentationMode = "oss_stack",
    pooling_mode: FinOpsPoolingMode = "folder_rhpt",
    platform_brand: PlatformBrand = "Gemini Platform",
    self_hosted_enabled: bool = True,
) -> ShowcaseScenario:
    """Compile customer Cloud Monitoring series, inference logs, and order metadata."""
    catalog = build_model_catalog()
    minute_series: dict[str, list[float]] = {}
    eff_minutes = total_minutes or (days * 24 * 60)

    norm_sparse = _normalize_sparse_map(sparse_series, by_region=by_region)
    norm_inventory = _normalize_inventory_map(inventory_telemetry, by_region=by_region)

    if norm_sparse:
        all_ts = sorted({ts for m in norm_sparse.values() for ts in m})
        if all_ts:
            first_ts = all_ts[0].astimezone(UTC).replace(second=0, microsecond=0)
            span = max(eff_minutes, int((all_ts[-1] - first_ts).total_seconds() // 60) + 1)
            span = min(span, MAX_MINUTE_POINTS)
            for raw_key, ts_map in sorted(norm_sparse.items()):
                arr = [0.0] * span
                for ts, val in ts_map.items():
                    idx = int((ts.astimezone(UTC) - first_ts).total_seconds() // 60)
                    if 0 <= idx < span:
                        arr[idx] += float(max(0, val))
                minute_series[raw_key] = arr

    if log_snapshot is not None and log_snapshot.minute_tpm_by_model_region:
        for mr_key, vals in sorted(log_snapshot.minute_tpm_by_model_region.items()):
            key = mr_key if by_region else mr_key.split("@", 1)[0]
            if key not in minute_series:
                minute_series[key] = [max(0.0, float(v)) for v in vals[:MAX_MINUTE_POINTS]]

    if not minute_series and log_snapshot is not None:
        key = (
            f"{normalize_model_id(log_snapshot.model)}@global" if by_region else log_snapshot.model
        )
        minute_series[key] = [
            max(0.0, float(v)) for v in log_snapshot.tpm_series[:MAX_MINUTE_POINTS]
        ]

    if not minute_series:
        raise ValueError("No throughput series found in Cloud Monitoring or --logs-file")

    series_len = max((len(v) for v in minute_series.values()), default=min(eff_minutes, 1_440))
    annotations: dict[str, ModelInventoryAnnotation] = {}
    all_keys = sorted(set(minute_series) | set(norm_inventory) | set(orders_overlay or {}))
    for key in all_keys:
        if key not in minute_series:
            minute_series[key] = [0.0] * series_len
        model_part = key.split("@", 1)[0]
        # Fail closed on unknown models instead of guessing capacities or lifecycles.
        catalog.require(model_part)
        inv = norm_inventory.get(key, {})
        ov = (orders_overlay or {}).get(key) or (orders_overlay or {}).get(model_part)
        log_spill = (
            log_snapshot.spillover_by_model_region.get(key) if log_snapshot is not None else None
        )
        log_429 = (
            log_snapshot.http_429_by_model_region.get(key) if log_snapshot is not None else None
        )
        contracted = (
            ov.contracted_gsus
            if ov and ov.contracted_gsus is not None
            else inv.get("contracted_gsus")
        )
        spillover = (
            ov.spillover_requests
            if ov and ov.spillover_requests is not None
            else (log_spill if log_spill is not None else inv.get("spillover_requests"))
        )
        err_429 = (
            ov.http_429_count
            if ov and ov.http_429_count is not None
            else (log_429 if log_429 is not None else inv.get("http_429_count"))
        )
        track = (
            ov.lifecycle_track
            if ov and ov.lifecycle_track is not None
            else infer_lifecycle_track(model_part, catalog=catalog)
        )
        annotations[key] = ModelInventoryAnnotation(
            contracted_gsus=contracted,
            scope_label=ov.scope_label if ov else None,
            spillover_requests=spillover,
            http_429_count=err_429,
            lifecycle_track=track,
            renewal_mode=ov.renewal_mode if ov else ("auto_renew" if contracted else "none"),
            expiry_date=ov.expiry_date if ov else None,
            expiring_fixed_gsus=ov.expiring_fixed_gsus if ov else None,
        )

    primary_key = max(minute_series, key=lambda k: sum(minute_series[k]))
    primary_model = normalize_model_id(primary_key.split("@", 1)[0])
    cap_tpm = catalog.require_capacity(primary_model)
    primary_series = minute_series[primary_key]
    peak_tpm = max(primary_series, default=float(cap_tpm))
    primary_contracted = annotations[primary_key].contracted_gsus
    prov_gsus = max(
        1,
        primary_contracted
        if primary_contracted is not None and primary_contracted > 0
        else math.ceil(peak_tpm / cap_tpm),
    )

    peak_1s = (
        log_snapshot.peak_1s_burndown_tps
        if log_snapshot is not None and log_snapshot.peak_1s_burndown_tps > 0.0
        else round((peak_tpm / 60.0) * ASSUMED_PEAK_TO_MEAN_RATIO, 2)
    )
    peak_10s = (
        log_snapshot.peak_10s_burndown_tps
        if log_snapshot is not None and log_snapshot.peak_10s_burndown_tps > 0.0
        else None
    )
    peak_30s = (
        log_snapshot.peak_30s_burndown_tps
        if log_snapshot is not None and log_snapshot.peak_30s_burndown_tps > 0.0
        else None
    )

    has_log_tenants = bool(log_snapshot is not None and log_snapshot.hourly_consumed_tpm_by_tenant)
    tenants: dict[str, list[float] | float] = (
        {k: list(v) for k, v in log_snapshot.hourly_consumed_tpm_by_tenant.items()}
        if log_snapshot is not None and has_log_tenants
        else make_default_chargeback_tenants()
    )
    tenant_weights = None if has_log_tenants else dict(DEFAULT_TENANT_RAW_TOKEN_WEIGHTS)
    spill_tier: Literal["priority", "standard", "flex"] = (
        "standard" if is_partner_claude(primary_model, catalog=catalog) else "priority"
    )

    return ShowcaseScenario(
        presentation=ScenarioPresentation(
            title=f"{platform_brand} Capacity, Gateway & FOCUS 1.2 Scorecard",
            subtitle=(
                f"{project_label} • {days}d Empirical Telemetry • "
                f"Sub-Second Burst & Prefix Cache • FOCUS 1.2 Two-Pool Chargeback"
            ),
            platform_brand=platform_brand,
        ),
        gsu_sizing=ScenarioGsuSizing(
            project_label=project_label,
            days=days,
            by_region=by_region,
            active_only=True,
            minute_tpm_series=minute_series,
            inventory_annotations=annotations,
        ),
        self_hosted=ScenarioSelfHosted(enabled=self_hosted_enabled),
        burst_and_cache=ScenarioBurstAndCache(
            default_model=primary_model,
            provisioned_gsus=prov_gsus,
            sixty_sec_tpm=round(peak_tpm, 2),
            peak_1s_tps=peak_1s,
            peak_10s_tps=peak_10s,
            peak_30s_tps=peak_30s,
        ),
        gateway=ScenarioGateway(
            presentation_mode=gateway_mode,
            config=GatewayConfigRequest(model=primary_model, provisioned_gsus=prov_gsus),
        ),
        finops=ScenarioFinOps(
            pooling_mode=pooling_mode,
            commitment=OptimizeRequest(
                model=primary_model,
                tpm_series=primary_series[:MAX_MINUTE_POINTS],
                spillover_tier=spill_tier,
            ),
            chargeback=ChargebackRequest(
                model=primary_model,
                provisioned_gsus=prov_gsus,
                hourly_consumed_tpm_by_tenant=tenants,
                tenant_raw_token_weights=tenant_weights,
            ),
            scavenger=BatchScavengerRequest(model=primary_model, provisioned_gsus=prov_gsus),
            residual_schedule=ResidualScheduleRequest(
                model=primary_model,
                provisioned_gsus=prov_gsus,
                deadline_jobs=make_default_deadline_jobs(),
            ),
        ),
    )
