from __future__ import annotations

import math
import os
import re
from datetime import datetime

from .core import DEFAULT_PERCENTILES, percentile
from .models import ModelRecommendation, PercentileDetail, RecommendationResponse


def _env_key_for_model(model: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]", "_", model).upper()
    return f"GSU_CAPACITY_{sanitized}"


def load_gsu_config() -> dict[str, int]:
    prefix = "GSU_CAPACITY_"
    return {
        k[len(prefix) :].lower(): int(v)
        for k, v in os.environ.items()
        if k.startswith(prefix) and v.isdigit()
    }


def _find_capacity(model: str, gsu_config: dict[str, int]) -> int | None:
    sanitized = re.sub(r"[^a-zA-Z0-9]", "_", model).lower()
    return gsu_config.get(sanitized)


def compute_recommendations(
    sparse: dict[str, dict[datetime, int]],
    total_minutes: int,
    percentiles: list[int] | None = None,
    gsu_config: dict[str, int] | None = None,
) -> list[ModelRecommendation]:
    if percentiles is None:
        percentiles = DEFAULT_PERCENTILES
    if gsu_config is None:
        gsu_config = load_gsu_config()

    results: list[ModelRecommendation] = []

    for model in sorted(sparse):
        vals = sorted(sparse[model].values())
        zeros_to_add = total_minutes - len(vals)
        if zeros_to_add > 0:
            vals = [0] * zeros_to_add + vals

        capacity = _find_capacity(model, gsu_config)
        pct_details: dict[str, PercentileDetail] = {}

        for p in percentiles:
            tpm = percentile(vals, p)
            if capacity:
                gsus = math.ceil(tpm / capacity) if tpm > 0 else 0
            else:
                gsus = None
            pct_details[f"P{p}"] = PercentileDetail(tpm=tpm, recommended_gsus=gsus)

        results.append(
            ModelRecommendation(
                model=model,
                percentiles=pct_details,
            )
        )

    return results


def build_response(
    project: str,
    days: int,
    sparse: dict[str, dict[datetime, int]],
    total_minutes: int,
    percentiles: list[int] | None = None,
) -> RecommendationResponse:
    if percentiles is None:
        percentiles = DEFAULT_PERCENTILES

    models = compute_recommendations(sparse, total_minutes, percentiles)

    return RecommendationResponse(
        project=project,
        period_days=days,
        percentiles=percentiles,
        models=models,
    )
