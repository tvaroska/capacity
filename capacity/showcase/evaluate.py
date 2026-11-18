"""Scenario evaluation, public-safety redaction check, and telemetry-to-scenario compiler."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from capacity.contract.common import PACKAGE_VERSION, SeriesKey
from capacity.contract.requests import (
    BatchScavengerRequest,
    ChargebackRequest,
    GatewayConfigRequest,
    MicroBurstRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
)
from capacity.contract.results import VllmTelemetrySnapshot
from capacity.contract.scenario import (
    MAX_MINUTE_POINTS,
    FinOpsPoolingMode,
    GatewayPresentationMode,
    ModelInventoryAnnotation,
    PlatformBrand,
    ScenarioBurstAndCache,
    ScenarioFinOps,
    ScenarioGateway,
    ScenarioGsuSizing,
    ScenarioPresentation,
    ScenarioSelfHosted,
    ShowcaseReport,
    ShowcaseScenario,
)
from capacity.engine.burst import (
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.engine.catalog import (
    ModelCatalog,
    build_model_catalog,
    infer_lifecycle_track,
    is_partner_claude,
    normalize_model_id,
)
from capacity.engine.chargeback import compute_focus_chargeback
from capacity.engine.commitment import optimize_hybrid_commitment
from capacity.engine.kpis import compute_finops_kpis
from capacity.engine.pooling import evaluate_folder_pooling
from capacity.engine.scheduler import (
    evaluate_batch_scavenger_economics,
    schedule_residual_capacity,
)
from capacity.engine.self_hosted import (
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)
from capacity.engine.sizing import ASSUMED_PEAK_TO_MEAN_RATIO, build_response
from capacity.engine.stats import SparseSeriesMap, minute_list_to_sparse
from capacity.gateway.bundle import generate_gateway_bundle
from capacity.showcase.sample_data import (
    DEFAULT_TENANT_RAW_TOKEN_WEIGHTS,
    build_default_showcase_scenario,
    make_default_chargeback_tenants,
    make_default_deadline_jobs,
    resolve_scenario_with_defaults,
)

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

type InventoryMap = Mapping[SeriesKey, Mapping[str, int]] | Mapping[str, Mapping[str, int]]


def evaluate_showcase_scenario(scenario: ShowcaseScenario | None = None) -> ShowcaseReport:
    """Evaluate a `ShowcaseScenario` (or the default demo) into a typed `ShowcaseReport`."""
    is_custom_scenario = scenario is not None
    eff = (
        resolve_scenario_with_defaults(scenario)
        if scenario is not None
        else build_default_showcase_scenario()
    )
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

    sh_req = eff.self_hosted.audit
    pd_req = eff.burst_and_cache.prefix_dilution
    fo = eff.finops
    if (
        sh_req is None
        or pd_req is None
        or fo.commitment is None
        or fo.chargeback is None
        or fo.residual_schedule is None
        or fo.folder_pooling is None
    ):
        raise RuntimeError("resolve_scenario_with_defaults left optional sub-blocks unset")

    sh_audit = evaluate_self_hosted_endpoint(sh_req) if eff.self_hosted.enabled else None
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
        simulator_options=catalog.simulator_options(),
        selected_simulator_model=normalize_model_id(bc.default_model),
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
    try:
        if stripped.startswith(("{", "[")):
            return json.loads(raw)
        return yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Unreadable JSON/YAML in {path}: {exc}") from exc


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
    return resolve_scenario_with_defaults(scenario)


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
) -> dict[str, dict[str, int]]:
    if not inventory_telemetry:
        return {}
    out: dict[str, dict[str, int]] = {}
    for raw_k, inv in inventory_telemetry.items():
        k_str = (
            raw_k.format(by_region=by_region)
            if isinstance(raw_k, SeriesKey)
            else SeriesKey.parse(raw_k, by_region=by_region and "@" in raw_k).format(
                by_region=by_region
            )
        )
        bucket = out.setdefault(k_str, {})
        for metric_name, metric_val in inv.items():
            bucket[metric_name] = bucket.get(metric_name, 0) + int(metric_val)
    return out


def _fold_model_region_counts(
    counts: Mapping[str, int],
    *,
    by_region: bool,
) -> dict[str, int]:
    folded: dict[str, int] = {}
    for mr_key, val in counts.items():
        key = mr_key if by_region else mr_key.split("@", 1)[0]
        folded[key] = folded.get(key, 0) + int(val)
    return folded


def _accumulate_log_minute_series(
    minute_series: dict[str, list[float]],
    monitoring_keys: set[str],
    log_series: Mapping[str, Sequence[float]],
    *,
    by_region: bool,
) -> None:
    for mr_key, vals in sorted(log_series.items()):
        key = mr_key if by_region else mr_key.split("@", 1)[0]
        if key in monitoring_keys:
            continue
        clipped = [max(0.0, float(v)) for v in vals[:MAX_MINUTE_POINTS]]
        existing = minute_series.get(key)
        if existing is None:
            minute_series[key] = clipped
            continue
        target_len = max(len(existing), len(clipped))
        if len(existing) < target_len:
            existing.extend([0.0] * (target_len - len(existing)))
        for idx, pt in enumerate(clipped):
            existing[idx] = round(existing[idx] + pt, 2)


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

    monitoring_keys = set(minute_series)
    if log_snapshot is not None and log_snapshot.minute_tpm_by_model_region:
        _accumulate_log_minute_series(
            minute_series,
            monitoring_keys,
            log_snapshot.minute_tpm_by_model_region,
            by_region=by_region,
        )

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
    log_spill_by_key = (
        _fold_model_region_counts(log_snapshot.spillover_by_model_region, by_region=by_region)
        if log_snapshot is not None
        else {}
    )
    log_429_by_key = (
        _fold_model_region_counts(log_snapshot.http_429_by_model_region, by_region=by_region)
        if log_snapshot is not None
        else {}
    )
    all_keys = sorted(set(minute_series) | set(norm_inventory) | set(orders_overlay or {}))
    for key in all_keys:
        if key not in minute_series:
            minute_series[key] = [0.0] * series_len
        model_part = key.split("@", 1)[0]
        # Fail closed on unknown models instead of guessing capacities or lifecycles.
        catalog.require(model_part)
        inv = norm_inventory.get(key, {})
        ov = (orders_overlay or {}).get(key) or (orders_overlay or {}).get(model_part)
        log_spill = log_spill_by_key.get(key)
        log_429 = log_429_by_key.get(key)
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

    raw_scenario = ShowcaseScenario(
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
    return resolve_scenario_with_defaults(raw_scenario)
