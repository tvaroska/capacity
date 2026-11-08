"""Core Cloud Monitoring telemetry queries, multi-project/folder aggregation, and Holt forecasting.

Supports:
1. Managed Gemini Platform GSUs (`.../publisher/online_serving/consumed_token_throughput`).
2. Self-Hosted Vertex AI Online Prediction endpoints (`.../prediction/online/prediction_count`,
   `accelerator/duty_cycle`, and `prometheus.googleapis.com/vertex_vllm_gpu_cache_usage_perc` /
   `vertex_vllm_num_requests_waiting`).
3. Multi-project & folder roll-up plus pure-Python Holt's Double Exponential Smoothing.
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

try:
    from google.api_core.exceptions import GoogleAPICallError, PermissionDenied
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import monitoring_v3
    from google.protobuf import duration_pb2
except ImportError:  # pragma: no cover

    class GoogleAPICallError(Exception):  # type: ignore[no-redef]
        message: str = ""

    class PermissionDenied(GoogleAPICallError):  # type: ignore[no-redef]
        pass

    class DefaultCredentialsError(Exception):  # type: ignore[no-redef]
        pass

    class _DurationNamespace:
        @staticmethod
        def Duration(seconds: int = 60) -> Any:  # noqa: ANN401
            return SimpleNamespace(seconds=seconds)

    class _MonitoringV3Fallback:
        class Aggregation:
            class Aligner:
                ALIGN_SUM = 1

            class Reducer:
                REDUCE_SUM = 1

            def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class ListTimeSeriesRequest:
            class TimeSeriesView:
                FULL = 1

        class TimeInterval:
            def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class MetricServiceAsyncClient:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
                err_cls: Any = DefaultCredentialsError
                raise err_cls(
                    "google-cloud-monitoring is not installed or credentials missing."
                )

    duration_pb2 = _DurationNamespace()
    monitoring_v3 = _MonitoringV3Fallback()

DEFAULT_DAYS = 30
DEFAULT_PERCENTILES = [50, 75, 90, 95]

PT_METRIC = "aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput"
SELF_HOSTED_PREDICTION_METRIC = "aiplatform.googleapis.com/prediction/online/prediction_count"
SELF_HOSTED_DUTY_CYCLE_METRIC = "aiplatform.googleapis.com/prediction/online/accelerator/duty_cycle"
SELF_HOSTED_KV_CACHE_METRIC = "prometheus.googleapis.com/vertex_vllm_gpu_cache_usage_perc"
SELF_HOSTED_QUEUE_METRIC = "prometheus.googleapis.com/vertex_vllm_num_requests_waiting"

GROUP_BY_FIELDS = [
    "resource.label.model_id",
    "resource.label.location",
    "metric.label.request_type",
]

SELF_HOSTED_GROUP_BY_FIELDS = [
    "resource.label.endpoint_id",
    "resource.label.location",
]


class GCPQueryError(Exception):
    """Raised when a GCP Cloud Monitoring query fails due to auth, permissions, or API errors."""


def _build_filter(
    metric: str,
    regions: Sequence[str] | None = None,
    extra: str | None = None,
) -> str:
    parts = [f'metric.type = "{metric}"']
    if regions:
        quoted = ", ".join(f'"{r}"' for r in regions)
        parts.append(f"resource.labels.location = one_of({quoted})")
    if extra:
        parts.append(extra)
    return " AND ".join(parts)


def _build_aggregation(
    group_by_fields: Sequence[str] | None = None,
    aligner: int = monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
    reducer: int = monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
) -> monitoring_v3.Aggregation:
    return monitoring_v3.Aggregation(
        alignment_period=duration_pb2.Duration(seconds=60),
        per_series_aligner=aligner,
        cross_series_reducer=reducer,
        group_by_fields=list(
            group_by_fields if group_by_fields is not None else GROUP_BY_FIELDS
        ),
    )


async def _query_series(
    client: monitoring_v3.MetricServiceAsyncClient,
    project: str,
    filter_str: str,
    interval: monitoring_v3.TimeInterval,
    aggregation: monitoring_v3.Aggregation,
) -> list[Any]:
    request = {
        "name": f"projects/{project}",
        "filter": filter_str,
        "interval": interval,
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        "aggregation": aggregation,
    }
    try:
        pager = await client.list_time_series(request=request)
        return [ts async for ts in pager]
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login` "
            "or pass `--demo` / `--trace-file` for offline simulation."
        ) from exc
    except PermissionDenied as exc:
        raise GCPQueryError(
            f"Permission denied querying Cloud Monitoring for project '{project}'. "
            f"Ensure the caller has `roles/monitoring.viewer`. Details: {exc.message}"
        ) from exc
    except GoogleAPICallError as exc:
        raise GCPQueryError(
            f"Cloud Monitoring API error for project '{project}': {exc.message}"
        ) from exc


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
        # Monday (0) through Friday (4) active 08:00..16:00 UTC (480 mins/day)
        is_weekday = (day_idx % 7) < 5
        if not is_weekday:
            continue
        for minute_of_day in range(8 * 60, 16 * 60):
            dt = base_dt + timedelta(days=day_idx, minutes=minute_of_day)
            # Peak around 10:00-14:00
            is_peak_window = 10 * 60 <= minute_of_day < 14 * 60
            if mode == "self-hosted":
                k1 = (
                    "llama-3.3-70b-fp8@us-central1"
                    if by_region
                    else "llama-3.3-70b-fp8"
                )
                k2 = (
                    "gemma-3-27b-bf16@europe-west4"
                    if by_region
                    else "gemma-3-27b-bf16"
                )
                sparse[k1][dt] = 520_000 if is_peak_window else 340_000
                sparse[k2][dt] = 195_000 if is_peak_window else 130_000
            else:
                k1 = (
                    "gemini-3.8-flash@us-central1"
                    if by_region
                    else "gemini-3.8-flash"
                )
                k2 = (
                    "gemini-4-argon@us-central1"
                    if by_region
                    else "gemini-4-argon"
                )
                k3 = (
                    "gemini-3.8-flash@europe-west4"
                    if by_region
                    else "gemini-3.8-flash"
                )
                sparse[k1][dt] = 483_600 if is_peak_window else 241_800
                sparse[k2][dt] = 90_000 if is_peak_window else 60_000
                if by_region:
                    sparse[k3][dt] = 241_800 if is_peak_window else 120_900

    return dict(sparse), total_minutes


async def fetch_throughput(
    project: str | Sequence[str],
    days: int = DEFAULT_DAYS,
    models: Sequence[str] | None = None,
    regions: Sequence[str] | None = None,
    by_region: bool = False,
    folder_map: Mapping[str, str] | None = None,
    mode: str = "gsu",
    tokens_per_prediction: int = 1_800,
) -> tuple[dict[str, dict[datetime, int]], int]:
    """Fetch per-minute token throughput across one or more GCP projects.

    Supports both `--mode gsu` (Managed Gemini Platform GSUs) and `--mode self-hosted`
    (Vertex AI Online Prediction `DedicatedResources` endpoints).
    """
    try:
        client = monitoring_v3.MetricServiceAsyncClient()
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login` "
            "or pass `--demo` / `--trace-file` for offline simulation."
        ) from exc

    project_list = [project] if isinstance(project, str) else list(project)
    if not project_list:
        project_list = ["default-project"]

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now - timedelta(days=days)
    interval = monitoring_v3.TimeInterval(start_time=start, end_time=now)
    total_minutes = days * 24 * 60

    sparse: dict[str, dict[datetime, int]] = defaultdict(lambda: defaultdict(int))
    models_filter = {m.lower() for m in models} if models else None

    if mode == "self-hosted":
        agg = _build_aggregation(group_by_fields=SELF_HOSTED_GROUP_BY_FIELDS)
        pred_filter = _build_filter(SELF_HOSTED_PREDICTION_METRIC, regions)
        tasks = [
            _query_series(client, proj, pred_filter, interval, agg)
            for proj in project_list
        ]
        results = await asyncio.gather(*tasks)
        for proj_id, series_list in zip(project_list, results, strict=False):
            folder_prefix = (
                f"{folder_map[proj_id]}/"
                if folder_map and proj_id in folder_map
                else ""
            )
            for ts in series_list:
                endpoint_id = ts.resource.labels.get("endpoint_id", "self-hosted-endpoint")
                location = ts.resource.labels.get("location", "global")
                if models_filter and not any(
                     endpoint_id.lower().startswith(m) for m in models_filter
                ):
                    continue
                base_key = f"{folder_prefix}{endpoint_id}"
                key = f"{base_key}@{location}" if by_region else base_key
                for pt in ts.points:
                    dt = pt.interval.end_time
                    if isinstance(dt, datetime):
                        dt_key = dt.replace(second=0, microsecond=0)
                    else:
                        dt_key = datetime.fromtimestamp(dt.timestamp(), tz=UTC).replace(
                            second=0, microsecond=0
                        )
                    val = int(pt.value.int64_value or round(pt.value.double_value))
                    sparse[key][dt_key] += val * max(1, tokens_per_prediction)
        return {k: dict(v) for k, v in sparse.items()}, total_minutes

    aggregation = _build_aggregation(group_by_fields=GROUP_BY_FIELDS)
    dedicated_filter = _build_filter(
        PT_METRIC,
        regions,
        'metric.labels.request_type = "dedicated"',
    )
    shared_filter = _build_filter(
        PT_METRIC,
        regions,
        'metric.labels.request_type = "shared"',
    )

    coros = []
    proj_for_coro: list[str] = []
    for proj_id in project_list:
        coros.append(_query_series(client, proj_id, dedicated_filter, interval, aggregation))
        proj_for_coro.append(proj_id)
        coros.append(_query_series(client, proj_id, shared_filter, interval, aggregation))
        proj_for_coro.append(proj_id)

    all_series_lists = await asyncio.gather(*coros)

    for proj_id, series_list in zip(proj_for_coro, all_series_lists, strict=False):
        folder_prefix = (
            f"{folder_map[proj_id]}/"
            if folder_map and proj_id in folder_map
            else ""
        )
        for ts in series_list:
            model_id = ts.resource.labels.get("model_id", "unknown")
            location = ts.resource.labels.get("location", "global")
            if models_filter and not any(
                model_id.lower().startswith(m) for m in models_filter
            ):
                continue
            base_key = f"{folder_prefix}{model_id}"
            key = f"{base_key}@{location}" if by_region else base_key
            for pt in ts.points:
                dt = pt.interval.end_time
                if isinstance(dt, datetime):
                    dt_key = dt.replace(second=0, microsecond=0)
                else:
                    dt_key = datetime.fromtimestamp(dt.timestamp(), tz=UTC).replace(
                        second=0, microsecond=0
                    )
                val = int(pt.value.int64_value or round(pt.value.double_value))
                sparse[key][dt_key] += val

    return {k: dict(v) for k, v in sparse.items()}, total_minutes
