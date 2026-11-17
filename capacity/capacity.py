"""Empirical percentile & Holt forecast sizing for Managed GSUs and Self-Hosted Replicas."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_prefix_cache_dilution,
)
from capacity.finops import optimize_hybrid_commitment
from capacity.forecasting import (
    DEFAULT_PERCENTILES,
    SparseSeriesMap,
    TelemetryPort,
    holts_linear_trend,
    percentile,
)
from capacity.model_families import (
    DEFAULT_GSU_CONFIG,
    ModelCatalog,
    build_model_catalog,
    normalize_model_id,
)
from capacity.models import (
    CapacityModeLiteral,
    MicroBurstRequest,
    ModelRecommendation,
    OptimizeRequest,
    OptimizeResponse,
    PercentileDetail,
    RecommendationResponse,
    SeriesKey,
)
from capacity.self_hosted import DEFAULT_SELF_HOSTED_PROFILES

# ADR-002: without sub-second gateway logs, the peak is assumed at this multiple of the mean.
ASSUMED_PEAK_TO_MEAN_RATIO = 1.9


def load_gsu_config(
    mode: str = "gsu",
    overrides: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Return capacity table (TPM per 1 GSU or 1 GPU replica) from the unified model catalog."""
    if mode == "self-hosted":
        base = {k: prof.tpm_per_replica for k, prof in DEFAULT_SELF_HOSTED_PROFILES.items()}
        cat = build_model_catalog(overrides=overrides)
        extra = {
            k: rec.gsu_capacity_tpm for k, rec in cat.records.items() if k not in DEFAULT_GSU_CONFIG
        }
        return {**base, **extra}
    cat = build_model_catalog(overrides=overrides)
    return {k: rec.gsu_capacity_tpm for k, rec in cat.records.items()}


def find_capacity(
    model_id: str,
    gsu_config: Mapping[str, int] | None = None,
    *,
    catalog: ModelCatalog | None = None,
) -> int | None:
    """Return TPM per GSU (or replica) for a listed model, or None; never a neighbour's rate."""
    if gsu_config is not None:
        raw = model_id.lower().split("@", 1)[0]
        if "/" in raw:
            raw = raw.rsplit("/", 1)[-1]
        for candidate in (raw, normalize_model_id(raw)):
            if candidate in gsu_config:
                return gsu_config[candidate]
        return None
    cat = catalog if catalog is not None else build_model_catalog()
    return cat.capacity_for(model_id)


def require_capacity(
    model_id: str,
    gsu_config: Mapping[str, int] | None = None,
    *,
    catalog: ModelCatalog | None = None,
) -> int:
    """Return TPM per GSU for a model, raising `UnknownModelError` instead of guessing."""
    if gsu_config is not None:
        return build_model_catalog(overrides=gsu_config).require_capacity(model_id)
    cat = catalog if catalog is not None else build_model_catalog()
    return cat.require_capacity(model_id)


def _build_percentile_map(
    sorted_vals: Sequence[int | float],
    percentiles: Sequence[int],
    cap: int | None,
) -> dict[str, PercentileDetail]:
    pct_map: dict[str, PercentileDetail] = {}
    for p in percentiles:
        tpm = percentile(sorted_vals, p)
        if cap and cap > 0 and tpm > 0:
            gsus = max(1, math.ceil(tpm / cap))
        else:
            gsus = 0
        pct_map[f"P{p}"] = PercentileDetail(tpm=tpm, recommended_gsus=gsus)
    return pct_map


