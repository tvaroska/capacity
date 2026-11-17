"""Pure-Python percentile, Holt trend math, demo traces, and Hexagonal telemetry port."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from capacity.models import SeriesKey

type SparseSeriesMap = (
    Mapping[SeriesKey, Mapping[datetime, int]] | Mapping[str, Mapping[datetime, int]]
)

DEFAULT_DAYS = 30
DEFAULT_PERCENTILES = [50, 75, 90, 95]
# Canonical Monday 00:00 UTC epoch shared across all offline minute-list and demo traces.
DEFAULT_TRACE_EPOCH_UTC = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


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


def filter_and_fold_series(
    entries: Iterable[tuple[str | None, SeriesKey, Mapping[datetime, int]]],
    *,
    models: Sequence[str] | None = None,
    regions: Sequence[str] | None = None,
    by_region: bool = False,
    folder_map: Mapping[str, str] | None = None,
) -> dict[SeriesKey, dict[datetime, int]]:
    """Filter by model/region, apply `folder_map`, and sum points by typed `SeriesKey`."""
    models_filter = {m.lower() for m in models} if models else None
    regions_filter = set(regions) if regions else None
    folded: dict[SeriesKey, dict[datetime, int]] = {}

    for project_id, key, pts in entries:
        if models_filter and not any(key.model.lower().startswith(m) for m in models_filter):
            continue
        if regions_filter and key.region is not None and key.region not in regions_filter:
            continue

        mapped_folder = (
            folder_map[project_id]
            if (folder_map and project_id and project_id in folder_map and key.folder is None)
            else key.folder
        )
        out_region = (key.region or "global") if by_region else None
        out_key = SeriesKey(model=key.model, region=out_region, folder=mapped_folder)
        bucket = folded.setdefault(out_key, {})
        for dt, val in pts.items():
            bucket[dt] = bucket.get(dt, 0) + int(val)
    return folded


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
    ) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
        """Return `(sparse_series_by_key, total_window_minutes)`."""


class LocalTraceAdapter:
    """Zero-credential local adapter satisfying `TelemetryPort` for offline traces and tests."""

    def __init__(
        self,
        sparse_series: SparseSeriesMap | None = None,
        total_minutes: int | None = None,
    ) -> None:
        if sparse_series is None:
            self._sparse: dict[SeriesKey, dict[datetime, int]] | None = None
        else:
            parsed: dict[SeriesKey, dict[datetime, int]] = {}
            for raw_k, pts in sparse_series.items():
                skey = (
                    raw_k
                    if isinstance(raw_k, SeriesKey)
                    else SeriesKey.parse(raw_k, by_region="@" in raw_k)
                )
                bucket = parsed.setdefault(skey, {})
                for dt, val in pts.items():
                    bucket[dt] = bucket.get(dt, 0) + int(val)
            self._sparse = parsed
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
    ) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
        proj_list = [project] if isinstance(project, str) else list(project)
        first_proj = proj_list[0] if proj_list else "local-project"
        if self._sparse is not None:
            raw_sparse = self._sparse
            longest = max((len(v) for v in raw_sparse.values()), default=0)
            base_mins = self._total_minutes if self._total_minutes is not None else days * 24 * 60
            total_mins = max(base_mins, longest)
        else:
            raw_sparse, total_mins = build_demo_throughput(
                days=days, by_region=by_region, mode=mode
            )

        entries = [(first_proj, skey, pts) for skey, pts in raw_sparse.items()]
        return (
            filter_and_fold_series(
                entries,
                models=models,
                regions=regions,
                by_region=by_region,
                folder_map=folder_map,
            ),
            total_mins,
        )


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


def build_demo_throughput(
    days: int = 7,
    by_region: bool = True,
    mode: str = "gsu",
) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
    """Generate deterministic 8x5 enterprise diurnal sparse throughput series for `--demo` mode."""
    total_minutes = days * 24 * 60
    sparse: dict[SeriesKey, dict[datetime, int]] = defaultdict(dict)

    for day_idx in range(days):
        if (day_idx % 7) >= 5:
            continue
        for minute_of_day in range(8 * 60, 16 * 60):
            dt = DEFAULT_TRACE_EPOCH_UTC + timedelta(days=day_idx, minutes=minute_of_day)
            is_peak_window = 10 * 60 <= minute_of_day < 14 * 60
            if mode == "self-hosted":
                k1 = SeriesKey("llama-3.3-70b-fp8", "us-central1" if by_region else None)
                k2 = SeriesKey("gemma-3-27b-bf16", "europe-west4" if by_region else None)
                sparse[k1][dt] = 520_000 if is_peak_window else 340_000
                sparse[k2][dt] = 195_000 if is_peak_window else 130_000
            else:
                k1 = SeriesKey("gemini-3.8-flash", "us-central1" if by_region else None)
                k2 = SeriesKey("gemini-4-argon", "us-central1" if by_region else None)
                sparse[k1][dt] = 483_600 if is_peak_window else 241_800
                sparse[k2][dt] = 90_000 if is_peak_window else 60_000
                if by_region:
                    k3 = SeriesKey("gemini-3.8-flash", "europe-west4")
                    sparse[k3][dt] = 241_800 if is_peak_window else 120_900

    return dict(sparse), total_minutes
