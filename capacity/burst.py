"""Sub-second token-bucket micro-burst and prefix-cache dilution physics (`ADR-002`).

Models three production failure modes that cause HTTP 429s or 2x-4x GSU/replica over-sizing:
1. Weighted Burndown Multipliers & the `>200K` Long-Context Cliff:
   - Standard (`<=200K` tokens): `1.0x` uncached + `0.1x` cached + `4.0x` (output + thoughts).
   - Long-Context (`>200K` tokens): `2.0x` uncached + `0.2x` cached + `7.5x` (output + thoughts).
2. Sub-Minute Token-Bucket Enforcement (`1s` for `>=50 GSUs`, `10s` for `<50 GSUs`, `5%` credit):
   - Explains why Cloud Monitoring's 60-second chart can show `<70%` utilization while sub-second
     agent bursts exhaust the `1s`/`10s` token bucket and trigger HTTP 429s or PayGo spillover.
3. Round-Robin Prefix-Cache Dilution ($h_{\\text{eff}} = h / N$):
   - Stateless round-robin load balancing across $N$ endpoints/replicas dilutes replica-local KV
     cache hit rates by $1/N$, inflating uncached prefill burndown and wasting GSUs or replicas.
"""

from __future__ import annotations

import math

from capacity.models import MicroBurstAudit, PrefixCacheAudit

LONG_CONTEXT_THRESHOLD_TOKENS = 200_000


def compute_weighted_burndown_tokens(
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int = 0,
    cached_tokens: int = 0,
    long_context_threshold: int = LONG_CONTEXT_THRESHOLD_TOKENS,
    standard_input_weight: float = 1.0,
    standard_cached_weight: float = 0.1,
    standard_output_weight: float = 4.0,
    long_context_input_weight: float = 2.0,
    long_context_cached_weight: float = 0.2,
    long_context_output_weight: float = 7.5,
) -> float:
    """Compute hardware-weighted burndown tokens accounting for reasoning and the `>200K` cliff."""
    total_in = max(0, input_tokens)
    cached = min(max(0, cached_tokens), total_in)
    uncached_in = total_in - cached
    decode_tokens = max(0, output_tokens) + max(0, thoughts_tokens)

    is_long_context = total_in > long_context_threshold
    in_w = long_context_input_weight if is_long_context else standard_input_weight
    cache_w = long_context_cached_weight if is_long_context else standard_cached_weight
    out_w = long_context_output_weight if is_long_context else standard_output_weight

    return (uncached_in * in_w) + (cached * cache_w) + (decode_tokens * out_w)


def evaluate_micro_burst_window(
    sixty_sec_tpm: float,
    peak_1s_tps: float,
    provisioned_gsus: int,
    tps_per_gsu: float = 2_015.0,
    peak_10s_tps: float | None = None,
    burst_credit_pct: float = 0.05,
) -> MicroBurstAudit:
    """Evaluate sub-minute token-bucket enforcement (`1s` for `>=50 GSUs`, `10s` for `<50 GSUs`).

    Demonstrates why a workload appearing `<70%` utilized on a 60-second Cloud Monitoring
    chart still experiences HTTP 429s or PayGo spillover during sub-second agent bursts.
    """
    gsus = max(0, provisioned_gsus)
    refill_window_seconds = 1 if gsus >= 50 else 10

    committed_tps = gsus * tps_per_gsu
    effective_window_tps_ceiling = committed_tps * (1.0 + max(0.0, burst_credit_pct))
    window_token_budget = effective_window_tps_ceiling * refill_window_seconds

    sixty_sec_avg_tps = max(0.0, sixty_sec_tpm) / 60.0
    apparent_60s_util_pct = (
        (sixty_sec_avg_tps / committed_tps) * 100.0 if committed_tps > 0 else 0.0
    )

    if refill_window_seconds == 1:
        active_peak_tps = max(sixty_sec_avg_tps, peak_1s_tps)
    else:
        effective_10s = (
            peak_10s_tps
            if peak_10s_tps is not None
            else max(sixty_sec_avg_tps, peak_1s_tps)
        )
        active_peak_tps = max(sixty_sec_avg_tps, effective_10s)

    peak_window_util_pct = (
        (active_peak_tps / effective_window_tps_ceiling) * 100.0
        if effective_window_tps_ceiling > 0
        else (100.0 if active_peak_tps > 0 else 0.0)
    )
    spillover_tps = max(0.0, active_peak_tps - effective_window_tps_ceiling)
    triggers_spillover = spillover_tps > 0.0
    illusion_detected = apparent_60s_util_pct < 70.0 and triggers_spillover

    per_gsu_effective_tps = tps_per_gsu * (1.0 + max(0.0, burst_credit_pct))
    required_gsus = (
        math.ceil(active_peak_tps / per_gsu_effective_tps)
        if per_gsu_effective_tps > 0 and active_peak_tps > 0
        else 0
    )

    return MicroBurstAudit(
        provisioned_gsus=gsus,
        refill_window_seconds=refill_window_seconds,
        tps_per_gsu=round(tps_per_gsu, 2),
        committed_tps_ceiling=round(committed_tps, 2),
        burst_credit_pct=round(burst_credit_pct * 100.0, 2),
        effective_window_tps_ceiling=round(effective_window_tps_ceiling, 2),
        window_token_budget=round(window_token_budget, 2),
        sixty_sec_average_tps=round(sixty_sec_avg_tps, 2),
        apparent_60s_utilization_pct=round(apparent_60s_util_pct, 2),
        peak_window_tps=round(active_peak_tps, 2),
        peak_window_utilization_pct=round(peak_window_util_pct, 2),
        burst_spillover_tps=round(spillover_tps, 2),
        triggers_429_or_spillover=triggers_spillover,
        illusion_detected=illusion_detected,
        gsus_required_for_zero_spillover=required_gsus,
    )