def compute_recommendations(
    sparse_series: SparseSeriesMap,
    total_minutes: int,
    percentiles: Sequence[int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: CapacityModeLiteral = "gsu",
    gsu_overrides: Mapping[str, int] | None = None,
    *,
    catalog: ModelCatalog | None = None,
) -> list[ModelRecommendation]:
    """Compute both 24x7 (`all_hours`) and business-hours (`active_hours`) recommendations."""
    pcts = list(percentiles) if percentiles is not None else list(DEFAULT_PERCENTILES)
    if mode == "self-hosted":
        gsu_config = load_gsu_config(mode="self-hosted", overrides=gsu_overrides)
    elif catalog is not None and gsu_overrides is None:
        gsu_config = {k: rec.gsu_capacity_tpm for k, rec in catalog.records.items()}
    else:
        gsu_config = load_gsu_config(mode="gsu", overrides=gsu_overrides)

    keyed: dict[SeriesKey, dict[datetime, int]] = {}
    for raw_key, minute_map in sparse_series.items():
        if isinstance(raw_key, SeriesKey):
            skey = SeriesKey(
                model=raw_key.model,
                region=raw_key.region if by_region else None,
                folder=raw_key.folder,
            )
        else:
            parsed = SeriesKey.parse(raw_key, by_region=by_region and "@" in raw_key)
            skey = SeriesKey(
                model=parsed.model,
                region=parsed.region if (by_region and "@" in raw_key) else None,
                folder=parsed.folder,
            )
        bucket = keyed.setdefault(skey, {})
        for ts, val in minute_map.items():
            bucket[ts] = bucket.get(ts, 0) + int(val)

    results: list[ModelRecommendation] = []
    for skey in sorted(keyed):
        minute_map = keyed[skey]
        cap = find_capacity(skey.model, gsu_config)
        active_vals = sorted(v for v in minute_map.values() if v > 0)
        active_count = len(active_vals)
        eff_total_minutes = max(total_minutes, active_count)
        num_zeros = max(0, eff_total_minutes - active_count)
        all_vals = [0] * num_zeros + active_vals

        all_hours_map = _build_percentile_map(all_vals, pcts, cap)
        active_hours_map = _build_percentile_map(active_vals, pcts, cap)
        primary_map = active_hours_map if active_only else all_hours_map
        duty_cycle = (
            min(100.0, round((active_count / eff_total_minutes) * 100.0, 2))
            if eff_total_minutes > 0
            else 0.0
        )

        daily_peaks: dict[date, int] = {}
        for ts, v in minute_map.items():
            if v > 0:
                day = ts.date()
                daily_peaks[day] = max(daily_peaks.get(day, 0), v)
        peak_series = [daily_peaks[d] for d in sorted(daily_peaks)]
        forecasts = (
            holts_linear_trend(peak_series, steps=max(1, math.ceil(forecast_steps / 24)))
            if peak_series and forecast_steps > 0
            else [0.0]
        )
        peak_forecast_tpm = round(max(forecasts)) if forecasts else 0
        forecast_gsus = (
            max(1, math.ceil(peak_forecast_tpm / cap))
            if cap and cap > 0 and peak_forecast_tpm > 0
            else 0
        )

        results.append(
            ModelRecommendation(
                model=skey.model,
                normalized_model=normalize_model_id(skey.model),
                region=skey.region,
                folder=skey.folder,
                gsu_capacity_tpm=cap,
                active_minutes=active_count,
                total_minutes=eff_total_minutes,
                duty_cycle_pct=duty_cycle,
                forecast_24h_peak_tpm=peak_forecast_tpm,
                forecast_24h_recommended_gsus=forecast_gsus,
                percentiles=primary_map,
                all_hours=all_hours_map,
                active_hours=active_hours_map,
            )
        )

    return results


def build_response(
    project: str,
    days: int,
    sparse_series: SparseSeriesMap,
    total_minutes: int,
    percentiles: Sequence[int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: CapacityModeLiteral = "gsu",
    gsu_overrides: Mapping[str, int] | None = None,
    *,
    catalog: ModelCatalog | None = None,
) -> RecommendationResponse:
    pcts = list(percentiles) if percentiles is not None else list(DEFAULT_PERCENTILES)
    recs = compute_recommendations(
        sparse_series,
        total_minutes,
        pcts,
        active_only=active_only,
        by_region=by_region,
        forecast_steps=forecast_steps,
        mode=mode,
        gsu_overrides=gsu_overrides,
        catalog=catalog,
    )
    return RecommendationResponse(
        project=project,
        days=days,
        active_only=active_only,
        by_region=by_region,
        mode=mode,
        forecast_hours=forecast_steps,
        requested_percentiles=pcts,
        models=recs,
    )


async def recommend_capacity(
    source: TelemetryPort,
    *,
    project: str | Sequence[str],
    days: int,
    percentiles: Sequence[int] | None = None,
    models: Sequence[str] | None = None,
    regions: Sequence[str] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    folder_map: Mapping[str, str] | None = None,
    forecast_steps: int = 24,
    mode: CapacityModeLiteral = "gsu",
) -> RecommendationResponse:
    """Read throughput through a telemetry port and size it: the one path CLI and API share."""
    sparse, total_minutes = await source.fetch_series(
        project, days, models, regions, by_region=by_region, folder_map=folder_map, mode=mode
    )
    label = project if isinstance(project, str) else ",".join(project)
    return build_response(
        label,
        days,
        sparse,
        total_minutes,
        percentiles,
        active_only=active_only,
        by_region=by_region,
        forecast_steps=forecast_steps,
        mode=mode,
    )


def evaluate_gsu_optimization(
    req: OptimizeRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> OptimizeResponse:
    """Shared GSU Newsvendor + sub-second micro-burst + prefix-cache orchestrator (CLI & API)."""
    cat = catalog if catalog is not None else build_model_catalog()
    record = cat.require(req.model)
    norm_model = normalize_model_id(req.model)
    spec = record.family_spec
    tps_per_gsu = record.gsu_capacity_tpm / 60.0

    req_weighted_burndown = compute_weighted_burndown_tokens(
        input_tokens=req.total_input_tokens,
        output_tokens=req.output_tokens,
        thoughts_tokens=req.thoughts_tokens,
        cached_tokens=req.cached_tokens,
        model=req.model,
        cache_write_5m_tokens=req.cache_write_5m_tokens,
        cache_write_1h_tokens=req.cache_write_1h_tokens,
        catalog=cat,
    )

    commitment = optimize_hybrid_commitment(req, catalog=cat)

    peak_minute_tpm = max(req.tpm_series, default=0.0)
    burst_inputs = (req.sixty_sec_tpm, req.peak_1s_tps, req.peak_10s_tps, req.peak_30s_tps)
    has_traffic = peak_minute_tpm > 0.0 or any(v is not None and v > 0.0 for v in burst_inputs)

    micro_burst = None
    if any(v is not None for v in burst_inputs):
        sixty_s = req.sixty_sec_tpm if req.sixty_sec_tpm is not None else peak_minute_tpm
        peak_1s = (
            req.peak_1s_tps
            if req.peak_1s_tps is not None
            else (sixty_s / 60.0) * ASSUMED_PEAK_TO_MEAN_RATIO
        )
        micro_burst = evaluate_micro_burst_window(
            MicroBurstRequest(
                model=req.model,
                provisioned_gsus=max(1, commitment.optimal_gsus) if has_traffic else 0,
                sixty_sec_tpm=sixty_s,
                peak_1s_tps=peak_1s,
                peak_10s_tps=req.peak_10s_tps,
                peak_30s_tps=req.peak_30s_tps,
            ),
            catalog=cat,
        )

    if req.requests_per_second is not None:
        requests_per_second = req.requests_per_second
    elif req_weighted_burndown > 0.0:
        requests_per_second = (peak_minute_tpm / 60.0) / req_weighted_burndown
    else:
        requests_per_second = 0.0

    prefix_cache = evaluate_prefix_cache_dilution(
        total_input_tokens=req.total_input_tokens,
        shared_prefix_ratio=req.shared_prefix_ratio,
        baseline_cache_hit_rate=req.baseline_cache_hit_rate,
        num_round_robin_endpoints=req.num_round_robin_endpoints,
        tps_per_gsu=tps_per_gsu,
        cached_token_weight=spec.standard_cached_read_weight,
        uncached_token_weight=spec.standard_input_weight,
        requests_per_second=requests_per_second if has_traffic else 0.0,
    )

    return OptimizeResponse(
        model=req.model,
        normalized_model=norm_model,
        weighted_burndown_tokens_per_request=round(req_weighted_burndown, 2),
        commitment=commitment,
        micro_burst=micro_burst,
        prefix_cache=prefix_cache,
    )
