from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import Forbidden, GoogleAPICallError
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import monitoring_v3

logger = logging.getLogger(__name__)

PT_METRIC = "aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput"

DEFAULT_DAYS = 7
DEFAULT_PERCENTILES = [50, 75, 90, 95]


def _build_filter(
    metric_type: str,
    regions: list[str] | None,
    extra: str | None = None,
) -> str:
    parts = [f'metric.type = "{metric_type}"']
    if extra:
        parts.append(extra)
    if regions:
        quoted = ", ".join(f'"{r}"' for r in regions)
        parts.append(f"resource.labels.location = one_of({quoted})")
    return " AND ".join(parts)


def _get_model(ts) -> str:
    return (
        ts.resource.labels.get("model_id")
        or ts.resource.labels.get("model_user_id")
        or "unknown"
    )


def percentile(sorted_vals: list[int], p: int) -> int:
    if not sorted_vals:
        return 0
    k = (p / 100) * (len(sorted_vals) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[f]
    return round(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]))


async def fetch_throughput(
    project: str,
    days: int = DEFAULT_DAYS,
    models: list[str] | None = None,
    regions: list[str] | None = None,
) -> tuple[dict[str, dict[datetime, int]], int]:
    """Fetch consumed_token_throughput (PT mode).

    Returns (sparse_data, total_minutes) where sparse_data is
    {model: {timestamp: token_count}}.
    """
    try:
        client = monitoring_v3.MetricServiceAsyncClient()
    except DefaultCredentialsError:
        raise RuntimeError(
            "GCP credentials not found. Run 'gcloud auth application-default login' "
            "or set GOOGLE_APPLICATION_CREDENTIALS."
        )
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=days)
    interval = monitoring_v3.TimeInterval(start_time=start, end_time=now)
    total_minutes = int((now - start).total_seconds()) // 60

    sparse: dict[str, dict[datetime, int]] = defaultdict(lambda: defaultdict(int))

    for req_type in ("dedicated", "shared"):
        filter_str = _build_filter(
            PT_METRIC,
            regions,
            extra=f'metric.labels.request_type = "{req_type}"',
        )
        try:
            results = await client.list_time_series(
                request={
                    "name": f"projects/{project}",
                    "filter": filter_str,
                    "interval": interval,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    "aggregation": monitoring_v3.Aggregation(
                        alignment_period={"seconds": 60},
                        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
                    ),
                }
            )
        except Forbidden as exc:
            raise RuntimeError(
                f"Permission denied for project '{project}'. "
                "The caller needs the monitoring.timeSeries.list permission "
                "(e.g. roles/monitoring.viewer)."
            ) from exc
        except GoogleAPICallError as exc:
            logger.warning("Failed to fetch %s throughput: %s", req_type, exc)
            continue

        async for ts in results:
            model = _get_model(ts)
            if models and model not in models:
                continue
            for point in ts.points:
                t = point.interval.end_time.replace(second=0, microsecond=0)
                val = point.value.int64_value if point.value.int64_value != 0 else point.value.double_value
                sparse[model][t] += val

    return dict(sparse), total_minutes