def evaluate_prefix_cache_dilution(
    total_input_tokens: int,
    shared_prefix_ratio: float = 0.80,
    baseline_cache_hit_rate: float = 0.80,
    num_round_robin_endpoints: int = 8,
    tps_per_gsu: float = 2_015.0,
    cached_token_weight: float = 0.1,
    uncached_token_weight: float = 1.0,
) -> PrefixCacheAudit:
    """Quantify GSU/replica waste from round-robin prefix-cache dilution ($h / N$)."""
    total_in = max(0, total_input_tokens)
    prefix_ratio = min(1.0, max(0.0, shared_prefix_ratio))
    h_sticky = min(1.0, max(0.0, baseline_cache_hit_rate))
    n_endpoints = max(1, num_round_robin_endpoints)

    h_effective = h_sticky / n_endpoints

    shared_tokens = total_in * prefix_ratio
    dynamic_tokens = total_in - shared_tokens

    sticky_cached = shared_tokens * h_sticky
    sticky_uncached = dynamic_tokens + (shared_tokens - sticky_cached)
    sticky_burndown = (sticky_cached * cached_token_weight) + (
        sticky_uncached * uncached_token_weight
    )

    rr_cached = shared_tokens * h_effective
    rr_uncached = dynamic_tokens + (shared_tokens - rr_cached)
    rr_burndown = (rr_cached * cached_token_weight) + (
        rr_uncached * uncached_token_weight
    )

    inflation_mult = rr_burndown / sticky_burndown if sticky_burndown > 0 else 1.0
    sticky_gsus = (
        max(1, math.ceil(sticky_burndown / tps_per_gsu))
        if sticky_burndown > 0 and tps_per_gsu > 0
        else 0
    )
    rr_gsus = (
        max(1, math.ceil(rr_burndown / tps_per_gsu))
        if rr_burndown > 0 and tps_per_gsu > 0
        else 0
    )
    wasted_gsus = max(0, rr_gsus - sticky_gsus)
    savings_pct = (
        ((rr_burndown - sticky_burndown) / rr_burndown) * 100.0
        if rr_burndown > 0
        else 0.0
    )

    return PrefixCacheAudit(
        total_input_tokens=total_in,
        shared_prefix_ratio=round(prefix_ratio, 4),
        baseline_cache_hit_rate=round(h_sticky, 4),
        num_round_robin_endpoints=n_endpoints,
        effective_cache_hit_rate=round(h_effective, 4),
        sticky_burndown_tokens=round(sticky_burndown, 2),
        round_robin_burndown_tokens=round(rr_burndown, 2),
        burndown_inflation_multiplier=round(inflation_mult, 3),
        sticky_gsus=sticky_gsus,
        round_robin_gsus=rr_gsus,
        wasted_gsus=wasted_gsus,
        gsu_savings_pct=round(savings_pct, 2),
    )
