"""Sub-second token-bucket micro-burst and prefix-cache dilution physics (`ADR-002`)."""

from __future__ import annotations

import math

from capacity.model_families import normalize_model_id, resolve_model_family
from capacity.models import MicroBurstAudit, ModelTierPrefixCacheAudit, PrefixCacheAudit


def compute_weighted_burndown_tokens(
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int = 0,
    cached_tokens: int = 0,
    *,
    model: str | None = None,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """Compute one request's weighted burndown: 1P Gemini (<=200K/>200K) or 3P Claude PT."""
    spec = resolve_model_family(model)
    total_in = max(0, input_tokens)
    cached = min(max(0, cached_tokens), total_in)
    rem_in = total_in - cached
    write_5m = min(max(0, cache_write_5m_tokens), rem_in)
    write_1h = min(max(0, cache_write_1h_tokens), rem_in - write_5m)
    uncached_in = rem_in - write_5m - write_1h
    decode_tokens = max(0, output_tokens) + max(0, thoughts_tokens)

    threshold = spec.long_context_threshold
    if threshold is not None and total_in > threshold:
        in_w = spec.long_context_input_weight
        cache_w = spec.long_context_cached_read_weight
        out_w = spec.long_context_output_weight
    else:
        in_w = spec.standard_input_weight
        cache_w = spec.standard_cached_read_weight
        out_w = spec.standard_output_weight

    return (
        (uncached_in * in_w)
        + (cached * cache_w)
        + (write_5m * spec.cache_write_5m_weight)
        + (write_1h * spec.cache_write_1h_weight)
        + (decode_tokens * out_w)
    )


def evaluate_micro_burst_window(
    sixty_sec_tpm: float,
    peak_1s_tps: float,
    provisioned_gsus: int,
    tps_per_gsu: float = 2_015.0,
    peak_10s_tps: float | None = None,
    *,
    model: str | None = None,
    peak_30s_tps: float | None = None,
) -> MicroBurstAudit:
    """Evaluate token-bucket enforcement (`1s`/`10s` on 1P Gemini; `30s`/`60s` on 3P Claude).

    Enforces 60-second token conservation: total tokens arriving inside the refill window
    cannot exceed `sixty_sec_tpm` (`active_peak_tps * refill_window_seconds <= sixty_sec_tpm`).
    """
    spec = resolve_model_family(model)
    gsus = max(0, provisioned_gsus)
    credit_pct = spec.burst_credit_pct
    if spec.family == "claude_3p":
        refill_window_seconds = spec.bucket_short_seconds
        long_window_seconds = spec.bucket_long_seconds
    else:
        refill_window_seconds = 1 if gsus >= 50 else 10
        long_window_seconds = None

    committed_tps = gsus * tps_per_gsu
    effective_window_tps_ceiling = committed_tps * (1.0 + max(0.0, credit_pct))
    window_token_budget = effective_window_tps_ceiling * refill_window_seconds

    bounded_tpm = max(0.0, float(sixty_sec_tpm))
    sixty_sec_avg_tps = bounded_tpm / 60.0
    apparent_60s_util_pct = (
        (sixty_sec_avg_tps / committed_tps) * 100.0 if committed_tps > 0 else 0.0
    )

    raw_1s = max(0.0, float(peak_1s_tps))
    if spec.family == "claude_3p":
        fallback_burst = (
            max(0.0, float(peak_30s_tps))
            if peak_30s_tps is not None
            else (
                max(0.0, float(peak_10s_tps))
                if peak_10s_tps is not None
                else max(sixty_sec_avg_tps, raw_1s)
            )
        )
        raw_peak_tps = max(sixty_sec_avg_tps, fallback_burst)
    elif refill_window_seconds == 1:
        raw_peak_tps = max(sixty_sec_avg_tps, raw_1s)
    else:
        effective_10s = (
            max(0.0, float(peak_10s_tps))
            if peak_10s_tps is not None
            else max(sixty_sec_avg_tps, raw_1s)
        )
        raw_peak_tps = max(sixty_sec_avg_tps, effective_10s)

    if bounded_tpm > 0.0:
        max_conserved_window_tps = bounded_tpm / float(refill_window_seconds)
        active_peak_tps = min(raw_peak_tps, max_conserved_window_tps)
    elif raw_1s > 0.0:
        active_peak_tps = raw_1s / float(refill_window_seconds)
    else:
        active_peak_tps = 0.0

    peak_window_util_pct = (
        (active_peak_tps / effective_window_tps_ceiling) * 100.0
        if effective_window_tps_ceiling > 0
        else (100.0 if active_peak_tps > 0 else 0.0)
    )
    spillover_tps = max(0.0, active_peak_tps - effective_window_tps_ceiling)
    triggers_spillover = spillover_tps > 0.0
    illusion_detected = apparent_60s_util_pct < 70.0 and triggers_spillover

    per_gsu_effective_tps = tps_per_gsu * (1.0 + max(0.0, credit_pct))
    required_gsus = (
        math.ceil(active_peak_tps / per_gsu_effective_tps)
        if per_gsu_effective_tps > 0 and active_peak_tps > 0
        else 0
    )

    return MicroBurstAudit(
        provisioned_gsus=gsus,
        refill_window_seconds=refill_window_seconds,
        long_window_seconds=long_window_seconds,
        enforcement_mode=spec.enforcement_mode,
        tps_per_gsu=round(tps_per_gsu, 2),
        committed_tps_ceiling=round(committed_tps, 2),
        burst_credit_pct=round(credit_pct * 100.0, 2),
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
    requests_per_second: float = 1.0,
) -> PrefixCacheAudit:
    """Quantify GSU/replica waste from round-robin prefix-cache dilution ($h / N$).

    Converts per-request burndown tokens into sustained burndown TPS via `requests_per_second`
    (`tokens/req * req/s = tokens/s`) before dividing by `tps_per_gsu`.
    """
    total_in = max(0, total_input_tokens)
    prefix_ratio = min(1.0, max(0.0, shared_prefix_ratio))
    h_sticky = min(1.0, max(0.0, baseline_cache_hit_rate))
    n_endpoints = max(1, num_round_robin_endpoints)
    rps = max(0.0, float(requests_per_second))

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
    rr_burndown = (rr_cached * cached_token_weight) + (rr_uncached * uncached_token_weight)

    sticky_tps = sticky_burndown * rps
    rr_tps = rr_burndown * rps

    inflation_mult = rr_burndown / sticky_burndown if sticky_burndown > 0 else 1.0
    sticky_gsus = (
        max(1, math.ceil(sticky_tps / tps_per_gsu)) if sticky_tps > 0 and tps_per_gsu > 0 else 0
    )
    rr_gsus = max(1, math.ceil(rr_tps / tps_per_gsu)) if rr_tps > 0 and tps_per_gsu > 0 else 0
    wasted_gsus = max(0, rr_gsus - sticky_gsus)
    savings_pct = (
        ((rr_burndown - sticky_burndown) / rr_burndown) * 100.0 if rr_burndown > 0 else 0.0
    )

    return PrefixCacheAudit(
        total_input_tokens=total_in,
        shared_prefix_ratio=round(prefix_ratio, 4),
        baseline_cache_hit_rate=round(h_sticky, 4),
        num_round_robin_endpoints=n_endpoints,
        requests_per_second=rps,
        effective_cache_hit_rate=round(h_effective, 4),
        sticky_burndown_tokens=round(sticky_burndown, 2),
        round_robin_burndown_tokens=round(rr_burndown, 2),
        burndown_inflation_multiplier=round(inflation_mult, 3),
        sticky_gsus=sticky_gsus,
        round_robin_gsus=rr_gsus,
        wasted_gsus=wasted_gsus,
        gsu_savings_pct=round(savings_pct, 2),
    )


def evaluate_model_tier_prefix_cache(
    model: str = "gemini-3.8-flash",
    total_input_tokens: int = 18_000,
    static_prefix_tokens: int = 14_000,
    output_tokens: int = 800,
    thoughts_tokens: int = 400,
    dynamic_header_offset_tokens: int | None = 120,
    single_turn_request: bool = False,
    dummy_turn_anchor_applied: bool = False,
    strip_thought_signatures: bool = False,
    num_replicas: int = 8,
) -> ModelTierPrefixCacheAudit:
    """Evaluate model-tier prefix-cache block alignment, Block-0 contamination, and routing hash."""
    norm = normalize_model_id(model)
    spec = resolve_model_family(norm)
    total_in = max(1, total_input_tokens)
    static_in = min(max(0, static_prefix_tokens), total_in)
    replicas = max(1, num_replicas)

    granularity = spec.checkpoint_granularity
    min_floor = spec.min_prefix_floor_tokens
    block_size = spec.prefix_block_size_tokens
    spillover_pct = 75.0 if granularity == "token_boundary" else 50.0

    fast_speedup = 2.5 if "-fast" in norm else 1.0
    fast_cost = 2.0 if "-fast" in norm else 1.0

    remediations: list[str] = []
    below_floor = static_in < min_floor
    if below_floor:
        remediations.append(
            f"Static prefix ({static_in:,} tokens) is below the {min_floor:,}-token minimum "
            f"floor for {norm}; consolidate static reference context above {min_floor:,} tokens."
        )

    block_zero_contaminated = False
    effective_static_span = static_in
    if dynamic_header_offset_tokens is not None and dynamic_header_offset_tokens < static_in:
        effective_static_span = max(0, dynamic_header_offset_tokens)
        if granularity == "block_4096" and dynamic_header_offset_tokens < block_size:
            block_zero_contaminated = True
            remediations.append(
                f"Dynamic token injected at offset {dynamic_header_offset_tokens:,} (<4,096) "
                "contaminates Block 0 and invalidates 100% of downstream 4,096-token cache blocks. "
                "Move timestamps, request UUIDs, and user state to the end of the prompt."
            )
        elif granularity == "token_boundary" and dynamic_header_offset_tokens < min_floor:
            block_zero_contaminated = True
            remediations.append(
                f"Dynamic token injected at offset {dynamic_header_offset_tokens:,} "
                f"(<{min_floor:,}) prevents prefix checkpointing. Move variable headers "
                "to the final user turn."
            )

    if below_floor or block_zero_contaminated or effective_static_span < min_floor:
        aligned_cacheable = 0
        trailing_uncached = static_in
    elif granularity == "block_4096":
        aligned_cacheable = (effective_static_span // block_size) * block_size
        trailing_uncached = static_in - aligned_cacheable
        if trailing_uncached > 0:
            remediations.append(
                f"Pad or align static system/tool prefix to a multiple of 4,096 tokens "
                f"({trailing_uncached:,} trailing static tokens currently re-prefill at 1.0x)."
            )
    else:
        aligned_cacheable = effective_static_span
        trailing_uncached = static_in - aligned_cacheable

    if single_turn_request and not dummy_turn_anchor_applied:
        routing_affinity = 1.0 / replicas
        remediations.append(
            "Single-turn request omits multi-turn prefix router hash; prepend a 2-token static "
            "dummy (user + model) turn pair after system_instruction to enable sticky routing."
        )
    else:
        routing_affinity = 0.90

    if strip_thought_signatures:
        routing_affinity = min(routing_affinity, 0.0077)
        remediations.append(
            "Stripping thought_signature on multi-turn tool loops collapses prefix cache hits "
            "to ~0.77%; preserve full modelContent (including thought_signature) across turns."
        )

    current_cached_tokens = round(aligned_cacheable * routing_affinity)
    effective_hit_rate = current_cached_tokens / total_in if total_in > 0 else 0.0

    if static_in < min_floor:
        opt_aligned = 0
    elif granularity == "block_4096":
        opt_aligned = (static_in // block_size) * block_size
    else:
        opt_aligned = static_in
    opt_cached_tokens = round(opt_aligned * 0.90)
    optimized_hit_rate = opt_cached_tokens / total_in if total_in > 0 else 0.0

    current_burndown = compute_weighted_burndown_tokens(
        input_tokens=total_in,
        output_tokens=output_tokens,
        thoughts_tokens=thoughts_tokens,
        cached_tokens=current_cached_tokens,
        model=norm,
    )
    optimized_burndown = compute_weighted_burndown_tokens(
        input_tokens=total_in,
        output_tokens=output_tokens,
        thoughts_tokens=thoughts_tokens,
        cached_tokens=opt_cached_tokens,
        model=norm,
    )
    waste_pct = (
        ((current_burndown - optimized_burndown) / current_burndown) * 100.0
        if current_burndown > 0 and current_burndown > optimized_burndown
        else 0.0
    )

    if spec.family == "claude_3p":
        remediations.append(
            f"3P Claude explicit prompt caching requires cache_control breakpoints above the "
            f"{min_floor:,}-token floor (1.25x 5m write / 2.0x 1h write vs. 0.1x cache read)."
        )
    elif not remediations:
        remediations.append(
            "Prefix structure is block-aligned and preserves multi-turn router hash affinity."
        )

    return ModelTierPrefixCacheAudit(
        model=model,
        normalized_model=norm,
        checkpoint_granularity=granularity,
        min_prefix_floor_tokens=min_floor,
        block_size_tokens=block_size,
        total_input_tokens=total_in,
        static_prefix_tokens=static_in,
        dynamic_header_offset_tokens=dynamic_header_offset_tokens,
        below_minimum_floor=below_floor,
        block_zero_contaminated=block_zero_contaminated,
        aligned_cacheable_tokens=aligned_cacheable,
        trailing_uncached_prefix_tokens=trailing_uncached,
        single_turn_request=single_turn_request,
        dummy_turn_anchor_applied=dummy_turn_anchor_applied,
        strip_thought_signatures=strip_thought_signatures,
        routing_affinity_hit_rate=round(routing_affinity, 4),
        effective_cache_hit_rate=round(effective_hit_rate, 4),
        optimized_cache_hit_rate=round(optimized_hit_rate, 4),
        current_burndown_tokens=round(current_burndown, 2),
        optimized_burndown_tokens=round(optimized_burndown, 2),
        burndown_waste_pct=round(waste_pct, 2),
        prefill_spillover_threshold_pct=spillover_pct,
        fast_decode_speedup_multiplier=fast_speedup,
        fast_decode_cost_multiplier=fast_cost,
        remediation_steps=remediations,
    )
