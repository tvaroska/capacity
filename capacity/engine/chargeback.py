"""FOCUS 1.2 Two-Pool Chargeback and async batch water-filling (`ADR-003`)."""

from __future__ import annotations

from capacity.contract.requests import ChargebackRequest
from capacity.contract.results import FocusChargebackReport, TenantChargebackRow
from capacity.engine.catalog import ModelCatalog, build_model_catalog
from capacity.engine.commitment import (
    DEFAULT_GSU_MONTHLY_PRICE_USD,
    MINUTES_PER_MONTH,
    pad_series_matrix,
)
from capacity.engine.self_hosted import resolve_self_hosted_profile


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

    normalized_tenants, max_hours = pad_series_matrix(req.hourly_consumed_tpm_by_tenant)
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
