"""Phase 2 vLLM/SGLang Prometheus `/metrics` & OpenTelemetry `gen_ai.usage.*` JSONL parsers."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from capacity.burst import compute_weighted_burndown_tokens
from capacity.models import DictAccessModel


class VllmTelemetrySnapshot(DictAccessModel):
    """Normalized telemetry extracted from Phase 2 vLLM/SGLang Prometheus scrapes or JSONL logs."""

    source_format: str
    model: str
    tpm_series: list[float]
    prompt_tokens_total: int
    cached_tokens_total: int
    completion_tokens_total: int
    thoughts_tokens_total: int
    weighted_burndown_tokens_total: float
    observed_kv_cache_usage_pct: float
    observed_requests_waiting: float
    observed_cache_hit_rate: float
    hourly_consumed_tpm_by_tenant: dict[str, list[float]]


_PROM_METRIC_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:\s+(\d+(?:\.\d+)?))?"
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
_SCRAPE_TS_COMMENT_RE = re.compile(r"^#\s*scrape_timestamp_seconds\s+(\d+(?:\.\d+)?)", re.I)


def parse_inference_server_telemetry(
    raw_content: str,
    default_model: str = "llama-3.3-70b-fp8",
    scrape_window_minutes: float = 1.0,
) -> VllmTelemetrySnapshot:
    """Parse Phase 2 vLLM/SGLang Prometheus `/metrics` text or OTel/vLLM JSONL logs."""
    stripped = raw_content.strip()
    if not stripped:
        return VllmTelemetrySnapshot(
            source_format="empty",
            model=default_model,
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
        return _parse_jsonl_or_json_logs(stripped, default_model=default_model)
    return _parse_prometheus_metrics(
        stripped, default_model=default_model, scrape_window_minutes=scrape_window_minutes
    )


def load_inference_server_file(
    path: Path | str,
    default_model: str = "llama-3.3-70b-fp8",
    scrape_window_minutes: float = 1.0,
) -> VllmTelemetrySnapshot:
    """Load and parse a vLLM/SGLang Prometheus `/metrics` snapshot or JSONL request log file."""
    content = Path(path).read_text(encoding="utf-8")
    return parse_inference_server_telemetry(
        content, default_model=default_model, scrape_window_minutes=scrape_window_minutes
    )


def _normalize_epoch_seconds(ts_raw: float) -> float:
    if ts_raw > 1e14:
        return ts_raw / 1e9
    if ts_raw > 1e11:
        return ts_raw / 1e3
    return ts_raw


def _parse_prometheus_metrics(
    text: str,
    default_model: str = "llama-3.3-70b-fp8",
    scrape_window_minutes: float = 1.0,
) -> VllmTelemetrySnapshot:
    model_name = default_model
    kv_usage_pct = 0.0
    requests_waiting = 0.0
    uptime_seconds = 0.0
    process_start_sec: float | None = None
    last_scrape_sec: float | None = None

    scrapes: list[dict[str, float]] = []
    current_scrape: dict[str, float] = {}
    current_ts: float | None = None

    def _flush_scrape() -> None:
        nonlocal current_scrape
        if current_scrape:
            if current_ts is not None:
                current_scrape["_ts"] = current_ts
            scrapes.append(current_scrape)
            current_scrape = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            ts_m = _SCRAPE_TS_COMMENT_RE.match(line)
            if ts_m:
                _flush_scrape()
                current_ts = _normalize_epoch_seconds(float(ts_m.group(1)))
                last_scrape_sec = current_ts
            continue

        m = _PROM_METRIC_RE.match(line)
        if not m:
            continue
        metric_name, label_str, val_str, ts_str = (
            m.group(1),
            m.group(2) or "",
            m.group(3),
            m.group(4),
        )
        val = float(val_str)
        if not math.isfinite(val):
            continue
        if ts_str is not None:
            sample_ts = _normalize_epoch_seconds(float(ts_str))
            if current_ts is not None and abs(sample_ts - current_ts) > 0.5:
                _flush_scrape()
            current_ts = sample_ts
            last_scrape_sec = sample_ts

        labels = dict(_PROM_LABEL_RE.findall(label_str))
        if "model_name" in labels:
            model_name = labels["model_name"]
        elif "model" in labels:
            model_name = labels["model"]

        norm_name = metric_name.replace(":", "_")
        if norm_name.endswith(("prompt_tokens_total", "input_tokens_total")):
            current_scrape["prompt"] = current_scrape.get("prompt", 0.0) + val
        elif norm_name.endswith(("generation_tokens_total", "output_tokens_total")):
            current_scrape["gen"] = current_scrape.get("gen", 0.0) + val
        elif norm_name.endswith("cached_tokens_total"):
            current_scrape["cached"] = current_scrape.get("cached", 0.0) + val
        elif norm_name.endswith(("gpu_cache_usage_perc", "token_usage")):
            kv_usage_pct = max(kv_usage_pct, val * 100.0 if val <= 1.0 else val)
        elif norm_name.endswith(("num_requests_waiting", "num_queue_reqs")):
            requests_waiting = max(requests_waiting, val)
        elif norm_name.endswith("prefix_cache_queries_total"):
            current_scrape["queries"] = current_scrape.get("queries", 0.0) + val
        elif norm_name.endswith("prefix_cache_hits_total"):
            current_scrape["hits"] = current_scrape.get("hits", 0.0) + val
        elif norm_name == "process_start_time_seconds":
            process_start_sec = val
        elif norm_name.endswith("uptime_seconds"):
            uptime_seconds = max(uptime_seconds, val)

    _flush_scrape()

    if len(scrapes) >= 2:
        prompt_delta_total = 0.0
        gen_delta_total = 0.0
        cached_delta_total = 0.0
        queries_delta_total = 0.0
        hits_delta_total = 0.0
        interval_tpms: list[float] = []

        for idx in range(1, len(scrapes)):
            prev, curr = scrapes[idx - 1], scrapes[idx]
            dt_min = max(1e-3, scrape_window_minutes)
            if "_ts" in prev and "_ts" in curr and curr["_ts"] > prev["_ts"]:
                dt_min = max(1.0 / 60.0, (curr["_ts"] - prev["_ts"]) / 60.0)

            p_d = curr.get("prompt", 0.0) - prev.get("prompt", 0.0)
            if p_d < 0:
                p_d = curr.get("prompt", 0.0)
            g_d = curr.get("gen", 0.0) - prev.get("gen", 0.0)
            if g_d < 0:
                g_d = curr.get("gen", 0.0)
            c_d = curr.get("cached", 0.0) - prev.get("cached", 0.0)
            if c_d < 0:
                c_d = curr.get("cached", 0.0)
            q_d = curr.get("queries", 0.0) - prev.get("queries", 0.0)
            if q_d < 0:
                q_d = curr.get("queries", 0.0)
            h_d = curr.get("hits", 0.0) - prev.get("hits", 0.0)
            if h_d < 0:
                h_d = curr.get("hits", 0.0)

            prompt_delta_total += p_d
            gen_delta_total += g_d
            cached_delta_total += c_d
            queries_delta_total += q_d
            hits_delta_total += h_d

            step_hit = h_d / q_d if q_d > 0 else (c_d / p_d if p_d > 0 else 0.0)
            step_cached = round(c_d if c_d > 0 else p_d * step_hit)
            step_w = compute_weighted_burndown_tokens(
                input_tokens=round(p_d),
                output_tokens=round(g_d),
                thoughts_tokens=0,
                cached_tokens=step_cached,
            )
            interval_tpms.append(round(step_w / dt_min, 2))

        hit_rate = (
            hits_delta_total / queries_delta_total
            if queries_delta_total > 0
            else (cached_delta_total / prompt_delta_total if prompt_delta_total > 0 else 0.0)
        )
        prompt_int = round(prompt_delta_total)
        gen_int = round(gen_delta_total)
        effective_cached = round(
            cached_delta_total if cached_delta_total > 0 else prompt_delta_total * hit_rate
        )
        weighted_total = compute_weighted_burndown_tokens(
            input_tokens=prompt_int,
            output_tokens=gen_int,
            thoughts_tokens=0,
            cached_tokens=effective_cached,
        )
        mean_tpm = round(sum(interval_tpms) / len(interval_tpms), 2) if interval_tpms else 0.0
        return VllmTelemetrySnapshot(
            source_format="prometheus",
            model=model_name,
            tpm_series=interval_tpms or [0.0],
            prompt_tokens_total=prompt_int,
            cached_tokens_total=effective_cached,
            completion_tokens_total=gen_int,
            thoughts_tokens_total=0,
            weighted_burndown_tokens_total=round(weighted_total, 2),
            observed_kv_cache_usage_pct=round(kv_usage_pct, 2),
            observed_requests_waiting=round(requests_waiting, 2),
            observed_cache_hit_rate=round(hit_rate, 4),
            hourly_consumed_tpm_by_tenant={"default": [mean_tpm]},
        )

    single = scrapes[0] if scrapes else {}
    prompt_tokens = single.get("prompt", 0.0)
    generation_tokens = single.get("gen", 0.0)
    cached_tokens = single.get("cached", 0.0)
    prefix_queries = single.get("queries", 0.0)
    prefix_hits = single.get("hits", 0.0)

    hit_rate = (
        prefix_hits / prefix_queries
        if prefix_queries > 0
        else (cached_tokens / prompt_tokens if prompt_tokens > 0 else 0.0)
    )
    effective_cached = round(cached_tokens or (prompt_tokens * hit_rate))
    prompt_int = round(prompt_tokens)
    gen_int = round(generation_tokens)
    weighted_total = compute_weighted_burndown_tokens(
        input_tokens=prompt_int,
        output_tokens=gen_int,
        thoughts_tokens=0,
        cached_tokens=effective_cached,
    )

    eff_window_min = max(1e-3, float(scrape_window_minutes))
    if uptime_seconds > 0.0:
        eff_window_min = max(eff_window_min, uptime_seconds / 60.0)
    elif (
        process_start_sec is not None
        and last_scrape_sec is not None
        and last_scrape_sec > process_start_sec
    ):
        eff_window_min = max(eff_window_min, (last_scrape_sec - process_start_sec) / 60.0)

    est_tpm = round(weighted_total / eff_window_min, 2) if weighted_total > 0 else 0.0

    return VllmTelemetrySnapshot(
        source_format="prometheus",
        model=model_name,
        tpm_series=[est_tpm],
        prompt_tokens_total=prompt_int,
        cached_tokens_total=effective_cached,
        completion_tokens_total=gen_int,
        thoughts_tokens_total=0,
        weighted_burndown_tokens_total=round(weighted_total, 2),
        observed_kv_cache_usage_pct=round(kv_usage_pct, 2),
        observed_requests_waiting=round(requests_waiting, 2),
        observed_cache_hit_rate=round(hit_rate, 4),
        hourly_consumed_tpm_by_tenant={"default": [est_tpm]},
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


def _bucket_utc_timestamp(rec: Mapping[str, object], fallback_idx: int) -> tuple[str, str]:
    attrs = rec.get("attributes")
    attr_map = attrs if isinstance(attrs, Mapping) else {}
    raw_ts = (
        rec.get("minute")
        or rec.get("timestamp")
        or rec.get("start_time")
        or rec.get("timeUnixNano")
        or rec.get("startTimeUnixNano")
        or attr_map.get("timestamp")
    )
    if raw_ts is None:
        h = fallback_idx // 120
        m = (fallback_idx // 2) % 60
        return f"synthetic-h{h:02d}:m{m:02d}", f"synthetic-h{h:02d}"

    if isinstance(raw_ts, (int, float)) or (
        isinstance(raw_ts, str) and re.fullmatch(r"\d+(?:\.\d+)?", raw_ts.strip())
    ):
        sec = _normalize_epoch_seconds(float(raw_ts))
        dt = datetime.fromtimestamp(sec, tz=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M"), dt.strftime("%Y-%m-%dT%H")

    ts_str = str(raw_ts).strip()
    iso_candidate = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
    try:
        dt = datetime.fromisoformat(iso_candidate)
        dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        return dt_utc.strftime("%Y-%m-%dT%H:%M"), dt_utc.strftime("%Y-%m-%dT%H")
    except ValueError:
        return ts_str[:16], ts_str[:13]


def _parse_jsonl_or_json_logs(
    text: str,
    default_model: str = "llama-3.3-70b-fp8",
) -> VllmTelemetrySnapshot:
    records: list[Mapping[str, object]] = []
    if text.startswith("["):
        loaded = json.loads(text)
        if isinstance(loaded, list):
            records = [r for r in loaded if isinstance(r, Mapping)]
    else:
        for line in text.splitlines():
            s = line.strip()
            if s:
                obj = json.loads(s)
                if isinstance(obj, Mapping):
                    records.append(obj)

    model_name = default_model
    prompt_total = 0
    cached_total = 0
    completion_total = 0
    thoughts_total = 0
    weighted_total = 0.0
    kv_max = 0.0
    wait_max = 0.0
    minute_buckets: dict[str, float] = {}
    tenant_hour_tokens: dict[str, dict[str, float]] = {}

    for idx, rec in enumerate(records):
        usage = rec.get("usage")
        usage_map: Mapping[str, object] = usage if isinstance(usage, Mapping) else {}
        prompt_details = usage_map.get("prompt_tokens_details")
        p_details_map: Mapping[str, object] = (
            prompt_details if isinstance(prompt_details, Mapping) else {}
        )
        comp_details = usage_map.get("completion_tokens_details")
        c_details_map: Mapping[str, object] = (
            comp_details if isinstance(comp_details, Mapping) else {}
        )
        attrs = rec.get("attributes")
        attr_map: Mapping[str, object] = attrs if isinstance(attrs, Mapping) else {}
        res_obj = rec.get("resource")
        res_attrs = res_obj.get("attributes") if isinstance(res_obj, Mapping) else None
        res_attr_map: Mapping[str, object] = res_attrs if isinstance(res_attrs, Mapping) else {}

        search_maps = (usage_map, p_details_map, c_details_map, attr_map, res_attr_map, rec)

        mod_candidate = _lookup_first(
            (rec, attr_map, res_attr_map),
            ("model", "gen_ai.response.model", "gen_ai.request.model"),
        )
        if isinstance(mod_candidate, str) and mod_candidate.strip():
            model_name = mod_candidate.strip()

        p_tok = _int_from_maps(
            search_maps,
            (
                "prompt_tokens",
                "input_tokens",
                "gen_ai.usage.input_tokens",
                "gen_ai.usage.prompt_tokens",
            ),
        )
        c_tok = _int_from_maps(
            search_maps,
            (
                "cached_tokens",
                "cached_content_token_count",
                "gen_ai.usage.cached_tokens",
                "cache_read_input_tokens",
            ),
        )
        o_tok = _int_from_maps(
            search_maps,
            (
                "completion_tokens",
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
                "gen_ai.usage.reasoning_tokens",
            ),
        )

        w_tok = compute_weighted_burndown_tokens(
            input_tokens=p_tok,
            output_tokens=o_tok,
            thoughts_tokens=t_tok,
            cached_tokens=c_tok,
        )
        prompt_total += p_tok
        cached_total += c_tok
        completion_total += o_tok
        thoughts_total += t_tok
        weighted_total += w_tok

        kv_val = _float_from_maps(
            (rec, attr_map),
            ("gpu_cache_usage_pct", "kv_cache_usage_pct", "vllm.gpu_cache_usage_perc"),
        )
        wait_val = _float_from_maps(
            (rec, attr_map),
            ("num_requests_waiting", "queue_depth", "vllm.num_requests_waiting"),
        )
        kv_max = max(kv_max, kv_val * 100.0 if 0.0 < kv_val <= 1.0 else kv_val)
        wait_max = max(wait_max, wait_val)

        minute_key, hour_key = _bucket_utc_timestamp(rec, idx)
        minute_buckets[minute_key] = minute_buckets.get(minute_key, 0.0) + w_tok

        tenant_val = _lookup_first(
            (rec, attr_map, res_attr_map),
            ("tenant", "team", "x-business-unit", "gen_ai.tenant", "service.name"),
        )
        tenant = str(tenant_val).strip() if tenant_val is not None else "default"
        t_hours = tenant_hour_tokens.setdefault(tenant or "default", {})
        t_hours[hour_key] = t_hours.get(hour_key, 0.0) + w_tok

    all_hours = sorted({h for th in tenant_hour_tokens.values() for h in th})
    tenant_series: dict[str, list[float]] = {}
    for tenant, th in tenant_hour_tokens.items():
        tenant_series[tenant] = [round(th.get(h, 0.0) / 60.0, 2) for h in all_hours]

    if not tenant_series:
        tenant_series = {"default": [0.0]}

    hit_rate = cached_total / prompt_total if prompt_total > 0 else 0.0
    ordered_minutes = [minute_buckets[k] for k in sorted(minute_buckets)]
    tpm_series = [round(v, 2) for v in ordered_minutes] or [0.0]

    return VllmTelemetrySnapshot(
        source_format="jsonl",
        model=model_name,
        tpm_series=tpm_series,
        prompt_tokens_total=prompt_total,
        cached_tokens_total=cached_total,
        completion_tokens_total=completion_total,
        thoughts_tokens_total=thoughts_total,
        weighted_burndown_tokens_total=round(weighted_total, 2),
        observed_kv_cache_usage_pct=round(kv_max, 2),
        observed_requests_waiting=round(wait_max, 2),
        observed_cache_hit_rate=round(hit_rate, 4),
        hourly_consumed_tpm_by_tenant=tenant_series,
    )
