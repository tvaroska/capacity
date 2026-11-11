"""Newsvendor hybrid commitment sizing, scavenger scheduling, and FOCUS 1.2 FinOps (`ADR-003`)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from capacity.forecasting import percentile
from capacity.models import (
    DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR,
    BatchScavengerAudit,
    DeadlineJobSpec,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    HybridCommitmentRecommendation,
    ResidualScheduleReport,
    ScheduledDeadlineJob,
    TenantChargebackRow,
    make_default_deadline_jobs,
)

MINUTES_PER_MONTH = 30 * 24 * 60  # 43,200 minutes in a standard 30-day billing month

SPILLOVER_MULTIPLIERS: dict[str, float] = {
    "priority": 1.8,
    "standard": 1.0,
    "flex": 0.5,
}

DEFAULT_TENANT_RAW_TOKEN_WEIGHTS: dict[str, float] = {
    "claims-triage": 1.85,
    "underwriting-copilot": 1.10,
    "policy-audit": 0.45,
}


def _clean_finite_series(series: Sequence[int | float]) -> list[float]:
    if not series:
        return [0.0]
    cleaned: list[float] = []
    for raw in series:
        val = float(raw)
        if not math.isfinite(val):
            raise ValueError(f"tpm_series values must be finite numbers, got {raw!r}")
        cleaned.append(max(0.0, val))
    return sorted(cleaned)


def optimize_hybrid_commitment(
    tpm_series: Sequence[int | float],
    model: str = "gemini-3.8-flash",
    tpm_per_gsu: int = 120_900,
    gsu_monthly_price_usd: float = 2_000.0,
    paygo_per_1m_burndown_tokens_usd: float = 0.50,
    spillover_tier: str = "priority",
    fsp_discount_pct: float = 0.0,
    active_duty_cycle_pct: float = 100.0,
) -> HybridCommitmentRecommendation:
    """Solve the Newsvendor critical fractile and integer GSU grid for lowest monthly cost.

    When `tpm_series` contains active-hours-only samples (e.g. an 8x5 daytime slice), pass
    `active_duty_cycle_pct` (such as `23.81` for 40h/168h) so variable spillover is integrated
    over active monthly minutes rather than 24x7 `43,200` minutes while PT still bills 24x7.
    """
    cleaned_series = _clean_finite_series(tpm_series)
    gsu_cap = max(1, int(tpm_per_gsu))
    duty_fraction = min(1.0, max(1e-6, float(active_duty_cycle_pct) / 100.0))
    active_month_minutes = MINUTES_PER_MONTH * duty_fraction

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

    tokens_per_gsu_active_millions = (gsu_cap * active_month_minutes) / 1_000_000.0
    spillover_equiv_active_per_gsu = tokens_per_gsu_active_millions * effective_spill_per_1m

    full_24x7_millions = (gsu_cap * MINUTES_PER_MONTH) / 1_000_000.0
    full_24x7_spill_equiv = full_24x7_millions * effective_spill_per_1m
    breakeven_utilization = (
        min(1.0, effective_gsu_price / full_24x7_spill_equiv)
        if full_24x7_spill_equiv > 0
        else 1.0
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
        mean_spill_tpm = (
            sum(max(0.0, x - committed_tpm) for x in cleaned_series) / n_points
        )
        monthly_spill_millions = (mean_spill_tpm * active_month_minutes) / 1_000_000.0
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
        (mean_total_tpm * active_month_minutes) / 1_000_000.0
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
            val = float(series_or_scalar)
            if not math.isfinite(val):
                raise ValueError(f"Non-finite chargeback value for tenant {tenant!r}")
            arr = [max(0.0, val)]
        else:
            arr = []
            for raw in series_or_scalar:
                val = float(raw)
                if not math.isfinite(val):
                    raise ValueError(f"Non-finite chargeback value for tenant {tenant!r}")
                arr.append(max(0.0, val))
            if not arr:
                arr = [0.0]
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
        active_util_h = min(1.0, hour_total_tpm / capacity_tpm) if capacity_tpm > 0 else 0.0
        sum_active_util += active_util_h

        active_cost_h = hourly_commit_cost * active_util_h
        total_active_cost += active_cost_h

        spare_tpm_h = max(0.0, capacity_tpm - min(capacity_tpm, hour_total_tpm))
        waterfilled_tpm_h = min(spare_tpm_h, waterfill_demand_tpm)
        waterfill_util_h = waterfilled_tpm_h / capacity_tpm if capacity_tpm > 0 else 0.0
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


def evaluate_batch_scavenger_economics(
    model: str = "gemini-3.8-flash",
    provisioned_gsus: int = 20,
    tpm_per_gsu: int = 120_900,
    gsu_monthly_price_usd: float = 2_000.0,
    daytime_active_utilization_pct: float = 24.0,
    batch_monthly_burndown_tokens: float = 45_000_000_000.0,
    paygo_per_1m_burndown_tokens_usd: float = 0.50,
) -> BatchScavengerAudit:
    """Compare Batch API (0.5x extra double-payment) vs $0 online PT scavenging."""
    gsus = max(1, int(provisioned_gsus))
    gsu_cap = max(1, int(tpm_per_gsu))
    pt_cost = gsus * float(gsu_monthly_price_usd)
    active_util = min(100.0, max(0.0, float(daytime_active_utilization_pct)))

    total_monthly_pt_tokens = float(gsus * gsu_cap * MINUTES_PER_MONTH)
    active_tokens = total_monthly_pt_tokens * (active_util / 100.0)
    idle_scavengable_tokens = max(0.0, (total_monthly_pt_tokens * 0.85) - active_tokens)

    batch_tokens = max(0.0, float(batch_monthly_burndown_tokens))
    harvested_free = min(batch_tokens, idle_scavengable_tokens)
    overflow_tokens = max(0.0, batch_tokens - harvested_free)

    flex_rate_per_1m = float(paygo_per_1m_burndown_tokens_usd) * SPILLOVER_MULTIPLIERS["flex"]
    batch_api_extra_usd = (batch_tokens / 1_000_000.0) * flex_rate_per_1m
    hybrid_flex_extra_usd = (overflow_tokens / 1_000_000.0) * flex_rate_per_1m
    waste_avoided = max(0.0, batch_api_extra_usd - hybrid_flex_extra_usd)

    effective_util_after = (
        ((active_tokens + harvested_free) / total_monthly_pt_tokens) * 100.0
        if total_monthly_pt_tokens > 0
        else 0.0
    )
    if overflow_tokens == 0.0:
        policy = "X-Vertex-AI-LLM-Request-Type: dedicated ($0.00 overage guaranteed)"
    else:
        policy = (
            "Omit X-Vertex-AI-LLM-Request-Type + "
            "X-Vertex-AI-LLM-Shared-Request-Type: flex ($0 PT first, 0.5x overflow)"
        )

    return BatchScavengerAudit(
        model=model,
        provisioned_gsus=gsus,
        pt_monthly_commitment_cost_usd=round(pt_cost, 2),
        daytime_active_utilization_pct=round(active_util, 2),
        idle_pt_monthly_burndown_tokens=round(idle_scavengable_tokens, 2),
        batch_monthly_burndown_tokens=round(batch_tokens, 2),
        tokens_harvested_on_free_pt=round(harvested_free, 2),
        overflow_tokens_to_flex_paygo=round(overflow_tokens, 2),
        batch_api_extra_spend_usd=round(batch_api_extra_usd, 2),
        online_dedicated_scavenger_extra_spend_usd=0.0,
        online_hybrid_flex_scavenger_extra_spend_usd=round(hybrid_flex_extra_usd, 2),
        double_payment_waste_avoided_usd=round(waste_avoided, 2),
        effective_utilization_after_scavenging_pct=round(effective_util_after, 2),
        recommended_header_policy=policy,
    )


def _is_edf_feasible(
    jobs: Sequence[DeadlineJobSpec],
    cum_residual: Sequence[float],
    horizon_hours: int,
) -> bool:
    cum_work = 0.0
    for job in sorted(jobs, key=lambda j: (j.deadline_hour, j.job_id)):
        if job.deadline_hour > horizon_hours:
            return False
        cum_work += job.burndown_tokens
        if cum_work > cum_residual[job.deadline_hour - 1] + 1e-6:
            return False
    return True


def _select_optimal_deadline_subset(
    jobs_in: Sequence[DeadlineJobSpec],
    cum_residual: Sequence[float],
    horizon_hours: int,
) -> set[str]:
    """Select the deadline-feasible subset maximizing SLA-protected business value (`<= 24` B&B)."""
    eligible = [j for j in jobs_in if 1 <= j.deadline_hour <= horizon_hours]
    if not eligible:
        return set()

    if len(eligible) > 24:
        ranked = sorted(
            eligible,
            key=lambda j: (-(j.value_usd / max(1.0, j.burndown_tokens)), j.deadline_hour),
        )
        chosen: list[DeadlineJobSpec] = []
        for cand in ranked:
            if _is_edf_feasible([*chosen, cand], cum_residual, horizon_hours):
                chosen.append(cand)
        return {j.job_id for j in chosen}

    edf_jobs = sorted(eligible, key=lambda j: (j.deadline_hour, -j.value_usd, j.job_id))
    gains = [j.value_usd for j in edf_jobs]
    suffix_gain = [0.0] * (len(edf_jobs) + 1)
    for idx in range(len(edf_jobs) - 1, -1, -1):
        suffix_gain[idx] = suffix_gain[idx + 1] + gains[idx]

    best_gain = -1.0
    best_tokens = float("inf")
    best_ids: set[str] = set()
    current_ids: list[str] = []

    def _dfs(idx: int, cum_tokens: float, acc_gain: float) -> None:
        nonlocal best_gain, best_tokens, best_ids
        if acc_gain + suffix_gain[idx] < best_gain - 1e-9:
            return
        if idx == len(edf_jobs):
            if acc_gain > best_gain + 1e-9 or (
                abs(acc_gain - best_gain) <= 1e-9 and cum_tokens < best_tokens
            ):
                best_gain = acc_gain
                best_tokens = cum_tokens
                best_ids = set(current_ids)
            return

        job = edf_jobs[idx]
        if cum_tokens + job.burndown_tokens <= cum_residual[job.deadline_hour - 1] + 1e-6:
            current_ids.append(job.job_id)
            _dfs(idx + 1, cum_tokens + job.burndown_tokens, acc_gain + gains[idx])
            current_ids.pop()
        _dfs(idx + 1, cum_tokens, acc_gain)

    _dfs(0, 0.0, 0.0)
    return best_ids


def schedule_residual_capacity(
    provisioned_gsus: int = 20,
    tpm_per_gsu: int = 120_900,
    safety_headroom_ratio: float = 0.85,
    revenue_tpm_by_hour: Sequence[float] | None = None,
    deadline_jobs: Sequence[DeadlineJobSpec] | None = None,
    evergreen_backfill_tokens: float = 250_000_000.0,
    paygo_per_1m_burndown_tokens_usd: float = 0.50,
) -> ResidualScheduleReport:
    """Schedule Tier-0 Revenue, Tier-1 Hard-Deadline (EDF + Exact Admission), & Tier-2 Evergreen."""
    gsus = max(1, int(provisioned_gsus))
    gsu_cap = max(1, int(tpm_per_gsu))
    safety = min(1.0, max(0.1, float(safety_headroom_ratio)))
    rev_series = (
        [max(0.0, float(v)) for v in revenue_tpm_by_hour]
        if revenue_tpm_by_hour is not None
        else list(DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR)
    )
    if not rev_series:
        rev_series = list(DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR)
    horizon_hours = len(rev_series)

    hourly_ceiling_tokens = gsus * gsu_cap * 60.0 * safety
    residual_by_hour = [
        max(0.0, hourly_ceiling_tokens - (rev_tpm * 60.0)) for rev_tpm in rev_series
    ]
    total_residual_tokens = sum(residual_by_hour)
    cum_residual: list[float] = []
    running = 0.0
    for r_h in residual_by_hour:
        running += r_h
        cum_residual.append(running)

    jobs_in = list(deadline_jobs) if deadline_jobs is not None else make_default_deadline_jobs()
    flex_rate_per_1m = float(paygo_per_1m_burndown_tokens_usd) * SPILLOVER_MULTIPLIERS["flex"]
    admitted_ids = _select_optimal_deadline_subset(jobs_in, cum_residual, horizon_hours)

    ordered_specs = sorted(
        jobs_in,
        key=lambda j: (0 if j.job_id in admitted_ids else 1, j.deadline_hour, j.job_id),
    )

    scheduled_rows: list[ScheduledDeadlineJob] = []
    admitted_tokens = 0.0
    spilled_tokens = 0.0
    total_value = 0.0
    flex_spill_cost = 0.0

    for spec in ordered_specs:
        density = (spec.value_usd / max(1.0, spec.burndown_tokens)) * 1_000_000.0
        if spec.job_id in admitted_ids:
            admitted_tokens += spec.burndown_tokens
            total_value += spec.value_usd
            scheduled_rows.append(
                ScheduledDeadlineJob(
                    job_id=spec.job_id,
                    value_usd=round(spec.value_usd, 2),
                    burndown_tokens=round(spec.burndown_tokens, 2),
                    deadline_hour=spec.deadline_hour,
                    value_density_per_1m_tokens=round(density, 4),
                    status="admitted_edf_pt",
                    routing_headers={"X-Vertex-AI-LLM-Request-Type": "dedicated"},
                    marginal_cost_usd=0.0,
                )
            )
        else:
            spilled_tokens += spec.burndown_tokens
            job_flex_cost = (spec.burndown_tokens / 1_000_000.0) * flex_rate_per_1m
            if spec.value_usd > job_flex_cost:
                total_value += spec.value_usd - job_flex_cost
                flex_spill_cost += job_flex_cost
                # Omit X-Vertex-AI-LLM-Request-Type so spare PT drains first before 0.5x Flex spill
                headers = {"X-Vertex-AI-LLM-Shared-Request-Type": "flex"}
                cost_val = job_flex_cost
            else:
                headers = {"X-Gateway-Admission": "aborted-early-low-density"}
                cost_val = 0.0
            scheduled_rows.append(
                ScheduledDeadlineJob(
                    job_id=spec.job_id,
                    value_usd=round(spec.value_usd, 2),
                    burndown_tokens=round(spec.burndown_tokens, 2),
                    deadline_hour=spec.deadline_hour,
                    value_density_per_1m_tokens=round(density, 4),
                    status="spilled_or_aborted",
                    routing_headers=headers,
                    marginal_cost_usd=round(cost_val, 2),
                )
            )

    remaining_residual = max(0.0, total_residual_tokens - admitted_tokens)
    waterfilled_evergreen = min(max(0.0, float(evergreen_backfill_tokens)), remaining_residual)

    return ResidualScheduleReport(
        provisioned_gsus=gsus,
        safety_headroom_ratio=round(safety, 2),
        horizon_hours=horizon_hours,
        total_residual_pt_tokens=round(total_residual_tokens, 2),
        admitted_deadline_tokens=round(admitted_tokens, 2),
        spilled_deadline_tokens=round(spilled_tokens, 2),
        evergreen_waterfilled_tokens=round(waterfilled_evergreen, 2),
        total_value_captured_usd=round(total_value, 2),
        flex_spillover_cost_usd=round(flex_spill_cost, 2),
        jobs=scheduled_rows,
    )


def evaluate_folder_pooling(
    hourly_tpm_by_project: Mapping[str, Sequence[float]],
    model: str = "gemini-3.8-flash",
    tpm_per_gsu: int = 120_900,
    percentile_target: int = 90,
    gsu_monthly_price_usd: float = 2_000.0,
) -> FolderPoolingReport:
    """Quantify GSU savings from pooling siloed spoke projects under a flat Folder RHPT pool."""
    gsu_cap = max(1, int(tpm_per_gsu))
    pct = min(100, max(1, int(percentile_target)))

    normalized: dict[str, list[float]] = {}
    max_len = 1
    for proj, series in hourly_tpm_by_project.items():
        arr = [max(0.0, float(v)) for v in series] or [0.0]
        normalized[proj] = arr
        max_len = max(max_len, len(arr))

    if not normalized:
        normalized = {"default-project": [0.0]}

    for proj, arr in list(normalized.items()):
        if len(arr) < max_len:
            normalized[proj] = arr + [0.0] * (max_len - len(arr))

    siloed_by_proj: dict[str, int] = {}
    for proj in sorted(normalized):
        sorted_proj = sorted(normalized[proj])
        p_tpm = percentile(sorted_proj, pct)
        siloed_by_proj[proj] = math.ceil(p_tpm / gsu_cap) if p_tpm > 0 else 0

    siloed_total = sum(siloed_by_proj.values())

    combined_series = sorted(
        sum(normalized[proj][i] for proj in normalized) for i in range(max_len)
    )
    pooled_p_tpm = percentile(combined_series, pct)
    pooled_gsus = math.ceil(pooled_p_tpm / gsu_cap) if pooled_p_tpm > 0 else 0

    stranded_saved = max(0, siloed_total - pooled_gsus)
    diversity = round(siloed_total / pooled_gsus, 2) if pooled_gsus > 0 else 1.0
    monthly_savings = stranded_saved * float(gsu_monthly_price_usd)

    notes = [
        "Place spoke projects as direct children of a single flat Prod-GenAI Folder "
        "(nested subfolders do not inherit Folder RHPT).",
        "Assign an independent GCP Project Spend Cap to each spoke project so a runaway loop "
        "in one business unit only triggers HTTP 403 on its own spoke project.",
        "Aggregate child consumed_token_throughput in the Folder Host Project Metrics Scope "
        "against the folder's dedicated_token_limit.",
    ]

    return FolderPoolingReport(
        model=model,
        percentile_target=pct,
        project_count=len(normalized),
        siloed_gsus_by_project=siloed_by_proj,
        siloed_total_gsus=siloed_total,
        pooled_folder_gsus=pooled_gsus,
        stranded_gsus_saved=stranded_saved,
        coincidence_diversity_factor=diversity,
        monthly_savings_usd=round(monthly_savings, 2),
        annual_savings_usd=round(monthly_savings * 12.0, 2),
        governance_notes=notes,
    )


def compute_finops_kpis(
    total_amortized_cost_usd: float = 24_500.0,
    uncached_input_tokens: int = 1_200_000_000,
    cached_input_tokens: int = 3_800_000_000,
    delivered_output_tokens: int = 420_000_000,
    thinking_tokens: int = 180_000_000,
    retry_and_judge_tokens: int = 95_000_000,
    verified_outcomes: int = 140_000,
    hourly_reserved_burndown_capacity: float = 72_540_000.0,
    weekly_consumed_burndown_tokens: float = 6_950_000_000.0,
    cached_weight: float = 0.10,
    output_weight: float = 4.0,
) -> FinOpsKpiScorecard:
    """Compute the 5 standardized FinOps for AI KPIs (bridging CPVO to bq-eval)."""
    cost = max(0.0, float(total_amortized_cost_usd))
    u_in = max(0, int(uncached_input_tokens))
    c_in = max(0, int(cached_input_tokens))
    out_tok = max(0, int(delivered_output_tokens))
    think_tok = max(0, int(thinking_tokens))
    retry_tok = max(0, int(retry_and_judge_tokens))
    outcomes = max(1, int(verified_outcomes))

    weighted_burndown = (
        float(u_in)
        + (float(c_in) * cached_weight)
        + (float(out_tok + think_tok + retry_tok) * output_weight)
    )
    cpwt_1m = (cost / (weighted_burndown / 1_000_000.0)) if weighted_burndown > 0 else 0.0
    cpvo = cost / outcomes

    total_raw = u_in + c_in + out_tok + think_tok + retry_tok
    ter_ratio = (out_tok / total_raw) if total_raw > 0 else 0.0

    total_prompt = u_in + c_in
    csr_pct = (
        (c_in / total_prompt) * (1.0 - cached_weight) * 100.0 if total_prompt > 0 else 0.0
    )

    weekly_reserved_capacity = max(1.0, float(hourly_reserved_burndown_capacity) * 168.0)
    ecur_168h_pct = min(
        100.0, (max(0.0, float(weekly_consumed_burndown_tokens)) / weekly_reserved_capacity) * 100.0
    )

    return FinOpsKpiScorecard(
        cpwt_1m_usd=round(cpwt_1m, 4),
        cpvo_usd=round(cpvo, 4),
        ter_ratio=round(ter_ratio, 4),
        ter_pct=round(ter_ratio * 100.0, 2),
        csr_pct=round(csr_pct, 2),
        ecur_168h_pct=round(ecur_168h_pct, 2),
        total_amortized_cost_usd=round(cost, 2),
        total_weighted_burndown_tokens=round(weighted_burndown, 2),
        verified_outcomes=outcomes,
    )
