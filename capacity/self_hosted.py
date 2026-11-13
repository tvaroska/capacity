"""Self-hosted model capacity planning, GPU/TPU quota & KV-cache physics, and P/D sizing."""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import Field

from capacity.burst import evaluate_prefix_cache_dilution
from capacity.finops import MINUTES_PER_MONTH, clean_finite_series
from capacity.forecasting import UnknownModelError, argmin_convex_int, percentile
from capacity.models import DisaggregatedPoolSizing, PrefixCacheAudit, StrictModel
from capacity.telemetry_parsers import (
    VllmTelemetrySnapshot,
    load_inference_server_file,
    parse_inference_server_telemetry,
)

HOURS_PER_MONTH = 720.0
# Scale-to-zero savings assume a non-production endpoint is idle this many of a week's 168 hours.
NONPROD_IDLE_HOURS_PER_WEEK = 108.0


class SelfHostedProfile(StrictModel):
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
    ),
}


class SelfHostedEndpointAudit(StrictModel):
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


def resolve_self_hosted_profile(model_or_profile: str) -> SelfHostedProfile:
    """Resolve a self-hosted model identifier to its hardware and throughput profile."""
    cleaned = model_or_profile.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if cleaned in DEFAULT_SELF_HOSTED_PROFILES:
        return DEFAULT_SELF_HOSTED_PROFILES[cleaned]
    for key in sorted(DEFAULT_SELF_HOSTED_PROFILES, key=len, reverse=True):
        if cleaned.startswith(key):
            return DEFAULT_SELF_HOSTED_PROFILES[key]
    raise UnknownModelError(
        f"No self-hosted profile for '{model_or_profile}'. "
        f"Known profiles: {', '.join(sorted(DEFAULT_SELF_HOSTED_PROFILES))}."
    )


def evaluate_self_hosted_endpoint(
    tpm_series: Sequence[int | float],
    profile: SelfHostedProfile | str = "llama-3.3-70b-fp8",
    min_replica_count: int = 2,
    max_replica_count: int = 6,
    observed_accelerator_duty_cycle_pct: float = 52.0,
    observed_vllm_kv_cache_usage_pct: float = 91.5,
    observed_vllm_requests_waiting: float = 14.0,
    total_input_tokens: int = 20_000,
    shared_prefix_ratio: float = 0.80,
    baseline_cache_hit_rate: float = 0.80,
) -> SelfHostedEndpointAudit:
    """Audit a self-hosted GPU/TPU endpoint for quota locks, KV saturation, and Newsvendor."""
    prof = (
        profile if isinstance(profile, SelfHostedProfile) else resolve_self_hosted_profile(profile)
    )
    min_reps = max(0, int(min_replica_count))
    max_reps = max(max(1, min_reps), int(max_replica_count))

    locked_accels = max_reps * prof.accelerators_per_replica
    baseline_accels = min_reps * prof.accelerators_per_replica
    stranded_accels = max(0, locked_accels - baseline_accels)

    cleaned = clean_finite_series(tpm_series)

    has_traffic = cleaned[-1] > 0.0
    kv_preemption_risk = has_traffic and (
        observed_vllm_kv_cache_usage_pct >= 85.0 or observed_vllm_requests_waiting >= 5.0
    )
    duty_cycle_illusion = (
        has_traffic and observed_accelerator_duty_cycle_pct < 60.0 and kv_preemption_risk
    )

    rep_tpm = float(prof.tpm_per_replica)
    p99_tpm = float(percentile(cleaned, 99))
    p99_replicas = math.ceil(p99_tpm / rep_tpm) if p99_tpm > 0 else 0
    max_series_replicas = math.ceil(cleaned[-1] / rep_tpm) if cleaned[-1] > 0 else 0

    on_demand_monthly_equiv = prof.on_demand_replica_hourly_usd * HOURS_PER_MONTH
    breakeven_util = min(1.0, prof.reserved_replica_monthly_usd / on_demand_monthly_equiv)

    n_points = len(cleaned)

    def _monthly_cost(reserved_reps: int) -> tuple[float, float, float]:
        base_cost = reserved_reps * prof.reserved_replica_monthly_usd
        base_cap_tpm = reserved_reps * rep_tpm
        mean_burst_replicas = (
            sum(math.ceil(max(0.0, x - base_cap_tpm) / rep_tpm) for x in cleaned) / n_points
        )
        burst_cost = mean_burst_replicas * on_demand_monthly_equiv
        return base_cost + burst_cost, base_cost, burst_cost

    # Reserved-plus-burst cost is convex in the reserved count, so bisection is exact.
    best_reps = argmin_convex_int(lambda r: _monthly_cost(r)[0], max_series_replicas)
    best_total, best_base, best_burst = _monthly_cost(best_reps)

    all_on_demand_cost = (
        sum(math.ceil(x / rep_tpm) for x in cleaned) / n_points
    ) * on_demand_monthly_equiv
    p99_static_reserved_cost = p99_replicas * prof.reserved_replica_monthly_usd

    idle_fraction = NONPROD_IDLE_HOURS_PER_WEEK / 168.0
    scale_to_zero_savings = max(1, min_reps) * on_demand_monthly_equiv * idle_fraction

    # The trace carries no request size, so no request rate can be derived from it: the
    # replica counts below stay at the function's nominal rate, which the audit reports.
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


