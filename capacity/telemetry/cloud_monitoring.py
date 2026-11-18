"""Cloud Monitoring driven adapter for Managed GSUs and Self-Hosted Vertex AI Endpoints."""

from __future__ import annotations

import asyncio
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from capacity.contract.common import (
    GCP_LOCATION_PATTERN,
    GCP_PROJECT_ID_PATTERN,
    MODEL_ID_PATTERN,
    SeriesKey,
)
from capacity.contract.results import GcpAccessCheckReport, GcpProjectAccessStatus
from capacity.engine.catalog import (
    CLAUDE_3P_SONNET_SPEC,
    GEMINI_1P_FLASH_SPEC,
    SELF_HOSTED_VLLM_SPEC,
    is_partner_claude,
    normalize_model_id,
    resolve_model_family,
)
from capacity.engine.sizing import find_capacity
from capacity.engine.stats import DEFAULT_DAYS
from capacity.telemetry.port import filter_and_fold_series

PT_METRIC = GEMINI_1P_FLASH_SPEC.monitoring_metric
DEDICATED_LIMIT_METRIC = "aiplatform.googleapis.com/publisher/online_serving/dedicated_token_limit"
INVOCATION_COUNT_METRIC = (
    "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"
)
PARTNER_TOKEN_METRIC = CLAUDE_3P_SONNET_SPEC.monitoring_metric
SELF_HOSTED_PREDICTION_METRIC = SELF_HOSTED_VLLM_SPEC.monitoring_metric

LIMIT_GROUP_BY_FIELDS = [
    "resource.label.model_id",
    "resource.label.location",
]

INVOCATION_GROUP_BY_FIELDS = [
    "resource.label.model_id",
    "resource.label.location",
    "metric.label.response_code",
    "metric.label.request_type",
]

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
DEFAULT_RPC_TIMEOUT_SECONDS = 30.0


class GCPQueryError(RuntimeError):
    """Raised when a GCP Cloud Monitoring query fails due to auth, permissions, or API errors."""


def create_monitoring_client() -> Any:  # noqa: ANN401
    """Lazily import `google.cloud.monitoring_v3` and create an async client."""
    from google.cloud import monitoring_v3

    return monitoring_v3.MetricServiceAsyncClient()


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


