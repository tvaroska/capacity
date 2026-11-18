"""Telemetry driven port (`TelemetryPort`), series folding, and `recommend_capacity`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from capacity.contract.common import CapacityModeLiteral, SeriesKey
from capacity.contract.results import RecommendationResponse
from capacity.engine.sizing import build_response
from capacity.engine.stats import DEFAULT_DAYS


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
