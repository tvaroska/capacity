"""Multi-scrape vLLM/SGLang Prometheus `/metrics` counter-rate and gauge parser (`ADR-006`)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from capacity.contract.common import MAX_HOURLY_POINTS, MAX_SERIES_POINTS
from capacity.contract.results import VllmTelemetrySnapshot
from capacity.engine.catalog import GEMINI_1P_FLASH_SPEC, normalize_model_id

_PROM_METRIC_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:\s+(\d+(?:\.\d+)?))?"
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
_SCRAPE_TS_COMMENT_RE = re.compile(r"^#\s*scrape_timestamp_seconds\s+(\d+(?:\.\d+)?)", re.I)

# Scrapes without timestamps are read as one minute apart, and a lone scrape as one minute.
_ASSUMED_SCRAPE_WINDOW_MINUTES = 1.0


def normalize_epoch_seconds(ts_raw: float) -> float:
    if ts_raw > 1e14:
        return ts_raw / 1e9
    if ts_raw > 1e11:
        return ts_raw / 1e3
    return ts_raw


def _interval_burndown(prompt_tokens: int, generated_tokens: int, cached_tokens: int) -> float:
    """Weighted burndown for counter totals accumulated over a scrape interval."""
    spec = GEMINI_1P_FLASH_SPEC
    prompt = max(0, prompt_tokens)
    cached = min(max(0, cached_tokens), prompt)
    return (
        (prompt - cached) * spec.standard_input_weight
        + cached * spec.standard_cached_read_weight
        + max(0, generated_tokens) * spec.standard_output_weight
    )


def _counter_delta(curr: Mapping[str, float], prev: Mapping[str, float], key: str) -> float:
    diff = curr.get(key, 0.0) - prev.get(key, 0.0)
    return curr.get(key, 0.0) if diff < 0.0 else diff


def _integrate_bins(
    segments: Sequence[tuple[float, float, float]],
    total_minutes: float,
    bin_minutes: float,
) -> list[float]:
    n_bins = max(1, math.ceil(total_minutes / bin_minutes - 1e-9))
    rates: list[float] = []
    seg_idx = 0
    for b in range(n_bins):
        b_start = b * bin_minutes
        b_end = min(total_minutes, (b + 1) * bin_minutes)
        width = b_end - b_start
        if width <= 0.0:
            break
        while seg_idx < len(segments) and segments[seg_idx][1] <= b_start + 1e-9:
            seg_idx += 1
        tokens = 0.0
        j = seg_idx
        while j < len(segments) and segments[j][0] < b_end - 1e-9:
            s0, s1, rate = segments[j]
            overlap = min(b_end, s1) - max(b_start, s0)
            if overlap > 0.0:
                tokens += overlap * rate
            j += 1
        rates.append(round(tokens / width, 2))
    return rates or [0.0]


def _build_multi_scrape_series(
    intervals: Sequence[tuple[float, float]],
) -> tuple[list[float], list[float]]:
    """Return `(tpm_series, hourly_tpms)` preserving duration weights across unequal intervals."""
    if not intervals:
        return [0.0], [0.0]
    dts = [dt for dt, _ in intervals]
    total_dt_min = sum(dts)
    n_hours = max(1, math.ceil(total_dt_min / 60.0 - 1e-9))
    if n_hours > MAX_HOURLY_POINTS:
        raise ValueError(
            f"scrapes span {n_hours:,} hours; split the file into windows of at most "
            f"{MAX_HOURLY_POINTS:,} hours"
        )

    segments: list[tuple[float, float, float]] = []
    cursor = 0.0
    for dt, step_w in intervals:
        segments.append((cursor, cursor + dt, step_w / dt))
        cursor += dt

    if len(intervals) >= 2 and (max(dts) - min(dts)) > 1e-6:
        n_mins = max(1, math.ceil(total_dt_min - 1e-9))
        if n_mins > MAX_SERIES_POINTS:
            raise ValueError(
                f"scrapes span {n_mins:,} minutes; split the file into windows of at most "
                f"{MAX_SERIES_POINTS:,} minutes"
            )
        tpm_series = _integrate_bins(segments, total_dt_min, 1.0)
    else:
        tpm_series = [round(step_w / dt, 2) for dt, step_w in intervals]

    hourly_tpms = _integrate_bins(segments, total_dt_min, 60.0)
    return tpm_series or [0.0], hourly_tpms


def _minute_floor_utc(epoch_sec: float | None) -> datetime | None:
    if epoch_sec is None:
        return None
    return datetime.fromtimestamp(epoch_sec, tz=UTC).replace(second=0, microsecond=0)


def _match_prometheus_model(
    labels: Mapping[str, str],
    seen_models: set[str],
    model_filter: str | None,
) -> tuple[bool, str | None]:
    raw = labels.get("model_name") or labels.get("model")
    if not raw:
        return True, None
    seen_models.add(normalize_model_id(raw))
    if model_filter is not None and normalize_model_id(raw) != normalize_model_id(model_filter):
        return False, None
    return True, raw


def _validate_prometheus_models(
    seen_models: set[str],
    *,
    model_filter: str | None,
    require_single_model: bool,
) -> None:
    if (
        model_filter is not None
        and seen_models
        and normalize_model_id(model_filter) not in seen_models
    ):
        found = ", ".join(sorted(seen_models))
        raise ValueError(f"model {model_filter!r} not found in metrics (found: {found})")
    if require_single_model and model_filter is None and len(seen_models) > 1:
        found = ", ".join(sorted(seen_models))
        raise ValueError(f"metrics contain multiple models ({found}); pass --model to select one")


def parse_prometheus_metrics(
    text: str,
    default_model: str = "llama-3.3-70b-fp8",
    *,
    model_filter: str | None = None,
    require_single_model: bool = False,
) -> VllmTelemetrySnapshot:
    model_name = model_filter if model_filter is not None else default_model
    kv_usage_pct = 0.0
    requests_waiting = 0.0
    uptime_seconds = 0.0
    process_start_sec: float | None = None
    last_scrape_sec: float | None = None

    scrapes: list[dict[str, float]] = []
    current_scrape: dict[str, float] = {}
    current_ts: float | None = None
    seen_series: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    seen_models: set[str] = set()

    def _flush_scrape() -> None:
        nonlocal current_scrape, current_ts
        if current_scrape:
            if current_ts is not None:
                current_scrape["_ts"] = current_ts
            scrapes.append(current_scrape)
            current_scrape = {}
        current_ts = None
        seen_series.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            ts_m = _SCRAPE_TS_COMMENT_RE.match(line)
            if ts_m:
                _flush_scrape()
                current_ts = normalize_epoch_seconds(float(ts_m.group(1)))
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

        labels = dict(_PROM_LABEL_RE.findall(label_str))
        keep, label_model = _match_prometheus_model(labels, seen_models, model_filter)
        if not keep:
            continue
        if label_model is not None:
            model_name = label_model

        norm_name = metric_name.replace(":", "_")
        series_id = (norm_name, tuple(sorted(labels.items())))

        if ts_str is not None:
            sample_ts = normalize_epoch_seconds(float(ts_str))
            if current_ts is not None and abs(sample_ts - current_ts) > 0.5:
                _flush_scrape()
            current_ts = sample_ts
            last_scrape_sec = sample_ts
        elif series_id in seen_series:
            _flush_scrape()
        seen_series.add(series_id)

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
    _validate_prometheus_models(
        seen_models,
        model_filter=model_filter,
        require_single_model=require_single_model,
    )

    if len(scrapes) >= 2:
        totals = {"prompt": 0.0, "gen": 0.0, "cached": 0.0, "queries": 0.0, "hits": 0.0}
        intervals: list[tuple[float, float]] = []

        for idx in range(1, len(scrapes)):
            prev, curr = scrapes[idx - 1], scrapes[idx]
            dt_min = _ASSUMED_SCRAPE_WINDOW_MINUTES
            if "_ts" in prev and "_ts" in curr and curr["_ts"] > prev["_ts"]:
                dt_min = max(1.0 / 60.0, (curr["_ts"] - prev["_ts"]) / 60.0)

            deltas = {k: _counter_delta(curr, prev, k) for k in totals}
            for k, dv in deltas.items():
                totals[k] += dv

            p_d, g_d, c_d, q_d, h_d = (
                deltas["prompt"],
                deltas["gen"],
                deltas["cached"],
                deltas["queries"],
                deltas["hits"],
            )
            step_hit = h_d / q_d if q_d > 0 else (c_d / p_d if p_d > 0 else 0.0)
            step_cached = round(c_d if c_d > 0 else p_d * step_hit)
            step_w = _interval_burndown(round(p_d), round(g_d), step_cached)
            intervals.append((dt_min, step_w))

        hit_rate = (
            totals["hits"] / totals["queries"]
            if totals["queries"] > 0
            else (totals["cached"] / totals["prompt"] if totals["prompt"] > 0 else 0.0)
        )
        prompt_int = round(totals["prompt"])
        gen_int = round(totals["gen"])
        effective_cached = round(
            totals["cached"] if totals["cached"] > 0 else totals["prompt"] * hit_rate
        )
        weighted_total = _interval_burndown(prompt_int, gen_int, effective_cached)
        tpm_series, hourly_tpms = _build_multi_scrape_series(intervals)
        return VllmTelemetrySnapshot(
            source_format="prometheus",
            model=model_name,
            tpm_series=tpm_series,
            window_start_utc=_minute_floor_utc(scrapes[0].get("_ts")),
            prompt_tokens_total=prompt_int,
            cached_tokens_total=effective_cached,
            completion_tokens_total=gen_int,
            thoughts_tokens_total=0,
            weighted_burndown_tokens_total=round(weighted_total, 2),
            observed_kv_cache_usage_pct=round(kv_usage_pct, 2),
            observed_requests_waiting=round(requests_waiting, 2),
            observed_cache_hit_rate=round(hit_rate, 4),
            hourly_consumed_tpm_by_tenant={"default": hourly_tpms},
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
    weighted_total = _interval_burndown(prompt_int, gen_int, effective_cached)

    eff_window_min = _ASSUMED_SCRAPE_WINDOW_MINUTES
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
        window_start_utc=_minute_floor_utc(last_scrape_sec),
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
