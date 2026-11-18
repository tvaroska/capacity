"""Newsvendor hybrid commitment sizing and shared series-matrix padding (`ADR-003`)."""

from __future__ import annotations

import math

from capacity.contract.requests import OptimizeRequest
from capacity.contract.results import HybridCommitmentRecommendation
from capacity.engine.catalog import ModelCatalog, build_model_catalog, normalize_model_id
from capacity.engine.stats import argmin_convex_int, percentile

# 43,200 minutes in a standard 30-day billing month (30 * 24 * 60).
MINUTES_PER_MONTH = 30 * 24 * 60
# 168 hours in a 7-day reservation week (`ECUR_168h` denominator, ADR-003).
HOURS_PER_WEEK = 168.0
# Default monthly list price per 1 Managed Gemini GSU in USD (ADR-003).
DEFAULT_GSU_MONTHLY_PRICE_USD = 2_000.0

SPILLOVER_MULTIPLIERS: dict[str, float] = {
    "priority": 1.8,
    "standard": 1.0,
    "flex": 0.5,
}


def pad_series_matrix(
    raw_by_key: dict[str, list[float] | float],
) -> tuple[dict[str, list[float]], int]:
    """Normalize scalar or variable-length series to a common zero-padded horizon."""
    normalized: dict[str, list[float]] = {}
    max_len = 1
    for key, series_or_scalar in raw_by_key.items():
        if isinstance(series_or_scalar, (int, float)):
            arr = [float(series_or_scalar)]
        else:
            arr = [float(v) for v in series_or_scalar] or [0.0]
        normalized[key] = arr
        max_len = max(max_len, len(arr))
    for key, arr in list(normalized.items()):
        if len(arr) < max_len:
            normalized[key] = arr + [0.0] * (max_len - len(arr))
    return normalized, max_len


def optimize_hybrid_commitment(
    req: OptimizeRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> HybridCommitmentRecommendation:
    """Solve the Newsvendor critical fractile and the GSU count with the lowest monthly cost."""
    cat = catalog if catalog is not None else build_model_catalog()
    norm_model = normalize_model_id(req.model)
    gsu_cap = cat.require_capacity(req.model)
    cleaned_series = sorted(req.tpm_series)
    duty_fraction = req.active_duty_cycle_pct / 100.0
    active_month_minutes = MINUTES_PER_MONTH * duty_fraction

    spill_mult = SPILLOVER_MULTIPLIERS[req.spillover_tier]
    effective_gsu_price = req.gsu_monthly_price_usd * (1.0 - req.fsp_discount_pct / 100.0)
    effective_spill_per_1m = req.paygo_per_1m_burndown_tokens_usd * spill_mult

    tokens_per_gsu_active_millions = (gsu_cap * active_month_minutes) / 1_000_000.0
    spillover_equiv_active_per_gsu = tokens_per_gsu_active_millions * effective_spill_per_1m

    full_24x7_millions = (gsu_cap * MINUTES_PER_MONTH) / 1_000_000.0
    full_24x7_spill_equiv = full_24x7_millions * effective_spill_per_1m
    breakeven_utilization = (
        min(1.0, effective_gsu_price / full_24x7_spill_equiv) if full_24x7_spill_equiv > 0 else 1.0
    )
    active_window_breakeven = (
        min(1.0, effective_gsu_price / spillover_equiv_active_per_gsu)
        if spillover_equiv_active_per_gsu > 0
        else 1.0
    )
    critical_quantile = max(0.0, min(1.0, 1.0 - active_window_breakeven))

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
        mean_spill_tpm = sum(max(0.0, x - committed_tpm) for x in cleaned_series) / n_points
        monthly_spill_millions = (mean_spill_tpm * active_month_minutes) / 1_000_000.0
        spill_cost = monthly_spill_millions * effective_spill_per_1m
        return pt_cost + spill_cost, pt_cost, spill_cost

    max_gsus = math.ceil(cleaned_series[-1] / gsu_cap) if cleaned_series[-1] > 0 else 0
    best_gsus = argmin_convex_int(lambda g: _evaluate_monthly_cost(g)[0], max_gsus)
    best_total, best_pt, best_spill = _evaluate_monthly_cost(best_gsus)

    mean_total_tpm = sum(cleaned_series) / n_points
    paygo_100pct_cost = (
        (mean_total_tpm * active_month_minutes) / 1_000_000.0
    ) * effective_spill_per_1m
    p99_pt_cost = p99_gsus * effective_gsu_price

    savings_vs_paygo = max(0.0, paygo_100pct_cost - best_total)
    savings_vs_paygo_pct = (
        (savings_vs_paygo / paygo_100pct_cost) * 100.0 if paygo_100pct_cost > 0 else 0.0
    )
    savings_vs_p99 = max(0.0, p99_pt_cost - best_total)
    savings_vs_p99_pct = (savings_vs_p99 / p99_pt_cost) * 100.0 if p99_pt_cost > 0 else 0.0

    return HybridCommitmentRecommendation(
        model=norm_model,
        tpm_per_gsu=gsu_cap,
        gsu_monthly_price_usd=round(req.gsu_monthly_price_usd, 2),
        fsp_discount_pct=round(req.fsp_discount_pct, 2),
        effective_gsu_monthly_price_usd=round(effective_gsu_price, 2),
        spillover_tier=req.spillover_tier,
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