def size_disaggregated_prefill_decode(
    profile: SelfHostedProfile | str = "llama-3.3-70b-fp8",
    peak_qps: float = 18.0,
    avg_input_tokens: int = 6_400,
    avg_output_tokens: int = 450,
    target_ttft_ms: float = 350.0,
    target_tpot_ms: float = 25.0,
) -> DisaggregatedPoolSizing:
    """Size separate Prefill (compute-bound) and Decode (KV/bandwidth-bound) worker pools."""
    prof = (
        profile if isinstance(profile, SelfHostedProfile) else resolve_self_hosted_profile(profile)
    )
    qps = max(0.01, float(peak_qps))
    in_tok = max(1, int(avg_input_tokens))
    out_tok = max(1, int(avg_output_tokens))
    ttft_ms = max(20.0, float(target_ttft_ms))
    tpot_ms = max(5.0, float(target_tpot_ms))

    prefill_scale = 4.5 * min(1.0, ttft_ms / 350.0)
    prefill_tps_capacity = prof.tps_per_replica * prefill_scale
    required_prefill_tps = qps * in_tok
    prefill_replicas = max(1, math.ceil(required_prefill_tps / max(1.0, prefill_tps_capacity)))

    decode_duration_sec = out_tok * (tpot_ms / 1_000.0)
    active_sequences = qps * decode_duration_sec
    active_kv_tokens = active_sequences * (in_tok + out_tok)
    kv_safe_per_replica = prof.max_kv_cache_tokens * 0.80

    decode_scale = min(1.2, 25.0 / tpot_ms)
    decode_by_kv = math.ceil(active_kv_tokens / max(1.0, kv_safe_per_replica))
    decode_tps_capacity = (prof.tps_per_replica / 4.0) * decode_scale
    decode_by_bw = math.ceil((qps * out_tok) / max(1.0, decode_tps_capacity))
    decode_replicas = max(1, decode_by_kv, decode_by_bw)

    total_disagg = prefill_replicas + decode_replicas

    mono_weighted_tps = (
        (required_prefill_tps / max(0.1, prefill_scale))
        + ((qps * out_tok * 4.0) / max(0.1, decode_scale))
    ) * 1.30
    mono_by_compute = math.ceil(mono_weighted_tps / max(1.0, prof.tps_per_replica))
    mono_by_kv = math.ceil(active_kv_tokens / max(1.0, kv_safe_per_replica))
    monolithic_replicas = max(1, mono_by_compute, mono_by_kv)
    delta = monolithic_replicas - total_disagg

    disagg_cost = total_disagg * prof.reserved_replica_monthly_usd
    mono_cost = monolithic_replicas * prof.reserved_replica_monthly_usd

    ttft_headroom = round(
        max(
            0.0,
            ((prefill_replicas * prefill_tps_capacity) - required_prefill_tps)
            / max(1.0, prefill_replicas * prefill_tps_capacity)
            * 100.0,
        ),
        2,
    )
    tpot_headroom = round(
        max(
            0.0,
            ((decode_replicas * kv_safe_per_replica) - active_kv_tokens)
            / max(1.0, decode_replicas * kv_safe_per_replica)
            * 100.0,
        ),
        2,
    )
    if delta > 0:
        rec = (
            f"Deploy {prefill_replicas} prefill + {decode_replicas} decode replicas "
            f"({total_disagg} total vs {monolithic_replicas} monolithic, saving {delta} "
            "replica[s]) behind GKE Inference Gateway (epp / llm-d) to isolate TTFT prefill "
            "bursts from TPOT decode streams."
        )
    else:
        rec = (
            f"Keep {monolithic_replicas} monolithic replica(s) at {qps:.2f} QPS "
            f"(disaggregation requires {total_disagg} minimum replicas; P/D separation pays off "
            "once combined pool demand exceeds 3+ replicas)."
        )

    return DisaggregatedPoolSizing(
        profile=prof.profile_id,
        accelerator_type=prof.accelerator_type,
        accelerators_per_replica=prof.accelerators_per_replica,
        peak_qps=round(qps, 2),
        avg_input_tokens=in_tok,
        avg_output_tokens=out_tok,
        prefill_replicas=prefill_replicas,
        decode_replicas=decode_replicas,
        total_disaggregated_replicas=total_disagg,
        monolithic_replicas=monolithic_replicas,
        replica_delta=delta,
        disaggregated_monthly_cost_usd=round(disagg_cost, 2),
        monolithic_monthly_cost_usd=round(mono_cost, 2),
        ttft_headroom_pct=ttft_headroom,
        tpot_headroom_pct=tpot_headroom,
        recommendation=rec,
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
    "size_disaggregated_prefill_decode",
]
