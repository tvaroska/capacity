"""Empirical percentile & Holt forecast sizing for Managed GSUs and Self-Hosted Replicas."""

from __future__ import annotations

import math
import os
import re
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
    TelemetryPort,
    UnknownModelError,
    holts_linear_trend,
    percentile,
)
from capacity.model_families import CLAUDE_GSU_CONFIG, normalize_model_id, resolve_model_family
from capacity.models import (
    ModelRecommendation,
    OptimizeRequest,
    OptimizeResponse,
    PercentileDetail,
    RecommendationResponse,
)
from capacity.self_hosted import DEFAULT_SELF_HOSTED_PROFILES

# Built-in GSU capacity table (sustained burndown Tokens Per Minute per 1 GSU = TPS_per_GSU * 60).
DEFAULT_GSU_CONFIG: dict[str, int] = {
    "gemini-2.5-pro": 39_000,  # 650 TPS/GSU
    "gemini-2.5-flash": 161_400,  # 2,690 TPS/GSU
    "gemini-2.5-flash-lite": 484_200,  # 8,070 TPS/GSU
    "gemini-3-pro": 30_000,  # 500 TPS/GSU
    "gemini-3.1-pro": 30_000,  # 500 TPS/GSU
    "gemini-3-flash": 120_900,  # 2,015 TPS/GSU
    "gemini-3.1-flash-lite": 241_800,  # 4,030 TPS/GSU
    "gemini-3.5-pro": 30_000,  # 500 TPS/GSU
    "gemini-3.5-flash": 120_900,  # 2,015 TPS/GSU
    "gemini-3.5-flash-lite": 241_800,  # 4,030 TPS/GSU
    "gemini-3.6-flash": 120_900,  # 2,015 TPS/GSU
    "gemini-3.7-flash": 120_900,  # 2,015 TPS/GSU
    "gemini-3.7-flash-fast": 60_450,  # 1,007.5 TPS/GSU (~2.5x decode speed @ 2.0x compute weight)
    "gemini-3.8-flash": 120_900,  # 2,015 TPS/GSU
    "gemini-3.8-flash-fast": 60_450,  # 1,007.5 TPS/GSU (~2.5x decode speed @ 2.0x compute weight)
    "gemini-4-argon": 30_000,  # 500 TPS/GSU
    "gemini-4.0-pro": 30_000,  # 500 TPS/GSU
    "gemini-embedding-2": 302_400,  # 5,040 TPS/GSU (256-token minimum request floor)
    "gemini-2.0-flash": 161_400,  # 2,690 TPS/GSU
    "gemini-2.0-flash-lite": 484_200,  # 8,070 TPS/GSU
    **CLAUDE_GSU_CONFIG,
}

ENV_PREFIX = "GSU_CAPACITY_"

# ADR-002: without sub-second gateway logs, the peak is assumed at this multiple of the mean.
ASSUMED_PEAK_TO_MEAN_RATIO = 1.9


def load_gsu_config(mode: str = "gsu") -> dict[str, int]:
    """Load capacity config (TPM per 1 GSU or 1 GPU replica) plus GSU_CAPACITY_* env vars."""
    config: dict[str, int] = {}
    if mode == "self-hosted":
        config.update({k: prof.tpm_per_replica for k, prof in DEFAULT_SELF_HOSTED_PROFILES.items()})
    else:
        config.update(DEFAULT_GSU_CONFIG)
    for key, val in os.environ.items():
        if key.startswith(ENV_PREFIX):
            model_name = key[len(ENV_PREFIX) :].lower().replace("_", "-")
            dotted_name = re.sub(r"(\d)-(\d)", r"\1.\2", model_name)
            try:
                cap_val = int(val)
                for candidate in (
                    model_name,
                    dotted_name,
                    normalize_model_id(model_name),
                    normalize_model_id(dotted_name),
                ):
                    if candidate:
                        config[candidate] = cap_val
            except ValueError:
                continue
    return config


def require_capacity(model_id: str, gsu_config: dict[str, int] | None = None) -> int:
    """Return TPM per GSU for a model, raising instead of guessing a default."""
    cap = find_capacity(model_id, gsu_config)
    if cap is None:
        env_key = ENV_PREFIX + normalize_model_id(model_id).upper().replace(".", "_").replace(
            "-", "_"
        )
        raise UnknownModelError(
            f"No capacity table entry for model '{model_id}'. "
            f"Set {env_key}=<TPM per GSU> to supply one."
        )
    return cap


