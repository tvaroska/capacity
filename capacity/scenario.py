"""Typed multi-profile scenario contract (`--scenario-file`) and telemetry-to-scenario compiler."""

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

from capacity.capacity import find_capacity
from capacity.model_families import is_partner_claude, normalize_model_id
from capacity.models import (
    MAX_SERIES_CELLS,
    MAX_SERIES_KEYS,
    MODEL_ID_PATTERN,
    BatchScavengerRequest,
    BoundedTpmFloat,
    ChargebackRequest,
    DisaggregatedPoolRequest,
    FinOpsKpiRequest,
    FolderPoolingRequest,
    GatewayConfigRequest,
    ModelTierPrefixCacheRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
    StrictModel,
)
from capacity.telemetry_parsers import VllmTelemetrySnapshot

PlatformBrand = Literal["Gemini Platform", "Vertex AI"]
LifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]
RenewalMode = Literal["fixed_term", "auto_renew", "mixed", "none"]
GatewayPresentationMode = Literal["oss_stack", "central_proxy"]
FinOpsPoolingMode = Literal["folder_rhpt", "1p_vs_3p_hub", "silo_stranding_audit"]

MAX_MINUTE_POINTS = 60_480  # up to 42 days of 1-minute buckets per series
MinuteTpmSeries = Annotated[
    list[BoundedTpmFloat], Field(min_length=1, max_length=MAX_MINUTE_POINTS)
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


def _default_chargeback_tenants() -> dict[str, list[float] | float]:
    return {
        "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
        "underwriting-copilot": [302_250.0] * 8 + [60_450.0] * 16,
        "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
    }


def infer_lifecycle_track(model_id: str) -> LifecycleTrack:
    """Classify a normalized model ID into its enterprise GA lifecycle track."""
    norm = normalize_model_id(model_id)
    if "preview" in model_id.lower() or "exp" in model_id.lower():
        return "preview"
    if "carbon" in norm:
        return "short_term_ga"
    if norm.startswith(("gemini-2.0-", "gemini-2.5-", "claude-3-")):
        return "legacy_sunset"
    return "long_term_ga"


class ScenarioPresentation(StrictModel):
    title: str = Field(
        default="Capacity Showcase Workbench (v0.3.0)",
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
    note: str | None = Field(default=None, max_length=240)


class ScenarioGsuSizing(StrictModel):
    project_label: str = Field(default="enterprise-ai-hub", min_length=1, max_length=80)
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
    audit: SelfHostedAuditRequest = Field(default_factory=SelfHostedAuditRequest)
    disaggregated: DisaggregatedPoolRequest = Field(default_factory=DisaggregatedPoolRequest)


class ScenarioBurstAndCache(StrictModel):
    default_model: str = Field(default="gemini-3.8-flash", pattern=MODEL_ID_PATTERN)
    provisioned_gsus: int = Field(default=10, ge=1, le=100_000)
    sixty_sec_tpm: BoundedTpmFloat = 750_000.0
    peak_1s_tps: BoundedTpmFloat = 25_000.0
    peak_10s_tps: BoundedTpmFloat | None = None
    peak_30s_tps: BoundedTpmFloat | None = None
    prefix_dilution: OptimizeRequest = Field(default_factory=OptimizeRequest)
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
        default_factory=lambda: OptimizeRequest(fsp_discount_pct=15.0)
    )
    chargeback: ChargebackRequest = Field(
        default_factory=lambda: ChargebackRequest(
            hourly_consumed_tpm_by_tenant=_default_chargeback_tenants(),
            async_batch_waterfill_tpm=362_700.0,
        )
    )
    scavenger: BatchScavengerRequest = Field(default_factory=BatchScavengerRequest)
    residual_schedule: ResidualScheduleRequest = Field(default_factory=ResidualScheduleRequest)
    folder_pooling: FolderPoolingRequest = Field(default_factory=FolderPoolingRequest)
    kpis: FinOpsKpiRequest = Field(default_factory=FinOpsKpiRequest)


class ShowcaseScenario(StrictModel):
    schema_version: Literal["capacity.scenario.v1"] = "capacity.scenario.v1"
    presentation: ScenarioPresentation = Field(default_factory=ScenarioPresentation)
    gsu_sizing: ScenarioGsuSizing
    self_hosted: ScenarioSelfHosted = Field(default_factory=ScenarioSelfHosted)
    burst_and_cache: ScenarioBurstAndCache = Field(default_factory=ScenarioBurstAndCache)
    gateway: ScenarioGateway = Field(default_factory=ScenarioGateway)
    finops: ScenarioFinOps = Field(default_factory=ScenarioFinOps)


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
    if stripped.startswith("{") or stripped.startswith("["):
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


def compile_scenario_from_telemetry(
    *,
    project_label: str,
    days: int = 7,
    by_region: bool = True,
    sparse_series: Mapping[str, Mapping[datetime, int]] | None = None,
    total_minutes: int | None = None,
    inventory_telemetry: Mapping[str, Mapping[str, int]] | None = None,
    log_snapshot: VllmTelemetrySnapshot | None = None,
    orders_overlay: Mapping[str, ModelInventoryAnnotation] | None = None,
    gateway_mode: GatewayPresentationMode = "oss_stack",
    pooling_mode: FinOpsPoolingMode = "folder_rhpt",
    platform_brand: PlatformBrand = "Gemini Platform",
    self_hosted_enabled: bool = True,
) -> ShowcaseScenario:
    """Compile customer Cloud Monitoring series, inference logs, and order metadata."""
    minute_series: dict[str, list[float]] = {}
    eff_minutes = total_minutes or (days * 24 * 60)

    if sparse_series:
        all_ts = sorted({ts for m in sparse_series.values() for ts in m})
        if all_ts:
            first_ts = all_ts[0].astimezone(UTC).replace(second=0, microsecond=0)
            span = max(eff_minutes, int((all_ts[-1] - first_ts).total_seconds() // 60) + 1)
            span = min(span, MAX_MINUTE_POINTS)
            for raw_key, ts_map in sorted(sparse_series.items()):
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
    all_keys = sorted(
        set(minute_series) | set(inventory_telemetry or {}) | set(orders_overlay or {})
    )
    for key in all_keys:
        if key not in minute_series:
            minute_series[key] = [0.0] * series_len
        model_part = key.split("@", 1)[0]
        inv = (inventory_telemetry or {}).get(key, {})
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
            else infer_lifecycle_track(model_part)
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
            note=ov.note if ov else None,
        )

    primary_key = max(minute_series, key=lambda k: sum(minute_series[k]))
    primary_model = normalize_model_id(primary_key.split("@", 1)[0])
    if find_capacity(primary_model) is None:
        primary_model = "gemini-3.8-flash"
    primary_series = minute_series[primary_key]
    peak_tpm = max(primary_series, default=120_900.0)
    cap_tpm = find_capacity(primary_model) or 120_900
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
        else round((peak_tpm / 60.0) * 1.9, 2)
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

    tenants: dict[str, list[float] | float] = (
        {k: list(v) for k, v in log_snapshot.hourly_consumed_tpm_by_tenant.items()}
        if log_snapshot is not None and log_snapshot.hourly_consumed_tpm_by_tenant
        else _default_chargeback_tenants()
    )
    spill_tier: Literal["priority", "standard", "flex"] = (
        "standard" if is_partner_claude(primary_model) else "priority"
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
                tpm_series=primary_series[:50_000],
                spillover_tier=spill_tier,
            ),
            chargeback=ChargebackRequest(
                model=primary_model,
                provisioned_gsus=prov_gsus,
                hourly_consumed_tpm_by_tenant=tenants,
            ),
            scavenger=BatchScavengerRequest(model=primary_model, provisioned_gsus=prov_gsus),
            residual_schedule=ResidualScheduleRequest(
                model=primary_model, provisioned_gsus=prov_gsus
            ),
        ),
    )
