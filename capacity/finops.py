"""Newsvendor hybrid commitment sizing and FinOps FOCUS 1.2 Two-Pool Chargeback (`ADR-003`).

1. Newsvendor Hybrid Commitment Optimizer (`optimize_hybrid_commitment`):
   - Balances Dedicated Provisioned Throughput (PT) + Flexible Savings Plans (FSP) against
     elastic PayGo spillover (`Priority 1.8x`, `Standard 1.0x`, `Flex/Batch 0.5x`).
2. FOCUS 1.2 Two-Pool Chargeback & Asynchronous Batch Water-Filling (`compute_focus_chargeback`):
   - Proves and fixes both enterprise reserved-capacity chargeback anti-patterns:
     (a) Unweighted raw-token counting vs. hardware-weighted burndown (`4x` decode/reasoning).
     (b) Naive full-pool idle amortization vs. splitting Active Consumed and Unallocated Idle
         Capacity (reclaimed off-peak via `$0`-marginal-cost asynchronous batch water-filling).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from capacity.core import percentile
from capacity.models import (
    FocusChargebackReport,
    HybridCommitmentRecommendation,
    TenantChargebackRow,
)

MINUTES_PER_MONTH = 30 * 24 * 60  # 43,200 minutes in a standard 30-day billing month

SPILLOVER_MULTIPLIERS: dict[str, float] = {
    "priority": 1.8,
    "standard": 1.0,
    "flex": 0.5,
}

# Realistic default weighted-to-raw token ratios per workload archetype:
# e.g., reasoning-heavy claims triage (high output/thinking share -> 1.85x burndown per raw token)
# vs. high-cached policy audit (0.45x burndown per raw token) vs. balanced copilot (1.10x).
DEFAULT_TENANT_RAW_TOKEN_WEIGHTS: dict[str, float] = {
    "claims-triage": 1.85,
    "underwriting-copilot": 1.10,
    "policy-audit": 0.45,
}


def optimize_hybrid_commitment(
    tpm_series: Sequence[int | float],
    model: str = "gemini-3.8-flash",
    tpm_per_gsu: int = 120_900,
    gsu_monthly_price_usd: float = 2_000.0,
    paygo_per_1m_burndown_tokens_usd: float = 0.50,
    spillover_tier: str = "priority",
    fsp_discount_pct: float = 0.0,
) -> HybridCommitmentRecommendation:
    """Solve the Newsvendor critical fractile and integer GSU grid for lowest monthly cost."""
    cleaned_series = sorted(max(0.0, float(v)) for v in tpm_series) if tpm_series else [0.0]
    gsu_cap = max(1, int(tpm_per_gsu))

    tier_key = spillover_tier.strip().lower()
    if tier_key not in SPILLOVER_MULTIPLIERS:
        raise ValueError(
            f"Unsupported spillover_tier '{spillover_tier}'. "
            f"Choose from {sorted(SPILLOVER_MULTIPLIERS)}."
        )
    spill_mult = SPILLOVER_MULTIPLIERS[tier_key]

    fsp_clamped = min(50.0, max(0.0, float(fsp_discount_pct)))
    effective_gsu_price = gsu_monthly_price_usd * (1.0 - fsp_clamped / 100.0)
    effective_spill_per_1m = paygo_per_1m_burndown_tokens_usd * spill_mult

    # Monthly cost of running 1 GSU's full capacity (100% 24x7 utilization) on the spillover tier
    tokens_per_gsu_month_millions = (gsu_cap * MINUTES_PER_MONTH) / 1_000_000.0
    spillover_equiv_monthly_per_gsu = (
        tokens_per_gsu_month_millions * effective_spill_per_1m
    )

    if spillover_equiv_monthly_per_gsu > 0:
        breakeven_utilization = min(
            1.0, effective_gsu_price / spillover_equiv_monthly_per_gsu
        )
    else:
        breakeven_utilization = 1.0

    critical_quantile = max(0.0, min(1.0, 1.0 - breakeven_utilization))

    p50_tpm = percentile(cleaned_series, 50)
    p90_tpm = percentile(cleaned_series, 90)
    p99_tpm = percentile(cleaned_series, 99)

    p50_gsus = math.ceil(p50_tpm / gsu_cap) if p50_tpm > 0 else 0
    p90_gsus = math.ceil(p90_tpm / gsu_cap) if p90_tpm > 0 else 0
    p99_gsus = math.ceil(p99_tpm / gsu_cap) if p99_tpm > 0 else 0

    n_points = len(cleaned_series)

    def _evaluate_monthly_cost(candidate_gsus: int) -> tuple[float, float, float]:
        pt_cost = candidate_gsus * effective_gsu_price
        committed_tpm = candidate_gsus * gsu_cap
        mean_spill_tpm = (
            sum(max(0.0, x - committed_tpm) for x in cleaned_series) / n_points
        )
        monthly_spill_millions = (mean_spill_tpm * MINUTES_PER_MONTH) / 1_000_000.0
        spill_cost = monthly_spill_millions * effective_spill_per_1m
        return pt_cost + spill_cost, pt_cost, spill_cost

    max_gsus = math.ceil(cleaned_series[-1] / gsu_cap) if cleaned_series[-1] > 0 else 0
    quantile_tpm = percentile(cleaned_series, critical_quantile * 100.0)
    seed_gsus = math.floor(quantile_tpm / gsu_cap)

    candidates = sorted(
        {
            g
            for g in [
                *range(min(max_gsus + 1, 501)),
                seed_gsus - 1,
                seed_gsus,
                seed_gsus + 1,
                p50_gsus,
                p90_gsus,
                p99_gsus,
                max_gsus,
            ]
            if 0 <= g <= max_gsus
        }
    )
    if max_gsus > 500:
        for g in range(max(0, seed_gsus - 100), min(max_gsus + 1, seed_gsus + 101)):
            candidates.append(g)
        candidates = sorted(set(candidates))

    best_gsus = 0
    best_total, best_pt, best_spill = _evaluate_monthly_cost(0)
    for g in candidates:
        tot, pt_c, sp_c = _evaluate_monthly_cost(g)
        if tot < best_total:
            best_gsus = g
            best_total, best_pt, best_spill = tot, pt_c, sp_c

    mean_total_tpm = sum(cleaned_series) / n_points
    paygo_100pct_cost = (
        (mean_total_tpm * MINUTES_PER_MONTH) / 1_000_000.0
    ) * effective_spill_per_1m
    p99_pt_cost = p99_gsus * effective_gsu_price

    savings_vs_paygo = max(0.0, paygo_100pct_cost - best_total)
    savings_vs_paygo_pct = (
        (savings_vs_paygo / paygo_100pct_cost) * 100.0 if paygo_100pct_cost > 0 else 0.0
    )
    savings_vs_p99 = max(0.0, p99_pt_cost - best_total)
    savings_vs_p99_pct = (
        (savings_vs_p99 / p99_pt_cost) * 100.0 if p99_pt_cost > 0 else 0.0
    )

    return HybridCommitmentRecommendation(
        model=model,
        tpm_per_gsu=gsu_cap,
        gsu_monthly_price_usd=round(gsu_monthly_price_usd, 2),
        fsp_discount_pct=round(fsp_clamped, 2),
        effective_gsu_monthly_price_usd=round(effective_gsu_price, 2),
        spillover_tier=tier_key,
        spillover_multiplier=spill_mult,
        effective_spillover_per_1m_tokens_usd=round(effective_spill_per_1m, 4),
        breakeven_utilization_pct=round(breakeven_utilization * 100.0, 2),
        critical_quantile=round(critical_quantile, 4),
        optimal_gsus=best_gsus,
        p50_gsus=p50_gsus,
        p90_gsus=p90_gsus,
        p99_gsus=p99_gsus,
        pt_base_monthly_cost_usd=round(best_pt, 2),
        spillover_monthly_cost_usd=round(best_spill, 2),
        total_hybrid_monthly_cost_usd=round(best_total, 2),
        paygo_100pct_monthly_cost_usd=round(paygo_100pct_cost, 2),
        p99_pt_monthly_cost_usd=round(p99_pt_cost, 2),
        savings_vs_paygo_usd=round(savings_vs_paygo, 2),
        savings_vs_paygo_pct=round(savings_vs_paygo_pct, 2),
        savings_vs_p99_pt_usd=round(savings_vs_p99, 2),
        savings_vs_p99_pt_pct=round(savings_vs_p99_pct, 2),
    )


def compute_focus_chargeback(
    hourly_consumed_tpm_by_tenant: Mapping[str, Sequence[int | float] | int | float],
    provisioned_gsus: int,
    tpm_per_gsu: int = 120_900,
    gsu_monthly_price_usd: float = 2_000.0,
    async_batch_waterfill_tpm: float = 0.0,
    tenant_raw_token_weights: Mapping[str, float] | None = None,
) -> FocusChargebackReport:
    """Compute FinOps FOCUS 1.2 Two-Pool Chargeback + Async Batch Water-Filling."""
    gsus = max(1, int(provisioned_gsus))
    gsu_cap = max(1, int(tpm_per_gsu))
    capacity_tpm = float(gsus * gsu_cap)
    total_commitment_cost = gsus * float(gsu_monthly_price_usd)

    normalized_tenants: dict[str, list[float]] = {}
    max_hours = 1
    for tenant, series_or_scalar in hourly_consumed_tpm_by_tenant.items():
        if isinstance(series_or_scalar, (int, float)):
            arr = [max(0.0, float(series_or_scalar))]
        else:
            arr = [max(0.0, float(v)) for v in series_or_scalar] or [0.0]
        normalized_tenants[tenant] = arr
        max_hours = max(max_hours, len(arr))

    if not normalized_tenants:
        normalized_tenants = {"default": [0.0]}
        max_hours = 1

    for tenant, arr in list(normalized_tenants.items()):
        if len(arr) < max_hours:
            normalized_tenants[tenant] = arr + [0.0] * (max_hours - len(arr))

    hourly_commit_cost = total_commitment_cost / max_hours
    scale_to_month_minutes = MINUTES_PER_MONTH / max_hours
    waterfill_demand_tpm = max(0.0, float(async_batch_waterfill_tpm))

    tenant_active_cost: dict[str, float] = dict.fromkeys(normalized_tenants, 0.0)
    tenant_tokens: dict[str, float] = dict.fromkeys(normalized_tenants, 0.0)

    total_active_cost = 0.0
    total_waterfill_cost = 0.0
    total_waterfill_tokens = 0.0
    sum_active_util = 0.0
    sum_effective_util = 0.0

    for h in range(max_hours):
        hour_total_tpm = sum(normalized_tenants[t][h] for t in normalized_tenants)
        active_util_h = (
            min(1.0, hour_total_tpm / capacity_tpm) if capacity_tpm > 0 else 0.0
        )
        sum_active_util += active_util_h

        active_cost_h = hourly_commit_cost * active_util_h
        total_active_cost += active_cost_h

        spare_tpm_h = max(0.0, capacity_tpm - min(capacity_tpm, hour_total_tpm))
        waterfilled_tpm_h = min(spare_tpm_h, waterfill_demand_tpm)
        waterfill_util_h = (
            waterfilled_tpm_h / capacity_tpm if capacity_tpm > 0 else 0.0
        )
        sum_effective_util += active_util_h + waterfill_util_h

        waterfill_cost_h = hourly_commit_cost * waterfill_util_h
        total_waterfill_cost += waterfill_cost_h
        total_waterfill_tokens += waterfilled_tpm_h * scale_to_month_minutes

        for t in normalized_tenants:
            t_tpm = normalized_tenants[t][h]
            tenant_tokens[t] += t_tpm * scale_to_month_minutes
            if hour_total_tpm > 0:
                share_h = t_tpm / hour_total_tpm
                tenant_active_cost[t] += active_cost_h * share_h

    total_tokens_all_tenants = sum(tenant_tokens.values())
    raw_weights_cfg = (
        dict(tenant_raw_token_weights)
        if tenant_raw_token_weights is not None
        else DEFAULT_TENANT_RAW_TOKEN_WEIGHTS
    )
    tenant_raw_tokens: dict[str, float] = {}
    for t, w_tok in tenant_tokens.items():
        w_ratio = max(0.1, float(raw_weights_cfg.get(t, 1.0)))
        tenant_raw_tokens[t] = w_tok / w_ratio
    total_raw_tokens_all_tenants = sum(tenant_raw_tokens.values())

    tenant_rows: list[TenantChargebackRow] = []
    for t in sorted(normalized_tenants):
        w_share = (
            tenant_tokens[t] / total_tokens_all_tenants
            if total_tokens_all_tenants > 0
            else 0.0
        )
        raw_share = (
            tenant_raw_tokens[t] / total_raw_tokens_all_tenants
            if total_raw_tokens_all_tenants > 0
            else w_share
        )
        naive_full_cost = total_commitment_cost * w_share
        act_cost = tenant_active_cost[t]
        unweighted_active_cost = total_active_cost * raw_share
        distortion = act_cost - unweighted_active_cost
        avoided_penalty = max(0.0, naive_full_cost - act_cost)

        tenant_rows.append(
            TenantChargebackRow(
                tenant=t,
                consumed_burndown_tokens=round(tenant_tokens[t], 2),
                usage_share_pct=round(w_share * 100.0, 2),
                unweighted_raw_token_share_pct=round(raw_share * 100.0, 2),
                active_allocated_cost_usd=round(act_cost, 2),
                unweighted_raw_token_cost_usd=round(unweighted_active_cost, 2),
                raw_token_distortion_usd=round(distortion, 2),
                naive_full_pool_cost_usd=round(naive_full_cost, 2),
                idle_penalty_avoided_usd=round(avoided_penalty, 2),
            )
        )

    unallocated_before_wf = max(0.0, total_commitment_cost - total_active_cost)
    net_unallocated = max(0.0, unallocated_before_wf - total_waterfill_cost)

    return FocusChargebackReport(
        provisioned_gsus=gsus,
        total_commitment_cost_usd=round(total_commitment_cost, 2),
        active_utilization_pct=round((sum_active_util / max_hours) * 100.0, 2),
        effective_utilization_with_waterfill_pct=round(
            (sum_effective_util / max_hours) * 100.0, 2
        ),
        active_consumed_cost_usd=round(total_active_cost, 2),
        unallocated_idle_cost_before_waterfill_usd=round(unallocated_before_wf, 2),
        reclaimed_waterfill_cost_usd=round(total_waterfill_cost, 2),
        net_unallocated_idle_cost_usd=round(net_unallocated, 2),
        waterfill_reclaimed_tokens=round(total_waterfill_tokens, 2),
        tenants=tenant_rows,
    )
