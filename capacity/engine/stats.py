"""Pure-Python percentile interpolation, convex integer bisection, and Holt trend maths."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from capacity.contract.common import SeriesKey

DEFAULT_DAYS = 30
DEFAULT_PERCENTILES = [50, 75, 90, 95]
# Canonical Monday 00:00 UTC epoch shared across all offline minute-list and demo traces.
DEFAULT_TRACE_EPOCH_UTC = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

type SparseSeriesMap = (
    Mapping[SeriesKey, Mapping[datetime, int]] | Mapping[str, Mapping[datetime, int]]
)


def minute_list_to_sparse(
    values: Sequence[int | float],
    *,
    start: datetime = DEFAULT_TRACE_EPOCH_UTC,
) -> dict[datetime, int]:
    """Convert a sequence of per-minute TPMs into a UTC-dated sparse map with uniform rounding."""
    series: dict[datetime, int] = {}
    for idx, raw in enumerate(values):
        fv = float(raw)
        if not math.isfinite(fv):
            raise ValueError(f"trace minute {idx} must be finite, got {raw!r}")
        if fv < 0.0:
            raise ValueError(f"trace minute {idx} must be >= 0, got {raw!r}")
        rounded = round(fv)
        if rounded > 0:
            series[start + timedelta(minutes=idx)] = rounded
    return series


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


def argmin_convex_int(cost: Callable[[int], float], upper: int) -> int:
    """Smallest minimiser of a convex cost over the integers 0..upper, by bisection.

    A convex cost stops falling exactly once, so the first `n` whose successor is no cheaper
    is the optimum. That takes about log2(upper) evaluations instead of `upper`.
    """
    lo, hi = 0, max(0, upper)
    while lo < hi:
        mid = (lo + hi) // 2
        if cost(mid + 1) < cost(mid):
            lo = mid + 1
        else:
            hi = mid
    return lo


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