def find_capacity(model_id: str, gsu_config: dict[str, int] | None = None) -> int | None:
    """Return TPM per GSU (or replica) for a listed model, or None; never a neighbour's rate."""
    cfg = gsu_config if gsu_config is not None else load_gsu_config()
    raw = model_id.lower().split("@", 1)[0]
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    for candidate in (raw, normalize_model_id(raw)):
        if candidate in cfg:
            return cfg[candidate]
    return None


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
    sparse_series: dict[str, dict[datetime, int]],
    total_minutes: int,
    percentiles: Sequence[int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: str = "gsu",
) -> list[ModelRecommendation]:
    """Compute both 24x7 (`all_hours`) and business-hours (`active_hours`) recommendations."""
    if percentiles is None:
        percentiles = DEFAULT_PERCENTILES
    gsu_config = load_gsu_config(mode=mode)

    results: list[ModelRecommendation] = []
    for raw_key in sorted(sparse_series):
        minute_map = sparse_series[raw_key]
        if by_region and "@" in raw_key:
            model_part, region = raw_key.split("@", 1)
        else:
            model_part, region = raw_key, None

        if "/" in model_part and not model_part.startswith("publishers/"):
            folder_name, model_id = model_part.split("/", 1)
        else:
            folder_name, model_id = None, model_part

        cap = find_capacity(model_id, gsu_config)
        active_vals = sorted(v for v in minute_map.values() if v > 0)
        active_count = len(active_vals)
        num_zeros = max(0, total_minutes - active_count)
        all_vals = [0] * num_zeros + active_vals

        all_hours_map = _build_percentile_map(all_vals, percentiles, cap)
        active_hours_map = _build_percentile_map(active_vals, percentiles, cap)
        primary_map = active_hours_map if active_only else all_hours_map
        duty_cycle = round((active_count / total_minutes) * 100.0, 2) if total_minutes > 0 else 0.0

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
                model=model_id,
                normalized_model=normalize_model_id(model_id),
                region=region,
                folder=folder_name,
                gsu_capacity_tpm=cap,
                active_minutes=active_count,
                total_minutes=total_minutes,
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
    sparse_series: dict[str, dict[datetime, int]],
    total_minutes: int,
    percentiles: Sequence[int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: str = "gsu",
) -> RecommendationResponse:
    recs = compute_recommendations(
        sparse_series,
        total_minutes,
        percentiles,
        active_only=active_only,
        by_region=by_region,
        forecast_steps=forecast_steps,
        mode=mode,
    )
    return RecommendationResponse(
        project=project,
        days=days,
        active_only=active_only,
        by_region=by_region,
        mode=mode,
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
    mode: str = "gsu",
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


def evaluate_gsu_optimization(req: OptimizeRequest) -> OptimizeResponse:
    """Shared GSU Newsvendor + sub-second micro-burst + prefix-cache orchestrator (CLI & API)."""
    norm_model = normalize_model_id(req.model)
    spec = resolve_model_family(norm_model)
    tpm_per_gsu = require_capacity(norm_model)
    tps_per_gsu = tpm_per_gsu / 60.0

    req_weighted_burndown = compute_weighted_burndown_tokens(
        input_tokens=req.total_input_tokens,
        output_tokens=req.output_tokens,
        thoughts_tokens=req.thoughts_tokens,
        cached_tokens=req.cached_tokens,
        model=norm_model,
        cache_write_5m_tokens=req.cache_write_5m_tokens,
        cache_write_1h_tokens=req.cache_write_1h_tokens,
    )

    commitment = optimize_hybrid_commitment(
        tpm_series=req.tpm_series,
        model=norm_model,
        tpm_per_gsu=tpm_per_gsu,
        gsu_monthly_price_usd=req.gsu_monthly_price_usd,
        paygo_per_1m_burndown_tokens_usd=req.paygo_per_1m_burndown_tokens_usd,
        spillover_tier=req.spillover_tier,
        fsp_discount_pct=req.fsp_discount_pct,
        active_duty_cycle_pct=req.active_duty_cycle_pct,
    )

    peak_minute_tpm = max(req.tpm_series, default=0.0)
    burst_inputs = (req.sixty_sec_tpm, req.peak_1s_tps, req.peak_10s_tps, req.peak_30s_tps)
    has_traffic = peak_minute_tpm > 0.0 or any(v is not None and v > 0.0 for v in burst_inputs)

    # A burst verdict needs burst data. With none supplied there is nothing to audit, and an
    # invented load would hand every workload the same alarm.
    micro_burst = None
    if any(v is not None for v in burst_inputs):
        sixty_s = req.sixty_sec_tpm if req.sixty_sec_tpm is not None else peak_minute_tpm
        peak_1s = (
            req.peak_1s_tps
            if req.peak_1s_tps is not None
            else (sixty_s / 60.0) * ASSUMED_PEAK_TO_MEAN_RATIO
        )
        micro_burst = evaluate_micro_burst_window(
            sixty_sec_tpm=sixty_s,
            peak_1s_tps=peak_1s,
            provisioned_gsus=max(1, commitment.optimal_gsus) if has_traffic else 0,
            tps_per_gsu=tps_per_gsu,
            peak_10s_tps=req.peak_10s_tps,
            model=norm_model,
            peak_30s_tps=req.peak_30s_tps,
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
