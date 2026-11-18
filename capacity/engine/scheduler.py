"""Batch 0.5x double-payment audit and 3-Tier EDF residual capacity scheduler (`ADR-003`)."""

from __future__ import annotations

from collections.abc import Sequence

from capacity.contract.requests import (
    BatchScavengerRequest,
    DeadlineJobSpec,
    ResidualScheduleRequest,
)
from capacity.contract.results import (
    BatchScavengerAudit,
    ResidualScheduleReport,
    ScheduledDeadlineJob,
)
from capacity.engine.catalog import ModelCatalog, build_model_catalog, normalize_model_id
from capacity.engine.commitment import MINUTES_PER_MONTH, SPILLOVER_MULTIPLIERS

# Maximum fraction of monthly PT capacity (85%, keeping 15% safety headroom) for $0 scavenging.
SCAVENGER_SAFETY_UTILIZATION_CAP = 0.85
_EXACT_SEARCH_MAX_JOBS = 24
# Jobs of equal value defeat the value bound and cost 2^N node visits; caps search at ~50 ms.
_EXACT_SEARCH_NODE_BUDGET = 250_000


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
