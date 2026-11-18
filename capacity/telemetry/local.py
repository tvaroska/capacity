"""Zero-credential local trace adapter (`LocalTraceAdapter`) and `--demo` diurnal generator."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from capacity.contract.common import SeriesKey
from capacity.engine.stats import DEFAULT_DAYS, DEFAULT_TRACE_EPOCH_UTC, SparseSeriesMap
from capacity.telemetry.port import filter_and_fold_series


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
