"""Pure-Python percentile, Holt trend math, demo traces, and Hexagonal telemetry port."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

DEFAULT_DAYS = 30
DEFAULT_PERCENTILES = [50, 75, 90, 95]


class UnknownModelError(ValueError):
    """Raised when a model or self-hosted profile has no known capacity, instead of guessing."""


@runtime_checkable
class TelemetryPort(Protocol):
    """Driven port for fetching per-minute token throughput across projects and regions."""

    async def fetch_series(
        self,
        project: str | Sequence[str],
        days: int = DEFAULT_DAYS,
        models: Sequence[str] | None = None,
        regions: Sequence[str] | None = None,
        by_region: bool = False,
        folder_map: Mapping[str, str] | None = None,
        mode: str = "gsu",
    ) -> tuple[dict[str, dict[datetime, int]], int]:
        """Return `(sparse_series_by_key, total_window_minutes)`."""


class LocalTraceAdapter:
    """Zero-credential local adapter satisfying `TelemetryPort` for offline traces and tests."""

    def __init__(
        self,
        sparse_series: Mapping[str, Mapping[datetime, int]] | None = None,
        total_minutes: int | None = None,
    ) -> None:
        self._sparse = (
            {k: dict(v) for k, v in sparse_series.items()} if sparse_series is not None else None
        )
        self._total_minutes = total_minutes

    async def fetch_series(
        self,
        project: str | Sequence[str],
        days: int = DEFAULT_DAYS,
        models: Sequence[str] | None = None,
        regions: Sequence[str] | None = None,
        by_region: bool = False,
        folder_map: Mapping[str, str] | None = None,
        mode: str = "gsu",
    ) -> tuple[dict[str, dict[datetime, int]], int]:
        proj_list = [project] if isinstance(project, str) else list(project)
        first_proj = proj_list[0] if proj_list else "local-project"
        folder_prefix = (
            f"{folder_map[first_proj]}/"
            if folder_map and first_proj in folder_map
            else ""
        )
        if self._sparse is not None:
            raw_sparse = self._sparse
            total_mins = (
                self._total_minutes if self._total_minutes is not None else days * 24 * 60
            )
        else:
            raw_sparse, total_mins = build_demo_throughput(
                days=days, by_region=by_region, mode=mode
            )

        models_filter = {m.lower() for m in models} if models else None
        regions_filter = set(regions) if regions else None
        filtered: dict[str, dict[datetime, int]] = {}
        for raw_key, pts in raw_sparse.items():
            model_part, sep, reg_part = raw_key.partition("@")
            if models_filter and not any(model_part.lower().startswith(m) for m in models_filter):
                continue
            if regions_filter and sep and reg_part not in regions_filter:
                continue
            out_model = (
                f"{folder_prefix}{model_part}"
                if folder_prefix and "/" not in model_part
                else model_part
            )
            if by_region and sep:
                out_key = f"{out_model}@{reg_part}"
            elif by_region:
                out_key = f"{out_model}@global"
            else:
                out_key = out_model
            bucket = filtered.setdefault(out_key, {})
            for dt, val in pts.items():
                bucket[dt] = bucket.get(dt, 0) + int(val)
        return filtered, total_mins


def percentile(sorted_vals: Sequence[int | float], p: float) -> int:
    """Compute linear-interpolated percentile on a pre-sorted numeric sequence."""
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_vals[f])
    return round(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]))


def holts_linear_trend(
    series: Sequence[int | float],
    alpha: float = 0.3,
    beta: float = 0.1,
    steps: int = 24,
) -> list[float]:
    """Project capacity series forward using pure-Python Holt's Double Exponential Smoothing."""
    if steps <= 0:
        return []
    if not series:
        return [0.0] * steps
    if len(series) < 2:
        val = max(0.0, float(series[0]))
        return [round(val, 2)] * steps

    level = float(series[0])
    trend = float(series[1]) - float(series[0])

    for i in range(1, len(series)):
        y_t = float(series[i])
        last_level = level
        level = alpha * y_t + (1.0 - alpha) * (level + trend)
        trend = beta * (level - last_level) + (1.0 - beta) * trend

    forecasts: list[float] = []
    for m in range(1, steps + 1):
        pred = max(0.0, level + m * trend)
        forecasts.append(round(pred, 2))
    return forecasts


def build_demo_throughput(
    days: int = 7,
    by_region: bool = True,
    mode: str = "gsu",
) -> tuple[dict[str, dict[datetime, int]], int]:
    """Generate deterministic 8x5 enterprise diurnal sparse throughput series for `--demo` mode."""
    total_minutes = days * 24 * 60
    base_dt = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    sparse: dict[str, dict[datetime, int]] = defaultdict(dict)

    for day_idx in range(days):
        is_weekday = (day_idx % 7) < 5
        if not is_weekday:
            continue
        for minute_of_day in range(8 * 60, 16 * 60):
            dt = base_dt + timedelta(days=day_idx, minutes=minute_of_day)
            is_peak_window = 10 * 60 <= minute_of_day < 14 * 60
            if mode == "self-hosted":
                k1 = "llama-3.3-70b-fp8@us-central1" if by_region else "llama-3.3-70b-fp8"
                k2 = "gemma-3-27b-bf16@europe-west4" if by_region else "gemma-3-27b-bf16"
                sparse[k1][dt] = 520_000 if is_peak_window else 340_000
                sparse[k2][dt] = 195_000 if is_peak_window else 130_000
            else:
                k1 = "gemini-3.8-flash@us-central1" if by_region else "gemini-3.8-flash"
                k2 = "gemini-4-argon@us-central1" if by_region else "gemini-4-argon"
                k3 = "gemini-3.8-flash@europe-west4" if by_region else "gemini-3.8-flash"
                sparse[k1][dt] = 483_600 if is_peak_window else 241_800
                sparse[k2][dt] = 90_000 if is_peak_window else 60_000
                if by_region:
                    sparse[k3][dt] = 241_800 if is_peak_window else 120_900

    return dict(sparse), total_minutes
