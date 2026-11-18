"""5 Standardized FinOps for AI Unit Economics KPIs (`ADR-003`)."""

from __future__ import annotations

from capacity.contract.requests import FinOpsKpiRequest
from capacity.contract.results import FinOpsKpiScorecard
from capacity.engine.catalog import GEMINI_1P_FLASH_SPEC
from capacity.engine.commitment import HOURS_PER_WEEK


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
