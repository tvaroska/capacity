"""Cloud Monitoring driven adapter for Managed GSUs and Self-Hosted Vertex AI Endpoints."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from capacity.forecasting import (
    DEFAULT_DAYS,
    DEFAULT_PERCENTILES,
    LocalTraceAdapter,
    TelemetryPort,
    UnknownModelError,
    build_demo_throughput,
    holts_linear_trend,
    percentile,
)
from capacity.model_families import is_partner_claude, resolve_model_family
from capacity.models import GCP_LOCATION_PATTERN, GCP_PROJECT_ID_PATTERN, MODEL_ID_PATTERN

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
    monitoring_v3 = _MonitoringV3Fallback()  # type: ignore[assignment]

PT_METRIC = "aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput"
PARTNER_TOKEN_METRIC = (
    "aiplatform.googleapis.com/publisher/online_serving/token_count"  # noqa: S105
)
SELF_HOSTED_PREDICTION_METRIC = "aiplatform.googleapis.com/prediction/online/prediction_count"

GROUP_BY_FIELDS = [
    "resource.label.model_id",
    "resource.label.location",
    "metric.label.request_type",
]

PARTNER_GROUP_BY_FIELDS = [
    "resource.label.model_id",
    "resource.label.location",
    "metric.label.request_type",
    "metric.label.type",
]

SELF_HOSTED_GROUP_BY_FIELDS = [
    "resource.label.endpoint_id",
    "resource.label.location",
]


def _partner_token_weight(model_id: str, token_type: str) -> float:
    """Map Cloud Monitoring `token_count` `metric.label.type` to 3P Claude burndown weight."""
    spec = resolve_model_family(model_id)
    tt = token_type.strip().lower()
    if tt == "output":
        return spec.standard_output_weight
    if tt in ("cache_read", "cached", "cache_read_input"):
        return spec.standard_cached_read_weight
    if tt in ("cache_write_1h", "cache_creation_1h"):
        return spec.cache_write_1h_weight
    if tt in ("cache_write", "cache_write_5m", "cache_creation", "cache_creation_5m"):
        return spec.cache_write_5m_weight
    return spec.standard_input_weight


class GCPQueryError(Exception):
    """Raised when a GCP Cloud Monitoring query fails due to auth, permissions, or API errors."""


def _validate_identifier(field_name: str, value: str, pattern: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(pattern, cleaned):
        raise ValueError(f"Invalid {field_name} {value!r}: must match {pattern}")
    return cleaned


def _build_filter(
    metric: str,
    regions: Sequence[str] | None = None,
    extra: str | None = None,
) -> str:
    if '"' in metric or "\n" in metric:
        raise ValueError(f"Invalid metric identifier: {metric!r}")
    parts = [f'metric.type = "{metric}"']
    if regions:
        validated_regions = [
            _validate_identifier("region", r, GCP_LOCATION_PATTERN) for r in regions
        ]
        quoted = ", ".join(f'"{r}"' for r in validated_regions)
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


async def fetch_throughput(
    project: str | Sequence[str],
    days: int = DEFAULT_DAYS,
    models: Sequence[str] | None = None,
    regions: Sequence[str] | None = None,
    by_region: bool = False,
    folder_map: Mapping[str, str] | None = None,
    mode: str = "gsu",
    tokens_per_prediction: int = 1_800,
    include_partner_models: bool = False,
) -> tuple[dict[str, dict[datetime, int]], int]:
    """Fetch per-minute token throughput across one or more GCP projects."""
    raw_projects = [project] if isinstance(project, str) else list(project)
    if not raw_projects:
        raw_projects = ["default-project"]
    project_list = [
        _validate_identifier("project", p, GCP_PROJECT_ID_PATTERN) for p in raw_projects
    ]
    if regions:
        for reg in regions:
            _validate_identifier("region", reg, GCP_LOCATION_PATTERN)
    if models:
        for mod in models:
            _validate_identifier("model", mod, MODEL_ID_PATTERN)

    try:
        client = monitoring_v3.MetricServiceAsyncClient()
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login` "
            "or pass `--demo` / `--trace-file` for offline simulation."
        ) from exc

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

    has_partner_filter = bool(models_filter and any(is_partner_claude(m) for m in models_filter))
    has_1p_filter = bool(models_filter and any(not is_partner_claude(m) for m in models_filter))
    query_1p = models_filter is None or has_1p_filter
    query_partner = include_partner_models or has_partner_filter

    coros = []
    coro_meta: list[tuple[str, bool]] = []
    if query_1p:
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
        for proj_id in project_list:
            coros.append(_query_series(client, proj_id, dedicated_filter, interval, aggregation))
            coro_meta.append((proj_id, False))
            coros.append(_query_series(client, proj_id, shared_filter, interval, aggregation))
            coro_meta.append((proj_id, False))

    if query_partner:
        partner_agg = _build_aggregation(group_by_fields=PARTNER_GROUP_BY_FIELDS)
        partner_filter = _build_filter(PARTNER_TOKEN_METRIC, regions)
        for proj_id in project_list:
            coros.append(_query_series(client, proj_id, partner_filter, interval, partner_agg))
            coro_meta.append((proj_id, True))

    all_series_lists = await asyncio.gather(*coros)

    for (proj_id, is_partner_query), series_list in zip(coro_meta, all_series_lists, strict=False):
        folder_prefix = (
            f"{folder_map[proj_id]}/"
            if folder_map and proj_id in folder_map
            else ""
        )
        for ts in series_list:
            model_id = ts.resource.labels.get("model_id", "unknown")
            location = ts.resource.labels.get("location", "global")
            if is_partner_query and not is_partner_claude(model_id):
                continue
            if models_filter and not any(
                model_id.lower().startswith(m) for m in models_filter
            ):
                continue
            token_weight = (
                _partner_token_weight(model_id, str(ts.metric.labels.get("type", "input")))
                if is_partner_query
                else 1.0
            )
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
                sparse[key][dt_key] += round(val * token_weight) if is_partner_query else val

    return {k: dict(v) for k, v in sparse.items()}, total_minutes


class CloudMonitoringAdapter:
    """Production Cloud Monitoring adapter implementing `TelemetryPort`."""

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
        return await fetch_throughput(
            project=project,
            days=days,
            models=models,
            regions=regions,
            by_region=by_region,
            folder_map=folder_map,
            mode=mode,
        )


__all__ = [
    "DEFAULT_DAYS",
    "DEFAULT_PERCENTILES",
    "GROUP_BY_FIELDS",
    "PARTNER_GROUP_BY_FIELDS",
    "PARTNER_TOKEN_METRIC",
    "PT_METRIC",
    "SELF_HOSTED_GROUP_BY_FIELDS",
    "SELF_HOSTED_PREDICTION_METRIC",
    "CloudMonitoringAdapter",
    "GCPQueryError",
    "LocalTraceAdapter",
    "TelemetryPort",
    "UnknownModelError",
    "_build_filter",
    "build_demo_throughput",
    "fetch_throughput",
    "holts_linear_trend",
    "percentile",
]
