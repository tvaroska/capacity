"""Multi-scrape vLLM/SGLang Prometheus `/metrics` counter-rate and gauge parser (`ADR-006`)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from capacity.contract.results import VllmTelemetrySnapshot
from capacity.engine.catalog import GEMINI_1P_FLASH_SPEC

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


def parse_prometheus_metrics(
    text: str, default_model: str = "llama-3.3-70b-fp8"
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
        if ts_str is not None:
            sample_ts = normalize_epoch_seconds(float(ts_str))
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
        totals = {"prompt": 0.0, "gen": 0.0, "cached": 0.0, "queries": 0.0, "hits": 0.0}
        interval_tpms: list[float] = []

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
            interval_tpms.append(round(step_w / dt_min, 2))

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