def _validate_identifier(field_name: str, value: str, pattern: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(pattern, cleaned):
        raise ValueError(f"Invalid {field_name} {value!r}: must match {pattern}")
    return cleaned


def _whole_day_window(now: datetime, days: int) -> tuple[datetime, datetime]:
    """The last `days` complete UTC days before `now`."""
    end = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


def _minute_start(end_time: Any) -> datetime:  # noqa: ANN401
    """Key a 60-second point by the start of the minute it covers, not by its end."""
    end = (
        end_time
        if isinstance(end_time, datetime)
        else datetime.fromtimestamp(end_time.timestamp(), tz=UTC)
    )
    return end.replace(second=0, microsecond=0) - timedelta(minutes=1)


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


def _build_aggregation(group_by_fields: Sequence[str], *, align_max: bool = False) -> Any:  # noqa: ANN401
    from google.cloud import monitoring_v3
    from google.protobuf import duration_pb2

    aligner = (
        monitoring_v3.Aggregation.Aligner.ALIGN_MAX
        if align_max
        else monitoring_v3.Aggregation.Aligner.ALIGN_SUM
    )
    return monitoring_v3.Aggregation(
        alignment_period=duration_pb2.Duration(seconds=60),
        per_series_aligner=aligner,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
        group_by_fields=list(group_by_fields),
    )


async def _query_series(
    client: Any,  # noqa: ANN401
    project: str,
    filter_str: str,
    interval: Any,  # noqa: ANN401
    aggregation: Any,  # noqa: ANN401
) -> list[Any]:
    from google.api_core.exceptions import GoogleAPICallError, PermissionDenied
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
    from google.cloud import monitoring_v3

    request = {
        "name": f"projects/{project}",
        "filter": filter_str,
        "interval": interval,
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        "aggregation": aggregation,
    }
    try:
        pager = await client.list_time_series(request=request, timeout=DEFAULT_RPC_TIMEOUT_SECONDS)
        return [ts async for ts in pager]
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login` "
            "or pass `--demo` / `--trace-file` for offline simulation."
        ) from exc
    except GoogleAuthError as exc:
        raise GCPQueryError(f"GCP authentication failed for project '{project}': {exc}") from exc
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
) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
    """Fetch per-minute token throughput across one or more GCP projects."""
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
    from google.cloud import monitoring_v3

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
        client = create_monitoring_client()
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login` "
            "or pass `--demo` / `--trace-file` for offline simulation."
        ) from exc
    except GoogleAuthError as exc:
        raise GCPQueryError(f"GCP authentication failed: {exc}") from exc

    start, end = _whole_day_window(datetime.now(UTC), days)
    interval = monitoring_v3.TimeInterval(start_time=start, end_time=end)
    total_minutes = days * 24 * 60
    raw_entries: list[tuple[str | None, SeriesKey, dict[datetime, int]]] = []

    if mode == "self-hosted":
        agg = _build_aggregation(group_by_fields=SELF_HOSTED_GROUP_BY_FIELDS)
        pred_filter = _build_filter(SELF_HOSTED_PREDICTION_METRIC, regions)
        tasks = [_query_series(client, proj, pred_filter, interval, agg) for proj in project_list]
        results = await asyncio.gather(*tasks)
        for proj_id, series_list in zip(project_list, results, strict=False):
            for ts in series_list:
                endpoint_id = ts.resource.labels.get("endpoint_id", "self-hosted-endpoint")
                location = ts.resource.labels.get("location", "global")
                pts: dict[datetime, int] = defaultdict(int)
                for pt in ts.points:
                    dt_key = _minute_start(pt.interval.end_time)
                    val = int(pt.value.int64_value or round(pt.value.double_value))
                    pts[dt_key] += val * max(1, tokens_per_prediction)
                raw_entries.append((proj_id, SeriesKey(model=endpoint_id, region=location), pts))
        return (
            filter_and_fold_series(
                raw_entries,
                models=models,
                regions=regions,
                by_region=by_region,
                folder_map=folder_map,
            ),
            total_minutes,
        )

    models_filter = {m.lower() for m in models} if models else None
    has_partner_filter = bool(models_filter and any(is_partner_claude(m) for m in models_filter))
    has_1p_filter = bool(models_filter and any(not is_partner_claude(m) for m in models_filter))
    query_1p = models_filter is None or has_1p_filter
    query_partner = models_filter is None or has_partner_filter

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
        for ts in series_list:
            model_id = ts.resource.labels.get("model_id", "unknown")
            location = ts.resource.labels.get("location", "global")
            if is_partner_query and not is_partner_claude(model_id):
                continue
            token_weight = (
                _partner_token_weight(model_id, str(ts.metric.labels.get("type", "input")))
                if is_partner_query
                else 1.0
            )
            pts = defaultdict(int)
            for pt in ts.points:
                dt_key = _minute_start(pt.interval.end_time)
                val = int(pt.value.int64_value or round(pt.value.double_value))
                pts[dt_key] += round(val * token_weight) if is_partner_query else val
            raw_entries.append((proj_id, SeriesKey(model=model_id, region=location), pts))

    return (
        filter_and_fold_series(
            raw_entries,
            models=models,
            regions=regions,
            by_region=by_region,
            folder_map=folder_map,
        ),
        total_minutes,
    )


async def check_gcp_project_access(projects: Sequence[str]) -> GcpAccessCheckReport:
    """Pre-flight check separating ADC auth, Monitoring API state, and viewer IAM access."""
    from google.api_core.exceptions import GoogleAPICallError, PermissionDenied
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError

    project_list = [
        _validate_identifier("project", p, GCP_PROJECT_ID_PATTERN) for p in projects if p.strip()
    ]
    instructions: list[str] = []
    try:
        import google.auth

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/monitoring.read"])
        account = getattr(creds, "service_account_email", None) or "user-adc"
        client = create_monitoring_client()
    except (DefaultCredentialsError, GoogleAuthError, ImportError) as exc:
        instructions.append(
            "Authenticate with Google Cloud ADC: gcloud auth application-default login"
        )
        return GcpAccessCheckReport(
            authenticated=False,
            account=None,
            ok=False,
            error=str(exc),
            instructions=instructions,
        )

    statuses: dict[str, GcpProjectAccessStatus] = {}
    for proj in project_list:
        try:
            pager = await client.list_metric_descriptors(
                request={
                    "name": f"projects/{proj}",
                    "filter": 'metric.type = starts_with("aiplatform.googleapis.com/")',
                    "page_size": 1,
                },
                timeout=DEFAULT_RPC_TIMEOUT_SECONDS,
            )
            async for _ in pager:
                break
            statuses[proj] = GcpProjectAccessStatus(
                monitoring_api_enabled=True,
                has_viewer_role=True,
                ok=True,
            )
        except PermissionDenied as exc:
            msg = str(getattr(exc, "message", "") or exc)
            if "has not been used" in msg or "is disabled" in msg:
                statuses[proj] = GcpProjectAccessStatus(
                    monitoring_api_enabled=False,
                    has_viewer_role=False,
                    ok=False,
                    error="Cloud Monitoring API is disabled on this project.",
                )
                instructions.append(
                    f"Enable API: gcloud services enable monitoring.googleapis.com --project={proj}"
                )
            else:
                statuses[proj] = GcpProjectAccessStatus(
                    monitoring_api_enabled=True,
                    has_viewer_role=False,
                    ok=False,
                    error="Caller lacks roles/monitoring.viewer on this project.",
                )
                instructions.append(
                    f"Grant viewer access (`roles/monitoring.viewer`) on project '{proj}'."
                )
        except (GoogleAPICallError, GoogleAuthError) as exc:
            statuses[proj] = GcpProjectAccessStatus(
                ok=False,
                error=str(getattr(exc, "message", "") or exc),
            )

    all_ok = all(s.ok for s in statuses.values())
    return GcpAccessCheckReport(
        authenticated=True,
        account=account,
        ok=all_ok,
        projects=statuses,
        instructions=instructions,
    )


