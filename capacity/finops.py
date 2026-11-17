"""Newsvendor hybrid commitment sizing, scavenger scheduling, and FOCUS 1.2 FinOps (`ADR-003`)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from capacity.forecasting import argmin_convex_int, percentile
from capacity.model_families import (
    GEMINI_1P_FLASH_SPEC,
    ModelCatalog,
    build_model_catalog,
    normalize_model_id,
)
from capacity.models import (
    BatchScavengerAudit,
    BatchScavengerRequest,
    ChargebackRequest,
    DeadlineJobSpec,
    FinOpsKpiRequest,
    FinOpsKpiScorecard,
    FocusChargebackReport,
    FolderPoolingReport,
    FolderPoolingRequest,
    HybridCommitmentRecommendation,
    OptimizeRequest,
    ResidualScheduleReport,
    ResidualScheduleRequest,
    ScheduledDeadlineJob,
    TenantChargebackRow,
)
from capacity.self_hosted import resolve_self_hosted_profile

# 43,200 minutes in a standard 30-day billing month (30 * 24 * 60).
MINUTES_PER_MONTH = 30 * 24 * 60
# 168 hours in a 7-day reservation week (`ECUR_168h` denominator, ADR-003).
HOURS_PER_WEEK = 168.0
# Default monthly list price per 1 Managed Gemini GSU in USD (ADR-003).
DEFAULT_GSU_MONTHLY_PRICE_USD = 2_000.0
# Maximum fraction of monthly PT capacity (85%, keeping 15% safety headroom) for $0 scavenging.
SCAVENGER_SAFETY_UTILIZATION_CAP = 0.85

SPILLOVER_MULTIPLIERS: dict[str, float] = {
    "priority": 1.8,
    "standard": 1.0,
    "flex": 0.5,
}

_EXACT_SEARCH_MAX_JOBS = 24
# Jobs of equal value defeat the value bound and cost 2^N node visits; caps search at ~50 ms.
_EXACT_SEARCH_NODE_BUDGET = 250_000


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


def _pad_series_matrix(
    raw_by_key: dict[str, list[float] | float],
) -> tuple[dict[str, list[float]], int]:
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


def compute_focus_chargeback(
    req: ChargebackRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> FocusChargebackReport:
    """Compute FinOps FOCUS 1.2 Two-Pool Chargeback + Async Batch Water-Filling."""
    if req.mode == "self-hosted":
        prof = resolve_self_hosted_profile(req.model)
        gsu_cap = prof.tpm_per_replica
        unit_price = (
            req.gsu_monthly_price_usd
            if req.gsu_monthly_price_usd is not None
            else prof.reserved_replica_monthly_usd
        )
    else:
        cat = catalog if catalog is not None else build_model_catalog()
        gsu_cap = cat.require_capacity(req.model)
        unit_price = (
            req.gsu_monthly_price_usd
            if req.gsu_monthly_price_usd is not None
            else DEFAULT_GSU_MONTHLY_PRICE_USD
        )

    gsus = req.provisioned_gsus
    capacity_tpm = float(gsus * gsu_cap)
    total_commitment_cost = gsus * unit_price

    normalized_tenants, max_hours = _pad_series_matrix(req.hourly_consumed_tpm_by_tenant)
    hourly_commit_cost = total_commitment_cost / max_hours
    scale_to_month_minutes = MINUTES_PER_MONTH / max_hours
    waterfill_demand_tpm = req.async_batch_waterfill_tpm

    tenant_active_cost: dict[str, float] = dict.fromkeys(normalized_tenants, 0.0)
    tenant_tokens: dict[str, float] = dict.fromkeys(normalized_tenants, 0.0)

    total_active_cost = 0.0
    total_waterfill_cost = 0.0
    total_waterfill_tokens = 0.0
    sum_active_util = 0.0
    sum_effective_util = 0.0

    for h in range(max_hours):
        hour_total_tpm = sum(normalized_tenants[t][h] for t in normalized_tenants)
        active_util_h = min(1.0, hour_total_tpm / capacity_tpm)
        sum_active_util += active_util_h

        active_cost_h = hourly_commit_cost * active_util_h
        total_active_cost += active_cost_h

        spare_tpm_h = max(0.0, capacity_tpm - min(capacity_tpm, hour_total_tpm))
        waterfilled_tpm_h = min(spare_tpm_h, waterfill_demand_tpm)
        waterfill_util_h = waterfilled_tpm_h / capacity_tpm
        sum_effective_util += active_util_h + waterfill_util_h

        total_waterfill_cost += hourly_commit_cost * waterfill_util_h
        total_waterfill_tokens += waterfilled_tpm_h * scale_to_month_minutes

        for t in normalized_tenants:
            t_tpm = normalized_tenants[t][h]
            tenant_tokens[t] += t_tpm * scale_to_month_minutes
            if hour_total_tpm > 0:
                tenant_active_cost[t] += active_cost_h * (t_tpm / hour_total_tpm)

    total_tokens_all_tenants = sum(tenant_tokens.values())
    raw_weights_cfg = (
        dict(req.tenant_raw_token_weights) if req.tenant_raw_token_weights is not None else {}
    )
    tenant_raw_tokens = {
        t: w_tok / max(0.1, float(raw_weights_cfg.get(t, 1.0)))
        for t, w_tok in tenant_tokens.items()
    }
    total_raw_tokens_all_tenants = sum(tenant_raw_tokens.values())

    tenant_rows: list[TenantChargebackRow] = []
    for t in sorted(normalized_tenants):
        w_share = (
            tenant_tokens[t] / total_tokens_all_tenants if total_tokens_all_tenants > 0 else 0.0
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
        unit_price_usd=round(unit_price, 2),
        mode=req.mode,
        total_commitment_cost_usd=round(total_commitment_cost, 2),
        active_utilization_pct=round((sum_active_util / max_hours) * 100.0, 2),
        effective_utilization_with_waterfill_pct=round((sum_effective_util / max_hours) * 100.0, 2),
        active_consumed_cost_usd=round(total_active_cost, 2),
        unallocated_idle_cost_before_waterfill_usd=round(unallocated_before_wf, 2),
        reclaimed_waterfill_cost_usd=round(total_waterfill_cost, 2),
        net_unallocated_idle_cost_usd=round(net_unallocated, 2),
        waterfill_reclaimed_tokens=round(total_waterfill_tokens, 2),
        tenants=tenant_rows,
    )


def evaluate_batch_scavenger_economics(
    req: BatchScavengerRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> BatchScavengerAudit:
    """Compare Batch API (0.5x extra double-payment) vs $0 online PT scavenging."""
    cat = catalog if catalog is not None else build_model_catalog()
    gsu_cap = cat.require_capacity(req.model)
    norm_model = normalize_model_id(req.model)
    gsus = req.provisioned_gsus
    pt_cost = gsus * req.gsu_monthly_price_usd
    active_util = req.daytime_active_utilization_pct

    total_monthly_pt_tokens = float(gsus * gsu_cap * MINUTES_PER_MONTH)
    active_tokens = total_monthly_pt_tokens * (active_util / 100.0)
    idle_scavengable_tokens = max(
        0.0, (total_monthly_pt_tokens * SCAVENGER_SAFETY_UTILIZATION_CAP) - active_tokens
    )

    batch_tokens = req.batch_monthly_burndown_tokens
    harvested_free = min(batch_tokens, idle_scavengable_tokens)
    overflow_tokens = max(0.0, batch_tokens - harvested_free)

    flex_rate_per_1m = req.paygo_per_1m_burndown_tokens_usd * SPILLOVER_MULTIPLIERS["flex"]
    batch_api_extra_usd = (batch_tokens / 1_000_000.0) * flex_rate_per_1m
    hybrid_flex_extra_usd = (overflow_tokens / 1_000_000.0) * flex_rate_per_1m
    waste_avoided = max(0.0, batch_api_extra_usd - hybrid_flex_extra_usd)

    effective_util_after = ((active_tokens + harvested_free) / total_monthly_pt_tokens) * 100.0
    if overflow_tokens == 0.0:
        policy = "X-Vertex-AI-LLM-Request-Type: dedicated ($0.00 overage guaranteed)"
    else:
        policy = (
            "Omit X-Vertex-AI-LLM-Request-Type + "
            "X-Vertex-AI-LLM-Shared-Request-Type: flex ($0 PT first, 0.5x overflow)"
        )

    return BatchScavengerAudit(
        model=norm_model,
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


def _admit_by_value_density(
    eligible: Sequence[DeadlineJobSpec],
    cum_residual: Sequence[float],
    horizon_hours: int,
) -> list[DeadlineJobSpec]:
    ranked = sorted(
        eligible,
        key=lambda j: (-(j.value_usd / max(1.0, j.burndown_tokens)), j.deadline_hour),
    )
    chosen: list[DeadlineJobSpec] = []
    for cand in ranked:
        if _is_edf_feasible([*chosen, cand], cum_residual, horizon_hours):
            chosen.append(cand)
    return chosen


def _select_optimal_deadline_subset(
    jobs_in: Sequence[DeadlineJobSpec],
    cum_residual: Sequence[float],
    horizon_hours: int,
) -> set[str]:
    """Select the deadline-feasible subset maximizing SLA-protected business value."""
    eligible = [j for j in jobs_in if 1 <= j.deadline_hour <= horizon_hours]
    if not eligible:
        return set()
    if len(eligible) > _EXACT_SEARCH_MAX_JOBS:
        return {j.job_id for j in _admit_by_value_density(eligible, cum_residual, horizon_hours)}

    edf_jobs = sorted(eligible, key=lambda j: (j.deadline_hour, -j.value_usd, j.job_id))
    gains = [j.value_usd for j in edf_jobs]
    suffix_gain = [0.0] * (len(edf_jobs) + 1)
    for idx in range(len(edf_jobs) - 1, -1, -1):
        suffix_gain[idx] = suffix_gain[idx + 1] + gains[idx]

    best_gain = -1.0
    best_tokens = float("inf")
    best_ids: set[str] = set()
    current_ids: list[str] = []
    nodes_visited = 0

    def _dfs(idx: int, cum_tokens: float, acc_gain: float) -> None:
        nonlocal best_gain, best_tokens, best_ids, nodes_visited
        nodes_visited += 1
        if nodes_visited > _EXACT_SEARCH_NODE_BUDGET:
            return
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
    if nodes_visited > _EXACT_SEARCH_NODE_BUDGET:
        greedy = _admit_by_value_density(eligible, cum_residual, horizon_hours)
        if sum(j.value_usd for j in greedy) > best_gain + 1e-9:
            return {j.job_id for j in greedy}
    return best_ids


def schedule_residual_capacity(
    req: ResidualScheduleRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> ResidualScheduleReport:
    """Schedule Tier-0 Revenue, Tier-1 Hard-Deadline (EDF + Exact Admission), & Tier-2 Evergreen."""
    cat = catalog if catalog is not None else build_model_catalog()
    gsu_cap = cat.require_capacity(req.model)
    gsus = req.provisioned_gsus
    safety = req.safety_headroom_ratio
    horizon_hours = len(req.revenue_tpm_by_hour)

    hourly_ceiling_tokens = gsus * gsu_cap * 60.0 * safety
    residual_by_hour = [
        max(0.0, hourly_ceiling_tokens - (rev_tpm * 60.0)) for rev_tpm in req.revenue_tpm_by_hour
    ]
    total_residual_tokens = sum(residual_by_hour)
    cum_residual: list[float] = []
    running = 0.0
    for r_h in residual_by_hour:
        running += r_h
        cum_residual.append(running)

    flex_rate_per_1m = req.paygo_per_1m_burndown_tokens_usd * SPILLOVER_MULTIPLIERS["flex"]
    admitted_ids = _select_optimal_deadline_subset(req.deadline_jobs, cum_residual, horizon_hours)

    ordered_specs = sorted(
        req.deadline_jobs,
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
    waterfilled_evergreen = min(req.evergreen_backfill_tokens, remaining_residual)

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
    req: FolderPoolingRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> FolderPoolingReport:
    """Quantify GSU savings from pooling siloed spoke projects under a flat Folder RHPT pool."""
    cat = catalog if catalog is not None else build_model_catalog()
    record = cat.require(req.model)
    norm_model = normalize_model_id(req.model)
    gsu_cap = record.gsu_capacity_tpm
    pct = req.percentile_target

    raw_projects: dict[str, list[float] | float] = {
        proj: list(series) for proj, series in req.hourly_tpm_by_project.items()
    }
    normalized, max_len = _pad_series_matrix(raw_projects)

    siloed_by_proj: dict[str, int] = {}
    for proj in sorted(normalized):
        p_tpm = percentile(sorted(normalized[proj]), pct)
        siloed_by_proj[proj] = math.ceil(p_tpm / gsu_cap) if p_tpm > 0 else 0

    siloed_total = sum(siloed_by_proj.values())
    combined_series = sorted(
        sum(normalized[proj][i] for proj in normalized) for i in range(max_len)
    )
    pooled_p_tpm = percentile(combined_series, pct)
    pooled_gsus = math.ceil(pooled_p_tpm / gsu_cap) if pooled_p_tpm > 0 else 0

    stranded_saved = max(0, siloed_total - pooled_gsus)
    diversity = round(siloed_total / pooled_gsus, 2) if pooled_gsus > 0 else 1.0
    monthly_savings = stranded_saved * req.gsu_monthly_price_usd

    spec = record.family_spec
    if spec.supports_folder_rhpt:
        notes = [
            "Place spoke projects as direct children of a single flat Prod-GenAI Folder "
            "(nested subfolders do not inherit Folder RHPT).",
            "Assign an independent GCP Project Spend Cap to each spoke project so a runaway loop "
            "in one business unit only triggers HTTP 403 on its own spoke project.",
            "Aggregate child consumed_token_throughput in the Folder Host Project Metrics Scope "
            "against the folder's dedicated_token_limit.",
        ]
    else:
        notes = [
            f"Folder-Level RHPT is 1P Gemini-only and unsupported for {norm_model}; realize these "
            "pooling savings by consolidating traffic into a central BU-segmented hub project "
            "with gateway-enforced per-BU weighted burndown token buckets.",
            "3P Claude Provisioned Throughput uses 30s/60s dual enforcement buckets with no "
            "1-week term and Standard PayGo spillover (Priority PayGo is 1P-only).",
            f"Monitor unweighted {spec.monitoring_metric} grouped by model_id, location, "
            "request_type, and type, applying client-side burndown weights "
            "(1.0x input, 0.1x cache read, 1.25x 5m / 2.0x 1h cache write, 5.0x output).",
        ]

    return FolderPoolingReport(
        model=norm_model,
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


def compute_finops_kpis(req: FinOpsKpiRequest) -> FinOpsKpiScorecard:
    """Compute the 5 standardized FinOps for AI KPIs (bridging CPVO to bq-eval)."""
    cached_w = GEMINI_1P_FLASH_SPEC.standard_cached_read_weight
    out_w = GEMINI_1P_FLASH_SPEC.standard_output_weight

    u_in = req.uncached_input_tokens
    c_in = req.cached_input_tokens
    out_tok = req.delivered_output_tokens
    think_tok = req.thinking_tokens
    retry_tok = req.retry_and_judge_tokens

    weighted_burndown = (
        float(u_in) + (float(c_in) * cached_w) + (float(out_tok + think_tok + retry_tok) * out_w)
    )
    cpwt_1m = (
        (req.total_amortized_cost_usd / (weighted_burndown / 1_000_000.0))
        if weighted_burndown > 0
        else 0.0
    )
    cpvo = req.total_amortized_cost_usd / req.verified_outcomes

    total_raw = u_in + c_in + out_tok + think_tok + retry_tok
    ter_ratio = (out_tok / total_raw) if total_raw > 0 else 0.0

    total_prompt = u_in + c_in
    csr_pct = (c_in / total_prompt) * (1.0 - cached_w) * 100.0 if total_prompt > 0 else 0.0

    weekly_reserved_capacity = req.hourly_reserved_burndown_capacity * HOURS_PER_WEEK
    ecur_168h_pct = min(
        100.0, (req.weekly_consumed_burndown_tokens / weekly_reserved_capacity) * 100.0
    )

    return FinOpsKpiScorecard(
        cpwt_1m_usd=round(cpwt_1m, 4),
        cpvo_usd=round(cpvo, 4),
        ter_ratio=round(ter_ratio, 4),
        ter_pct=round(ter_ratio * 100.0, 2),
        csr_pct=round(csr_pct, 2),
        ecur_168h_pct=round(ecur_168h_pct, 2),
        total_amortized_cost_usd=round(req.total_amortized_cost_usd, 2),
        total_weighted_burndown_tokens=round(weighted_burndown, 2),
        verified_outcomes=req.verified_outcomes,
    )
