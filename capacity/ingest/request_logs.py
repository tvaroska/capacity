"""OpenTelemetry `gen_ai.usage.*`, Observability 2.0 `inference_request`, and vLLM log parser."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capacity.contract.common import MAX_HOURLY_POINTS, MAX_SERIES_CELLS, MAX_SERIES_POINTS
from capacity.contract.requests import VllmParseRequest
from capacity.contract.results import VllmTelemetrySnapshot
from capacity.engine.burst import compute_weighted_burndown_tokens
from capacity.engine.catalog import (
    SELF_HOSTED_MODEL_IDS,
    ModelCatalog,
    build_model_catalog,
    normalize_model_id,
)
from capacity.ingest.prometheus import normalize_epoch_seconds, parse_prometheus_metrics


def parse_inference_server_telemetry(
    req: VllmParseRequest,
    *,
    catalog: ModelCatalog | None = None,
    model_filter: str | None = None,
    require_single_model: bool = False,
) -> VllmTelemetrySnapshot:
    """Parse vLLM/SGLang Prometheus `/metrics` text or OTel/vLLM JSONL logs."""
    stripped = req.raw_content.strip()
    if not stripped:
        return VllmTelemetrySnapshot(
            source_format="empty",
            model=req.default_model,
            tpm_series=[0.0],
            prompt_tokens_total=0,
            cached_tokens_total=0,
            completion_tokens_total=0,
            thoughts_tokens_total=0,
            weighted_burndown_tokens_total=0.0,
            observed_kv_cache_usage_pct=0.0,
            observed_requests_waiting=0.0,
            observed_cache_hit_rate=0.0,
            hourly_consumed_tpm_by_tenant={"default": [0.0]},
        )

    first_non_comment = next(
        (
            line.strip()
            for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ),
        "",
    )

    if first_non_comment.startswith(("{", "[")):
        return parse_jsonl_or_json_logs(
            stripped,
            default_model=req.default_model,
            catalog=catalog,
            model_filter=model_filter,
            require_single_model=require_single_model,
        )
    return parse_prometheus_metrics(
        stripped,
        default_model=req.default_model,
        model_filter=model_filter,
        require_single_model=require_single_model,
    )


def load_inference_server_file(
    path: Path | str,
    default_model: str = "llama-3.3-70b-fp8",
    *,
    catalog: ModelCatalog | None = None,
    model_filter: str | None = None,
    require_single_model: bool = False,
) -> VllmTelemetrySnapshot:
    """Load and parse a vLLM/SGLang Prometheus `/metrics` snapshot or JSONL request log file."""
    content = Path(path).read_text(encoding="utf-8")
    return parse_inference_server_telemetry(
        VllmParseRequest(raw_content=content, default_model=default_model),
        catalog=catalog,
        model_filter=model_filter,
        require_single_model=require_single_model,
    )


def _lookup_first(maps: Sequence[Mapping[str, object]], keys: Sequence[str]) -> object | None:
    for mp in maps:
        for k in keys:
            val = mp.get(k)
            if val is not None:
                return val
    return None


def _int_from_maps(maps: Sequence[Mapping[str, object]], keys: Sequence[str]) -> int:
    val = _lookup_first(maps, keys)
    if val is None:
        return 0
    return max(0, int(float(str(val))))


def _float_from_maps(maps: Sequence[Mapping[str, object]], keys: Sequence[str]) -> float:
    val = _lookup_first(maps, keys)
    if val is None:
        return 0.0
    fv = float(str(val))
    return fv if math.isfinite(fv) else 0.0


def _status_code_from_maps(maps: Sequence[Mapping[str, object]]) -> int:
    for key in ("http_status_code", "response_code", "http.response.status_code", "status_code"):
        val = _lookup_first(maps, (key,))
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return max(0, int(val))
        raw = str(val).strip().upper()
        if raw.isdigit():
            return int(raw)
        if raw == "RATE_LIMITED":
            return 429
        if raw == "TIMEOUT":
            return 504
        if raw == "ERROR":
            return 500
        if raw in {"OK", "UNSET", "SUCCESS"}:
            return 200
    return 0


def _record_time_utc(rec: Mapping[str, object], index: int) -> datetime:
    """Return a record's UTC time, or fail: a log without times has no rate to size from."""
    attrs = rec.get("attributes")
    attr_map = attrs if isinstance(attrs, Mapping) else {}
    json_payload = rec.get("jsonPayload")
    payload_map = json_payload if isinstance(json_payload, Mapping) else {}
    raw_ts = (
        rec.get("minute")
        or rec.get("timestamp")
        or rec.get("usage_hour")
        or rec.get("receiveTimestamp")
        or rec.get("start_time")
        or rec.get("timeUnixNano")
        or rec.get("startTimeUnixNano")
        or attr_map.get("timestamp")
        or payload_map.get("timestamp")
    )
    if raw_ts is None:
        raise ValueError(
            f"record {index + 1} has no timestamp (expected minute, timestamp, usage_hour, "
            "start_time, timeUnixNano, startTimeUnixNano or attributes.timestamp)"
        )

    try:
        if isinstance(raw_ts, (int, float)) or (
            isinstance(raw_ts, str) and re.fullmatch(r"\d+(?:\.\d+)?", raw_ts.strip())
        ):
            return datetime.fromtimestamp(normalize_epoch_seconds(float(raw_ts)), tz=UTC)
        ts_str = str(raw_ts).strip()
        dt = datetime.fromisoformat(ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str)
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"record {index + 1} has an unreadable timestamp {raw_ts!r}") from None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _hourly_series_by_tenant(
    tenant_hour_tokens: Mapping[str, Mapping[datetime, float]],
) -> dict[str, list[float]]:
    """Per-tenant hourly TPM for every hour from the first record to the last."""
    observed = [hour for hours in tenant_hour_tokens.values() for hour in hours]
    if not observed:
        return {"default": [0.0]}
    first, last = min(observed), max(observed)
    span = int((last - first).total_seconds() // 3600) + 1
    if span > MAX_HOURLY_POINTS:
        raise ValueError(
            f"records span {span:,} hours; split the log into windows of at most "
            f"{MAX_HOURLY_POINTS:,} hours"
        )
    if span * len(tenant_hour_tokens) > MAX_SERIES_CELLS:
        raise ValueError(
            f"{len(tenant_hour_tokens):,} tenants over {span:,} hours exceeds "
            f"{MAX_SERIES_CELLS:,} tenant-hours; split the log"
        )
    axis = [first + timedelta(hours=i) for i in range(span)]
    return {
        tenant: [round(hours.get(hour, 0.0) / 60.0, 2) for hour in axis]
        for tenant, hours in tenant_hour_tokens.items()
    }


def _minute_series_by_key(
    key_minute_tokens: Mapping[str, Mapping[datetime, float]],
) -> dict[str, list[float]]:
    observed = [minute for minutes in key_minute_tokens.values() for minute in minutes]
    if not observed:
        return {}
    first, last = min(observed), max(observed)
    span = int((last - first).total_seconds() // 60) + 1
    if span > MAX_SERIES_POINTS:
        raise ValueError(
            f"records span {span:,} minutes; split the log into windows of at most "
            f"{MAX_SERIES_POINTS:,} minutes"
        )
    if span * len(key_minute_tokens) > MAX_SERIES_CELLS:
        raise ValueError(
            f"{len(key_minute_tokens):,} model-region series over {span:,} minutes exceeds "
            f"{MAX_SERIES_CELLS:,} cells; split the log"
        )
    axis = [first + timedelta(minutes=i) for i in range(span)]
    return {
        key: [round(minutes.get(minute, 0.0), 2) for minute in axis]
        for key, minutes in key_minute_tokens.items()
    }


def _zero_filled_minute_series(
    minute_buckets: Mapping[datetime, float],
) -> tuple[list[float], datetime | None]:
    if not minute_buckets:
        return [0.0], None
    first, last = min(minute_buckets), max(minute_buckets)
    span = int((last - first).total_seconds() // 60) + 1
    if span > MAX_SERIES_POINTS:
        raise ValueError(
            f"records span {span:,} minutes; split the log into windows of at most "
            f"{MAX_SERIES_POINTS:,} minutes"
        )
    return (
        [round(minute_buckets.get(first + timedelta(minutes=i), 0.0), 2) for i in range(span)],
        first,
    )


def _as_map(obj: object) -> Mapping[str, object]:
    return obj if isinstance(obj, Mapping) else {}


def _load_json_records(text: str) -> list[Mapping[str, object]]:
    if text.startswith("["):
        loaded = json.loads(text)
        return [r for r in loaded if isinstance(r, Mapping)] if isinstance(loaded, list) else []
    records: list[Mapping[str, object]] = []
    for line in text.splitlines():
        s = line.strip()
        if s:
            obj = json.loads(s)
            if isinstance(obj, Mapping):
                records.append(obj)
    return records


def _record_raw_model(rec: Mapping[str, object], default_model: str) -> str:
    payload_map = _as_map(rec.get("jsonPayload"))
    attr_map = _as_map(rec.get("attributes"))
    res_map = _as_map(rec.get("resource"))
    mod_candidate = _lookup_first(
        (
            rec,
            payload_map,
            attr_map,
            _as_map(res_map.get("attributes")),
            _as_map(res_map.get("labels")),
        ),
        (
            "model",
            "model_name",
            "model_id",
            "model_user_id",
            "publisher_model_id",
            "gen_ai.response.model",
            "gen_ai.request.model",
        ),
    )
    if isinstance(mod_candidate, str) and mod_candidate.strip():
        return mod_candidate.strip()
    return default_model


def _filter_records_by_model(
    records: Sequence[Mapping[str, object]],
    default_model: str,
    *,
    model_filter: str | None,
    require_single_model: bool,
) -> list[tuple[int, Mapping[str, object], str]]:
    tagged: list[tuple[int, Mapping[str, object], str]] = []
    running_default = default_model
    for idx, rec in enumerate(records):
        resolved = _record_raw_model(rec, running_default)
        running_default = resolved
        tagged.append((idx, rec, resolved))
    if not tagged:
        return []
    observed_norm = sorted({normalize_model_id(m) for _, _, m in tagged})
    if model_filter is not None:
        target = normalize_model_id(model_filter)
        matched = [item for item in tagged if normalize_model_id(item[2]) == target]
        if not matched:
            raise ValueError(
                f"model {model_filter!r} not found in log (found: {', '.join(observed_norm)})"
            )
        return matched
    if require_single_model and len(observed_norm) > 1:
        raise ValueError(
            f"log contains multiple models ({', '.join(observed_norm)}); pass --model to select one"
        )
    return tagged


def parse_jsonl_or_json_logs(
    text: str,
    default_model: str = "llama-3.3-70b-fp8",
    *,
    catalog: ModelCatalog | None = None,
    model_filter: str | None = None,
    require_single_model: bool = False,
) -> VllmTelemetrySnapshot:
    raw_records = _load_json_records(text)
    selected = _filter_records_by_model(
        raw_records,
        default_model,
        model_filter=model_filter,
        require_single_model=require_single_model,
    )
    cat = catalog if catalog is not None else build_model_catalog()

    model_name = model_filter if model_filter is not None else default_model
    prompt_total = 0
    cached_total = 0
    cache_write_5m_total = 0
    cache_write_1h_total = 0
    completion_total = 0
    thoughts_total = 0
    weighted_total = 0.0
    kv_max = 0.0
    wait_max = 0.0
    spillover_total = 0
    http_429_total = 0
    minute_buckets: dict[datetime, float] = {}
    sec1_buckets: dict[int, float] = {}
    sec10_buckets: dict[int, float] = {}
    sec30_buckets: dict[int, float] = {}
    tenant_hour_tokens: dict[str, dict[datetime, float]] = {}
    model_region_minutes: dict[str, dict[datetime, float]] = {}
    reqs_by_mr: dict[str, int] = {}
    spill_by_mr: dict[str, int] = {}
    err429_by_mr: dict[str, int] = {}

    for idx, rec, rec_model in selected:
        usage_map = _as_map(rec.get("usage"))
        payload_map = _as_map(rec.get("jsonPayload"))
        p_usage_map = _as_map(payload_map.get("usage_metadata") or payload_map.get("usage"))
        req_label_map = _as_map(payload_map.get("request_labels") or payload_map.get("labels"))
        top_label_map = _as_map(rec.get("labels"))
        p_details_map = _as_map(usage_map.get("prompt_tokens_details"))
        c_details_map = _as_map(usage_map.get("completion_tokens_details"))
        cc_map = _as_map(usage_map.get("cache_creation"))
        attr_map = _as_map(rec.get("attributes"))
        res_map = _as_map(rec.get("resource"))
        res_attr_map = _as_map(res_map.get("attributes"))
        res_label_map = _as_map(res_map.get("labels"))

        search_maps = (
            usage_map,
            p_usage_map,
            p_details_map,
            c_details_map,
            cc_map,
            payload_map,
            attr_map,
            res_attr_map,
            res_label_map,
            rec,
        )
        model_name = rec_model

        loc_candidate = _lookup_first(
            (rec, payload_map, attr_map, res_attr_map, res_label_map),
            ("location", "region", "request_cloud_region", "cloud.region"),
        )
        rec_region = (
            str(loc_candidate).strip().removeprefix("prod-")
            if loc_candidate is not None and str(loc_candidate).strip()
            else "global"
        )
        mr_key = f"{normalize_model_id(rec_model)}@{rec_region}"

        c_tok = _int_from_maps(
            search_maps,
            (
                "cached_tokens",
                "cached_input_tokens",
                "cached_content_token_count",
                "gen_ai.usage.cached_tokens",
                "cache_read_input_tokens",
                "gen_ai.usage.cache_read.input_tokens",
            ),
        )
        w1_tok = _int_from_maps(search_maps, ("ephemeral_1h_input_tokens", "cache_write_1h_tokens"))
        w5_explicit = _lookup_first(
            search_maps, ("ephemeral_5m_input_tokens", "cache_write_5m_tokens")
        )
        if w5_explicit is not None:
            w5_tok = max(0, int(float(str(w5_explicit))))
        else:
            cc_total = _int_from_maps(
                search_maps,
                ("cache_creation_input_tokens", "gen_ai.usage.cache_creation.input_tokens"),
            )
            w5_tok = max(0, cc_total - w1_tok)

        raw_prompt = _lookup_first(
            search_maps,
            ("prompt_tokens", "prompt_token_count", "gen_ai.usage.prompt_tokens"),
        )
        raw_input = _lookup_first(search_maps, ("input_tokens", "gen_ai.usage.input_tokens"))
        if raw_prompt is not None:
            p_tok = max(max(0, int(float(str(raw_prompt)))), c_tok + w5_tok + w1_tok)
        elif raw_input is not None:
            base_in = max(0, int(float(str(raw_input))))
            has_anthropic_cache = bool(cc_map) or (
                _lookup_first(
                    search_maps,
                    (
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "gen_ai.usage.cache_read.input_tokens",
                        "gen_ai.usage.cache_creation.input_tokens",
                    ),
                )
                is not None
            )
            p_tok = base_in + c_tok + w5_tok + w1_tok if has_anthropic_cache else base_in
        else:
            p_tok = c_tok + w5_tok + w1_tok

        o_tok = _int_from_maps(
            search_maps,
            (
                "completion_tokens",
                "candidates_token_count",
                "output_tokens",
                "gen_ai.usage.output_tokens",
                "gen_ai.usage.completion_tokens",
            ),
        )
        t_tok = _int_from_maps(
            search_maps,
            (
                "reasoning_tokens",
                "thoughts_tokens",
                "thinking_tokens",
                "thoughts_token_count",
                "gen_ai.usage.reasoning_tokens",
            ),
        )

        # Known catalog/self-hosted models use their family weights; arbitrary external trace IDs
        # default to standard 1P weights without guessing capacities.
        is_known = (
            cat.get(rec_model) is not None or normalize_model_id(rec_model) in SELF_HOSTED_MODEL_IDS
        )
        known_model = rec_model if is_known else None
        preweighted = _lookup_first(
            (rec, payload_map),
            ("precomputed_weighted_burndown_tokens", "weighted_burndown_tokens"),
        )
        if preweighted is not None:
            pw_val = float(str(preweighted))
            w_tok = pw_val if math.isfinite(pw_val) and pw_val >= 0.0 else 0.0
        else:
            w_tok = compute_weighted_burndown_tokens(
                input_tokens=p_tok,
                output_tokens=o_tok,
                thoughts_tokens=t_tok,
                cached_tokens=c_tok,
                model=known_model,
                cache_write_5m_tokens=w5_tok,
                cache_write_1h_tokens=w1_tok,
                catalog=cat,
            )
        prompt_total += p_tok
        cached_total += c_tok
        cache_write_5m_total += w5_tok
        cache_write_1h_total += w1_tok
        completion_total += o_tok
        thoughts_total += t_tok
        weighted_total += w_tok

        status_code = _status_code_from_maps((rec, payload_map, attr_map))
        traffic_type_raw = _lookup_first(
            (rec, payload_map, p_usage_map, attr_map, top_label_map),
            ("traffic_type", "request_type", "gen_ai.vertex.traffic_type"),
        )
        tt_upper = str(traffic_type_raw).strip().upper() if traffic_type_raw is not None else ""
        reqs_by_mr[mr_key] = reqs_by_mr.get(mr_key, 0) + 1
        if status_code == 429:
            http_429_total += 1
            err429_by_mr[mr_key] = err429_by_mr.get(mr_key, 0) + 1
        if tt_upper in {
            "ON_DEMAND",
            "ON_DEMAND_PRIORITY",
            "ON_DEMAND_FLEX",
            "SHARED",
            "SPILLOVER",
        }:
            spillover_total += 1
            spill_by_mr[mr_key] = spill_by_mr.get(mr_key, 0) + 1

        kv_val = _float_from_maps(
            (rec, payload_map, attr_map),
            ("gpu_cache_usage_pct", "kv_cache_usage_pct", "vllm.gpu_cache_usage_perc"),
        )
        wait_val = _float_from_maps(
            (rec, payload_map, attr_map),
            ("num_requests_waiting", "queue_depth", "vllm.num_requests_waiting"),
        )
        kv_max = max(kv_max, kv_val * 100.0 if 0.0 < kv_val <= 1.0 else kv_val)
        wait_max = max(wait_max, wait_val)

        ts_utc = _record_time_utc(rec, idx)
        epoch_sec = int(ts_utc.timestamp())
        sec1_buckets[epoch_sec] = sec1_buckets.get(epoch_sec, 0.0) + w_tok
        sec10_buckets[epoch_sec // 10] = sec10_buckets.get(epoch_sec // 10, 0.0) + w_tok
        sec30_buckets[epoch_sec // 30] = sec30_buckets.get(epoch_sec // 30, 0.0) + w_tok

        minute_key = ts_utc.replace(second=0, microsecond=0)
        hour_key = minute_key.replace(minute=0)
        minute_buckets[minute_key] = minute_buckets.get(minute_key, 0.0) + w_tok
        mr_minutes = model_region_minutes.setdefault(mr_key, {})
        mr_minutes[minute_key] = mr_minutes.get(minute_key, 0.0) + w_tok

        tenant_val = _lookup_first(
            (rec, req_label_map, top_label_map, payload_map, attr_map, res_attr_map),
            (
                "tenant",
                "apphub_application_id",
                "team",
                "x-business-unit",
                "business_unit",
                "app_id",
                "cost_center",
                "workload",
                "gen_ai.tenant",
                "service.name",
            ),
        )
        tenant = str(tenant_val).strip() if tenant_val is not None else "default"
        t_hours = tenant_hour_tokens.setdefault(tenant or "default", {})
        t_hours[hour_key] = t_hours.get(hour_key, 0.0) + w_tok

    tenant_series = _hourly_series_by_tenant(tenant_hour_tokens)
    mr_series = _minute_series_by_key(model_region_minutes)

    hit_rate = cached_total / prompt_total if prompt_total > 0 else 0.0
    tpm_series, window_start_utc = _zero_filled_minute_series(minute_buckets)
    peak_1s = max(sec1_buckets.values(), default=0.0)
    peak_10s = max(sec10_buckets.values(), default=0.0) / 10.0
    peak_30s = max(sec30_buckets.values(), default=0.0) / 30.0

    return VllmTelemetrySnapshot(
        source_format="jsonl",
        model=model_name,
        tpm_series=tpm_series,
        window_start_utc=window_start_utc,
        prompt_tokens_total=prompt_total,
        cached_tokens_total=cached_total,
        cache_write_5m_tokens_total=cache_write_5m_total,
        cache_write_1h_tokens_total=cache_write_1h_total,
        completion_tokens_total=completion_total,
        thoughts_tokens_total=thoughts_total,
        weighted_burndown_tokens_total=round(weighted_total, 2),
        observed_kv_cache_usage_pct=round(kv_max, 2),
        observed_requests_waiting=round(wait_max, 2),
        observed_cache_hit_rate=round(hit_rate, 4),
        hourly_consumed_tpm_by_tenant=tenant_series,
        total_requests=len(selected),
        spillover_requests=spillover_total,
        http_429_count=http_429_total,
        peak_1s_burndown_tps=round(peak_1s, 2),
        peak_10s_burndown_tps=round(peak_10s, 2),
        peak_30s_burndown_tps=round(peak_30s, 2),
        minute_tpm_by_model_region=mr_series,
        requests_by_model_region=reqs_by_mr,
        spillover_by_model_region=spill_by_mr,
        http_429_by_model_region=err429_by_mr,
    )


def parse_bq_eval_capacity_export(payload: str | Mapping[str, object]) -> dict[str, object]:
    """Validate and normalize a versioned `bq-eval capacity-export` payload (`v1.0` | `v1.1`)."""
    raw_obj = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(raw_obj, Mapping):
        raise ValueError("Expected JSON object from bq-eval capacity-export.")
    version = str(raw_obj.get("schema_version", "1.0")).strip()
    if version not in {"1.0", "1.1"}:
        raise ValueError(f"Unsupported bq-eval capacity-export schema_version {version!r}")
    tp_cb = raw_obj.get("three_pool_chargeback")
    res_sched = raw_obj.get("residual_schedule_request")
    if not isinstance(tp_cb, Mapping):
        raise ValueError("bq-eval capacity-export missing object field 'three_pool_chargeback'")
    if not isinstance(res_sched, Mapping):
        raise ValueError("bq-eval capacity-export missing object field 'residual_schedule_request'")
    raw_hours = raw_obj.get("hours")
    hours = list(raw_hours) if isinstance(raw_hours, list) else []

    def _list_of_maps(field: str) -> list[dict[str, object]]:
        val = raw_obj.get(field, [])
        if not isinstance(val, list):
            raise ValueError(f"Expected list for {field!r} in bq-eval capacity-export.")
        return [dict(item) for item in val if isinstance(item, Mapping)]

    return {
        "schema_version": version,
        "corpus_id": str(raw_obj.get("corpus_id", "")),
        "model": str(raw_obj.get("model", "")),
        "hours": hours,
        "three_pool_chargeback": dict(tp_cb),
        "residual_schedule_request": dict(res_sched),
        "burst_metrics_1m": _list_of_maps("burst_metrics_1m"),
        "app_risk_governance": _list_of_maps("app_risk_governance"),
        "lob_model_lifecycle": _list_of_maps("lob_model_lifecycle"),
    }