async def fetch_inventory_telemetry(
    project: str | Sequence[str],
    days: int = DEFAULT_DAYS,
    regions: Sequence[str] | None = None,
    by_region: bool = True,
) -> dict[SeriesKey, dict[str, int]]:
    """Fetch provisioned GSU ceiling (`dedicated_token_limit`) and spillover/429 counts."""
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
    from google.cloud import monitoring_v3

    raw_projects = [project] if isinstance(project, str) else list(project)
    project_list = [
        _validate_identifier("project", p, GCP_PROJECT_ID_PATTERN) for p in raw_projects
    ]
    if regions:
        for reg in regions:
            _validate_identifier("region", reg, GCP_LOCATION_PATTERN)

    try:
        client = create_monitoring_client()
    except DefaultCredentialsError as exc:
        raise GCPQueryError(
            "GCP credentials not found. Run `gcloud auth application-default login`."
        ) from exc
    except GoogleAuthError as exc:
        raise GCPQueryError(f"GCP authentication failed: {exc}") from exc

    start, end = _whole_day_window(datetime.now(UTC), days)
    interval = monitoring_v3.TimeInterval(start_time=start, end_time=end)

    limit_agg = _build_aggregation(group_by_fields=LIMIT_GROUP_BY_FIELDS, align_max=True)
    inv_agg = _build_aggregation(group_by_fields=INVOCATION_GROUP_BY_FIELDS)
    limit_filter = _build_filter(DEDICATED_LIMIT_METRIC, regions)
    inv_filter = _build_filter(INVOCATION_COUNT_METRIC, regions)

    limit_tasks = [
        _query_series(client, proj, limit_filter, interval, limit_agg) for proj in project_list
    ]
    inv_tasks = [
        _query_series(client, proj, inv_filter, interval, inv_agg) for proj in project_list
    ]
    limit_lists, inv_lists = await asyncio.gather(
        asyncio.gather(*limit_tasks),
        asyncio.gather(*inv_tasks),
    )

    max_tps_by_key: dict[SeriesKey, float] = defaultdict(float)
    for series_list in limit_lists:
        for ts in series_list:
            model_id = normalize_model_id(ts.resource.labels.get("model_id", "unknown"))
            loc = ts.resource.labels.get("location", "global")
            skey = SeriesKey(model=model_id, region=loc if by_region else None)
            peak_pt = max(
                (float(pt.value.int64_value or pt.value.double_value or 0.0) for pt in ts.points),
                default=0.0,
            )
            max_tps_by_key[skey] += peak_pt

    summary: dict[SeriesKey, dict[str, int]] = defaultdict(
        lambda: {"contracted_gsus": 0, "spillover_requests": 0, "http_429_count": 0}
    )
    for skey, limit_tps in max_tps_by_key.items():
        cap_tpm = find_capacity(skey.model)
        if cap_tpm and cap_tpm > 0 and limit_tps > 0.0:
            summary[skey]["contracted_gsus"] = max(1, math.ceil((limit_tps * 60.0) / cap_tpm))

    for series_list in inv_lists:
        for ts in series_list:
            model_id = normalize_model_id(ts.resource.labels.get("model_id", "unknown"))
            loc = ts.resource.labels.get("location", "global")
            skey = SeriesKey(model=model_id, region=loc if by_region else None)
            resp_code = str(ts.metric.labels.get("response_code", "")).strip()
            req_type = str(ts.metric.labels.get("request_type", "")).strip().lower()
            total_calls = sum(
                int(pt.value.int64_value or round(pt.value.double_value)) for pt in ts.points
            )
            if resp_code == "429":
                summary[skey]["http_429_count"] += total_calls
            if req_type in {"shared", "spillover", "on_demand", "on_demand_priority"}:
                summary[skey]["spillover_requests"] += total_calls

    return dict(summary)


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
    ) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
        return await fetch_throughput(
            project=project,
            days=days,
            models=models,
            regions=regions,
            by_region=by_region,
            folder_map=folder_map,
            mode=mode,
        )
