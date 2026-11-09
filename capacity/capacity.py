"""Empirical percentile & Holt forecast sizing for Managed GSUs and Self-Hosted Replicas."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from datetime import datetime

from capacity.core import DEFAULT_PERCENTILES, holts_linear_trend, percentile
from capacity.models import (
    ModelRecommendation,
    PercentileDetail,
    RecommendationResponse,
)
from capacity.self_hosted import DEFAULT_SELF_HOSTED_PROFILES

# Built-in GSU capacity table (sustained burndown Tokens Per Minute per 1 GSU = TPS_per_GSU * 60).
DEFAULT_GSU_CONFIG: dict[str, int] = {
    "gemini-2.5-pro": 39_000,          # 650 TPS/GSU
    "gemini-2.5-flash": 161_400,       # 2,690 TPS/GSU
    "gemini-2.5-flash-lite": 484_200,  # 8,070 TPS/GSU
    "gemini-3-pro": 30_000,            # 500 TPS/GSU
    "gemini-3.1-pro": 30_000,          # 500 TPS/GSU
    "gemini-3-flash": 120_900,         # 2,015 TPS/GSU
    "gemini-3.1-flash-lite": 241_800,  # 4,030 TPS/GSU
    "gemini-3.5-pro": 30_000,          # 500 TPS/GSU
    "gemini-3.5-flash": 120_900,       # 2,015 TPS/GSU
    "gemini-3.5-flash-lite": 241_800,  # 4,030 TPS/GSU
    "gemini-3.6-flash": 120_900,       # 2,015 TPS/GSU
    "gemini-3.7-flash": 120_900,       # 2,015 TPS/GSU
    "gemini-3.7-flash-fast": 60_450,   # 1,007.5 TPS/GSU (~2.5x decode speed @ 2.0x compute weight)
    "gemini-3.8-flash": 120_900,       # 2,015 TPS/GSU
    "gemini-3.8-flash-fast": 60_450,   # 1,007.5 TPS/GSU (~2.5x decode speed @ 2.0x compute weight)
    "gemini-4-argon": 30_000,          # 500 TPS/GSU
    "gemini-4.0-pro": 30_000,          # 500 TPS/GSU
    "gemini-embedding-2": 302_400,     # 5,040 TPS/GSU (256-token minimum request floor)
    "gemini-2.0-flash": 161_400,       # 2,690 TPS/GSU
    "gemini-2.0-flash-lite": 484_200,  # 8,070 TPS/GSU
}

ENV_PREFIX = "GSU_CAPACITY_"

_VERSION_SUFFIX_RE = re.compile(
    r"(?:-(?:00\d|preview(?:-\d{2}-\d{2})?|exp(?:-\d{4})?|rc\d*|ga|qcd|latest|v\d+))+$",
    re.IGNORECASE,
)


def normalize_model_id(model_id: str) -> str:
    """Normalize a model identifier by stripping publisher prefixes, folders, and suffixes."""
    cleaned = model_id.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    cleaned = _VERSION_SUFFIX_RE.sub("", cleaned)
    return cleaned


def load_gsu_config(include_defaults: bool = True, mode: str = "gsu") -> dict[str, int]:
    """Load capacity config (TPM per 1 GSU or 1 GPU replica) plus GSU_CAPACITY_* env vars."""
    config: dict[str, int] = {}
    if include_defaults:
        if mode == "self-hosted":
            config.update(
                {k: prof.tpm_per_replica for k, prof in DEFAULT_SELF_HOSTED_PROFILES.items()}
            )
        else:
            config.update(DEFAULT_GSU_CONFIG)
    for key, val in os.environ.items():
        if key.startswith(ENV_PREFIX):
            model_name = key[len(ENV_PREFIX) :].lower().replace("_", "-")
            dotted_name = re.sub(r"(\d)-(\d)", r"\1.\2", model_name)
            try:
                cap_val = int(val)
                config[model_name] = cap_val
                config[dotted_name] = cap_val
            except ValueError:
                continue
    return config


def _find_capacity(
    model_id: str,
    gsu_config: dict[str, int] | None = None,
    mode: str = "gsu",
) -> int | None:
    cfg = gsu_config if gsu_config is not None else load_gsu_config(mode=mode)
    raw = model_id.lower().split("@", 1)[0]
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    norm = normalize_model_id(raw)

    for candidate in (raw, norm):
        if candidate in cfg:
            return cfg[candidate]

    for prefix in sorted(cfg, key=len, reverse=True):
        if raw.startswith(prefix) or norm.startswith(prefix):
            return cfg[prefix]
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
    gsu_config: dict[str, int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: str = "gsu",
) -> list[ModelRecommendation]:
    """Compute both 24x7 (`all_hours`) and business-hours (`active_hours`) recommendations."""
    if percentiles is None:
        percentiles = DEFAULT_PERCENTILES
    if gsu_config is None:
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

        cap = _find_capacity(model_id, gsu_config, mode=mode)
        ordered_points = [
            v for _, v in sorted(minute_map.items(), key=lambda kv: kv[0]) if v > 0
        ]
        active_vals = sorted(ordered_points)
        active_count = len(active_vals)
        num_zeros = max(0, total_minutes - active_count)
        all_vals = [0] * num_zeros + active_vals

        all_hours_map = _build_percentile_map(all_vals, percentiles, cap)
        active_hours_map = _build_percentile_map(active_vals, percentiles, cap)
        primary_map = active_hours_map if active_only else all_hours_map
        duty_cycle = (
            round((active_count / total_minutes) * 100.0, 2)
            if total_minutes > 0
            else 0.0
        )

        # Holt's Double Exponential Smoothing forecast over chronological active series
        forecasts = (
            holts_linear_trend(ordered_points, steps=forecast_steps)
            if ordered_points and forecast_steps > 0
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
    gsu_config: dict[str, int] | None = None,
    active_only: bool = False,
    by_region: bool = False,
    forecast_steps: int = 24,
    mode: str = "gsu",
) -> RecommendationResponse:
    recs = compute_recommendations(
        sparse_series,
        total_minutes,
        percentiles,
        gsu_config,
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
