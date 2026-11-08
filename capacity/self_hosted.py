"""Self-hosted model capacity planning, GPU/TPU quota & KV-cache physics, and vLLM/SGLang ingestion.

Covers two operational stages:
1. Phase 1 (Vertex AI Online Prediction `DedicatedResources` + sidecar vLLM/SGLang Prometheus):
   - Upfront regional GPU/TPU quota locking (`max_replica_count * accelerators_per_replica`).
   - Continuous-batching Duty-Cycle Illusion (`accelerator/duty_cycle` appearing `<60%` while
     `prometheus.googleapis.com/vertex_vllm_gpu_cache_usage_perc` hits `>90%` and preempts).
   - Endpoint round-robin $1/N$ KV-cache dilution across `DeployedModel` replicas.
   - Newsvendor replica commitment sizing (`min_replica_count` reserved CUD nodes vs. elastic
     `max_replica_count` on-demand/DWS `flex_start` burst + non-prod `ScaleToZeroSpec` savings).
2. Phase 2 Seam (Direct vLLM / SGLang / TGI Prometheus `/metrics` & JSONL request log ingestion):
   - Parses exported Prometheus text snapshots (`vllm:*`, `sglang:*`) and JSONL request logs into
     normalized throughput series and tenant token breakdowns.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field

from capacity.burst import compute_weighted_burndown_tokens, evaluate_prefix_cache_dilution
from capacity.core import percentile
from capacity.finops import MINUTES_PER_MONTH
from capacity.models import DictAccessModel, PrefixCacheAudit

HOURS_PER_MONTH = 720.0


class SelfHostedProfile(DictAccessModel):
    """Hardware and serving throughput profile for a self-hosted open-weight model replica."""

    profile_id: str
    model_family: str
    machine_type: str
    accelerator_type: str
    accelerators_per_replica: int = Field(ge=1)
    tps_per_replica: float = Field(
        gt=0.0, description="Sustained weighted burndown tokens/sec per replica"
    )
    max_kv_cache_tokens: int = Field(
        gt=0, description="Usable KV-cache token capacity per replica in HBM"
    )
    reserved_replica_monthly_usd: float = Field(
        gt=0.0, description="Monthly cost per 1Y CUD / reserved replica"
    )
    on_demand_replica_hourly_usd: float = Field(
        gt=0.0, description="Hourly cost per elastic burst replica"
    )
    serverless_burst_per_1m_tokens_usd: float = Field(
        default=0.60,
        ge=0.0,
        description="Optional serverless Open MaaS burst spillover rate per 1M tokens",
    )

    @property
    def tpm_per_replica(self) -> int:
        return round(self.tps_per_replica * 60.0)


DEFAULT_SELF_HOSTED_PROFILES: dict[str, SelfHostedProfile] = {
    "llama-3.3-70b-fp8": SelfHostedProfile(
        profile_id="llama-3.3-70b-fp8",
        model_family="Llama 3.3 70B (FP8, vLLM)",
        machine_type="a3-highgpu-8g",
        accelerator_type="NVIDIA_H100_80GB",
        accelerators_per_replica=8,
        tps_per_replica=3_200.0,
        max_kv_cache_tokens=524_288,
        reserved_replica_monthly_usd=18_500.0,
        on_demand_replica_hourly_usd=38.80,
        serverless_burst_per_1m_tokens_usd=0.72,
    ),
    "qwen-2.5-72b-fp8": SelfHostedProfile(
        profile_id="qwen-2.5-72b-fp8",
        model_family="Qwen 2.5 72B (FP8, SGLang)",
        machine_type="a3-highgpu-8g",
        accelerator_type="NVIDIA_H100_80GB",
        accelerators_per_replica=8,
        tps_per_replica=3_100.0,
        max_kv_cache_tokens=524_288,
        reserved_replica_monthly_usd=18_500.0,
        on_demand_replica_hourly_usd=38.80,
        serverless_burst_per_1m_tokens_usd=0.72,
    ),
    "gemma-3-27b-bf16": SelfHostedProfile(
        profile_id="gemma-3-27b-bf16",
        model_family="Gemma 3 27B (BF16, vLLM)",
        machine_type="g2-standard-48",
        accelerator_type="NVIDIA_L4",
        accelerators_per_replica=4,
        tps_per_replica=1_450.0,
        max_kv_cache_tokens=196_608,
        reserved_replica_monthly_usd=2_450.0,
        on_demand_replica_hourly_usd=4.95,
        serverless_burst_per_1m_tokens_usd=0.35,
    ),
    "deepseek-r1-distill-32b": SelfHostedProfile(
        profile_id="deepseek-r1-distill-32b",
        model_family="DeepSeek R1 Distill 32B (vLLM)",
        machine_type="a2-highgpu-4g",
        accelerator_type="NVIDIA_A100_40GB",
        accelerators_per_replica=4,
        tps_per_replica=1_600.0,
        max_kv_cache_tokens=262_144,
        reserved_replica_monthly_usd=6_800.0,
        on_demand_replica_hourly_usd=14.20,
        serverless_burst_per_1m_tokens_usd=0.55,
    ),
}


class SelfHostedEndpointAudit(DictAccessModel):
    """Comprehensive capacity, KV-cache, quota-lock, and Newsvendor audit for GPU replicas."""

    profile_id: str
    machine_type: str
    accelerator_type: str
    accelerators_per_replica: int
    tps_per_replica: float
    tpm_per_replica: int
    min_replica_count: int
    max_replica_count: int
    locked_regional_accelerators: int
    active_baseline_accelerators: int
    stranded_quota_accelerators: int
    observed_accelerator_duty_cycle_pct: float
    observed_vllm_kv_cache_usage_pct: float
    observed_vllm_requests_waiting: float
    duty_cycle_illusion_detected: bool
    kv_preemption_risk: bool
    recommended_autoscaler_metric: str
    optimal_reserved_replicas: int
    p99_peak_replicas: int
    breakeven_replica_utilization_pct: float
    reserved_base_monthly_cost_usd: float
    elastic_burst_monthly_cost_usd: float
    total_hybrid_monthly_cost_usd: float
    all_on_demand_monthly_cost_usd: float
    p99_static_reserved_monthly_cost_usd: float
    scale_to_zero_nonprod_savings_usd: float
    prefix_cache: PrefixCacheAudit


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


def resolve_self_hosted_profile(model_or_profile: str) -> SelfHostedProfile:
    """Resolve a self-hosted model identifier to its hardware and throughput profile."""
    cleaned = model_or_profile.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if cleaned in DEFAULT_SELF_HOSTED_PROFILES:
        return DEFAULT_SELF_HOSTED_PROFILES[cleaned]
    for key, prof in DEFAULT_SELF_HOSTED_PROFILES.items():
        if cleaned.startswith(key) or key.startswith(cleaned):
            return prof
    return DEFAULT_SELF_HOSTED_PROFILES["llama-3.3-70b-fp8"]


def evaluate_self_hosted_endpoint(
    tpm_series: Sequence[int | float],
    profile: SelfHostedProfile | str = "llama-3.3-70b-fp8",
    min_replica_count: int = 2,
    max_replica_count: int = 6,
    observed_accelerator_duty_cycle_pct: float = 52.0,
    observed_vllm_kv_cache_usage_pct: float = 91.5,
    observed_vllm_requests_waiting: float = 14.0,
    nonprod_idle_hours_per_week: float = 108.0,
    total_input_tokens: int = 20_000,
    shared_prefix_ratio: float = 0.80,
    baseline_cache_hit_rate: float = 0.80,
) -> SelfHostedEndpointAudit:
    """Audit a self-hosted GPU/TPU endpoint for quota locks, KV saturation, and Newsvendor."""
    prof = (
        profile
        if isinstance(profile, SelfHostedProfile)
        else resolve_self_hosted_profile(profile)
    )
    min_reps = max(0, int(min_replica_count))
    max_reps = max(max(1, min_reps), int(max_replica_count))

    locked_accels = max_reps * prof.accelerators_per_replica
    baseline_accels = min_reps * prof.accelerators_per_replica
    stranded_accels = max(0, locked_accels - baseline_accels)

    kv_preemption_risk = (
        observed_vllm_kv_cache_usage_pct >= 85.0 or observed_vllm_requests_waiting >= 5.0
    )
    duty_cycle_illusion = (
        observed_accelerator_duty_cycle_pct < 60.0 and kv_preemption_risk
    )

    cleaned = sorted(max(0.0, float(x)) for x in tpm_series) if tpm_series else [0.0]
    rep_tpm = float(prof.tpm_per_replica)
    p99_tpm = float(percentile(cleaned, 99))
    p99_replicas = max(1, math.ceil(p99_tpm / rep_tpm)) if p99_tpm > 0 else 1

    on_demand_monthly_equiv = prof.on_demand_replica_hourly_usd * HOURS_PER_MONTH
    breakeven_util = min(
        1.0, prof.reserved_replica_monthly_usd / on_demand_monthly_equiv
    )

    n_points = len(cleaned)

    def _monthly_cost(reserved_reps: int) -> tuple[float, float, float]:
        base_cost = reserved_reps * prof.reserved_replica_monthly_usd
        base_cap_tpm = reserved_reps * rep_tpm
        mean_burst_replicas = (
            sum(
                math.ceil(max(0.0, x - base_cap_tpm) / rep_tpm)
                for x in cleaned
            )
            / n_points
        )
        burst_cost = mean_burst_replicas * on_demand_monthly_equiv
        return base_cost + burst_cost, base_cost, burst_cost

    best_reps = 0
    best_total, best_base, best_burst = _monthly_cost(0)
    for r in range(p99_replicas + 1):
        tot, b_cost, s_cost = _monthly_cost(r)
        if tot < best_total:
            best_reps = r
            best_total, best_base, best_burst = tot, b_cost, s_cost

    all_on_demand_cost = (
        sum(math.ceil(x / rep_tpm) for x in cleaned) / n_points
    ) * on_demand_monthly_equiv
    p99_static_reserved_cost = p99_replicas * prof.reserved_replica_monthly_usd

    idle_fraction = min(1.0, max(0.0, nonprod_idle_hours_per_week / 168.0))
    scale_to_zero_savings = (
        max(1, min_reps) * on_demand_monthly_equiv * idle_fraction
    )

    prefix_audit = evaluate_prefix_cache_dilution(
        total_input_tokens=total_input_tokens,
        shared_prefix_ratio=shared_prefix_ratio,
        baseline_cache_hit_rate=baseline_cache_hit_rate,
        num_round_robin_endpoints=max(1, max_reps),
        tps_per_gsu=prof.tps_per_replica,
    )

    return SelfHostedEndpointAudit(
        profile_id=prof.profile_id,
        machine_type=prof.machine_type,
        accelerator_type=prof.accelerator_type,
        accelerators_per_replica=prof.accelerators_per_replica,
        tps_per_replica=prof.tps_per_replica,
        tpm_per_replica=prof.tpm_per_replica,
        min_replica_count=min_reps,
        max_replica_count=max_reps,
        locked_regional_accelerators=locked_accels,
        active_baseline_accelerators=baseline_accels,
        stranded_quota_accelerators=stranded_accels,
        observed_accelerator_duty_cycle_pct=round(observed_accelerator_duty_cycle_pct, 2),
        observed_vllm_kv_cache_usage_pct=round(observed_vllm_kv_cache_usage_pct, 2),
        observed_vllm_requests_waiting=round(observed_vllm_requests_waiting, 2),
        duty_cycle_illusion_detected=duty_cycle_illusion,
        kv_preemption_risk=kv_preemption_risk,
        recommended_autoscaler_metric=(
            "prometheus.googleapis.com/vertex_vllm_gpu_cache_usage_perc (target <= 80%) "
            "+ vertex_vllm_num_requests_waiting"
        ),
        optimal_reserved_replicas=best_reps,
        p99_peak_replicas=p99_replicas,
        breakeven_replica_utilization_pct=round(breakeven_util * 100.0, 2),
        reserved_base_monthly_cost_usd=round(best_base, 2),
        elastic_burst_monthly_cost_usd=round(best_burst, 2),
        total_hybrid_monthly_cost_usd=round(best_total, 2),
        all_on_demand_monthly_cost_usd=round(all_on_demand_cost, 2),
        p99_static_reserved_monthly_cost_usd=round(p99_static_reserved_cost, 2),
        scale_to_zero_nonprod_savings_usd=round(scale_to_zero_savings, 2),
        prefix_cache=prefix_audit,
    )


_PROM_METRIC_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


def parse_inference_server_telemetry(
    raw_content: str,
    default_model: str = "llama-3.3-70b-fp8",
) -> VllmTelemetrySnapshot:
    """Parse Phase 2 vLLM/SGLang Prometheus `/metrics` text or JSONL logs into normalized series."""
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
    return _parse_prometheus_metrics(stripped, default_model=default_model)


def load_inference_server_file(
    path: Path | str,
    default_model: str = "llama-3.3-70b-fp8",
) -> VllmTelemetrySnapshot:
    """Load and parse a vLLM/SGLang Prometheus `/metrics` snapshot or JSONL request log file."""
    content = Path(path).read_text(encoding="utf-8")
    return parse_inference_server_telemetry(content, default_model=default_model)


def _parse_prometheus_metrics(
    text: str,
    default_model: str = "llama-3.3-70b-fp8",
) -> VllmTelemetrySnapshot:
    model_name = default_model
    prompt_tokens = 0.0
    generation_tokens = 0.0
    cached_tokens = 0.0
    kv_usage_pct = 0.0
    requests_waiting = 0.0
    prefix_queries = 0.0
    prefix_hits = 0.0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_METRIC_RE.match(line)
        if not m:
            continue
        metric_name, label_str, val_str = m.group(1), m.group(2) or "", m.group(3)
        val = float(val_str)
        labels = dict(_PROM_LABEL_RE.findall(label_str))
        if "model_name" in labels:
            model_name = labels["model_name"]
        elif "model" in labels:
            model_name = labels["model"]

        norm_name = metric_name.replace(":", "_")
        if norm_name.endswith(("prompt_tokens_total", "input_tokens_total")):
            prompt_tokens += val
        elif norm_name.endswith(("generation_tokens_total", "output_tokens_total")):
            generation_tokens += val
        elif norm_name.endswith("cached_tokens_total"):
            cached_tokens += val
        elif norm_name.endswith(("gpu_cache_usage_perc", "token_usage")):
            kv_usage_pct = max(kv_usage_pct, val * 100.0 if val <= 1.0 else val)
        elif norm_name.endswith(("num_requests_waiting", "num_queue_reqs")):
            requests_waiting = max(requests_waiting, val)
        elif norm_name.endswith("prefix_cache_queries_total"):
            prefix_queries += val
        elif norm_name.endswith("prefix_cache_hits_total"):
            prefix_hits += val

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
    est_tpm = max(1_000.0, weighted_total)

    return VllmTelemetrySnapshot(
        source_format="prometheus",
        model=model_name,
        tpm_series=[est_tpm * 0.6, est_tpm * 0.85, est_tpm, est_tpm * 1.15],
        prompt_tokens_total=prompt_int,
        cached_tokens_total=effective_cached,
        completion_tokens_total=gen_int,
        thoughts_tokens_total=0,
        weighted_burndown_tokens_total=round(weighted_total, 2),
        observed_kv_cache_usage_pct=round(kv_usage_pct, 2),
        observed_requests_waiting=round(requests_waiting, 2),
        observed_cache_hit_rate=round(hit_rate, 4),
        hourly_consumed_tpm_by_tenant={"default": [round(est_tpm, 2)]},
    )


def _int_metric(usage_map: Mapping[str, object], key1: str, key2: str) -> int:
    raw = usage_map.get(key1, usage_map.get(key2, 0)) or 0
    return int(float(str(raw)))


def _float_metric(rec: Mapping[str, object], key1: str, key2: str) -> float:
    raw = rec.get(key1, rec.get(key2, 0.0)) or 0.0
    return float(str(raw))


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
    tenant_buckets: dict[str, dict[str, float]] = {}

    for idx, rec in enumerate(records):
        if isinstance(rec.get("model"), str):
            model_name = str(rec["model"])
        usage = rec.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else rec

        p_tok = _int_metric(usage_map, "prompt_tokens", "input_tokens")
        c_tok = _int_metric(usage_map, "cached_tokens", "cached_content_token_count")
        o_tok = _int_metric(usage_map, "completion_tokens", "output_tokens")
        t_tok = _int_metric(usage_map, "reasoning_tokens", "thoughts_tokens")

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

        kv_val = _float_metric(rec, "gpu_cache_usage_pct", "kv_cache_usage_pct")
        wait_val = _float_metric(rec, "num_requests_waiting", "queue_depth")
        kv_max = max(kv_max, kv_val * 100.0 if 0.0 < kv_val <= 1.0 else kv_val)
        wait_max = max(wait_max, wait_val)

        ts_key = str(rec.get("minute", rec.get("timestamp", f"m{idx // 2}")))[:16]
        hour_key = ts_key[:13]
        minute_buckets[ts_key] = minute_buckets.get(ts_key, 0.0) + w_tok

        tenant = str(rec.get("tenant", rec.get("team", "default")))
        t_hours = tenant_buckets.setdefault(tenant, {})
        t_hours[hour_key] = t_hours.get(hour_key, 0.0) + w_tok

    all_hours = sorted({h for th in tenant_buckets.values() for h in th})
    tenant_series: dict[str, list[float]] = {}
    for tenant, th in tenant_buckets.items():
        tenant_series[tenant] = [round(th.get(h, 0.0), 2) for h in all_hours]

    if not tenant_series:
        tenant_series = {"default": [0.0]}

    hit_rate = cached_total / prompt_total if prompt_total > 0 else 0.0
    tpm_series = [round(v, 2) for v in minute_buckets.values()] or [0.0]

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


__all__ = [
    "DEFAULT_SELF_HOSTED_PROFILES",
    "MINUTES_PER_MONTH",
    "SelfHostedEndpointAudit",
    "SelfHostedProfile",
    "VllmTelemetrySnapshot",
    "evaluate_self_hosted_endpoint",
    "load_inference_server_file",
    "parse_inference_server_telemetry",
    "resolve_self_hosted_profile",
]
