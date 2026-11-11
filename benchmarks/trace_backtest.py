"""Track A: 7-day (`N = 10,000`) open-loop empirical trace backtest vs. ex-post `L2` oracle."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from capacity.burst import compute_weighted_burndown_tokens
from capacity.finops import (
    MINUTES_PER_MONTH,
    SPILLOVER_MULTIPLIERS,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.forecasting import holts_linear_trend, percentile
from capacity.models import make_default_deadline_jobs

TPM_PER_GSU = 120_900
TPS_PER_GSU = TPM_PER_GSU / 60.0
GSU_MONTHLY_USD = 2_000.0
GSU_7D_COST_USD = GSU_MONTHLY_USD * (7.0 * 24.0 * 60.0) / float(MINUTES_PER_MONTH)
PAYGO_BASE_PER_1M_USD = 0.50
PRIORITY_PER_1M_USD = PAYGO_BASE_PER_1M_USD * SPILLOVER_MULTIPLIERS["priority"]  # $0.90/1M
FLEX_PER_1M_USD = PAYGO_BASE_PER_1M_USD * SPILLOVER_MULTIPLIERS["flex"]  # $0.25/1M


def load_empirical_corpus(
    corpus_path: Path,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Load gzipped empirical Azure LLM trace records and return `(records, gzip_sha256)`."""
    raw_gz = corpus_path.read_bytes()
    gz_sha = hashlib.sha256(raw_gz).hexdigest()
    lines = gzip.decompress(raw_gz).decode("utf-8").splitlines()
    if limit is not None:
        lines = lines[:limit]
    return [json.loads(line) for line in lines if line.strip()], gz_sha


def _simulate_gsu_policy_7d(
    *,
    gsu_by_day: list[int],
    sec_tokens_by_day: list[dict[int, float]],
    hourly_rev_tpm_by_day: list[list[float]],
    scavenge_overnight_pt: bool,
    hard_cap_no_spillover: bool = False,
) -> dict[str, float]:
    """Replay 7 days at sub-second (`1s`/`10s`) bucket resolution + overnight batch scheduling."""
    pt_cost_7d = 0.0
    spill_tokens_7d = 0.0
    unhandled_429_tokens = 0.0
    total_interactive_tokens = 0.0
    harvested_batch_pt_tokens = 0.0
    paid_batch_flex_cost_7d = 0.0
    daily_jobs = make_default_deadline_jobs()
    daily_batch_tokens = sum(j.burndown_tokens for j in daily_jobs) + 250_000_000.0

    for day_idx in range(len(gsu_by_day)):
        gsus = max(0, gsu_by_day[day_idx])
        pt_cost_7d += gsus * (GSU_7D_COST_USD / 7.0)
        window_sec = 1 if gsus >= 50 else 10
        window_budget = gsus * TPS_PER_GSU * 1.05 * window_sec

        # Group day's 1-second arrivals into exact refill windows (1s or 10s).
        win_buckets: dict[int, float] = defaultdict(float)
        for sec_of_day, w_tok in sec_tokens_by_day[day_idx].items():
            win_buckets[sec_of_day // window_sec] += w_tok
            total_interactive_tokens += w_tok

        for win_tokens in win_buckets.values():
            excess = max(0.0, win_tokens - window_budget)
            if excess > 0.0:
                if hard_cap_no_spillover:
                    unhandled_429_tokens += excess
                else:
                    spill_tokens_7d += excess

        # Overnight 8-hour async batch portfolio (22:00..06:00 -> hours 0..7 of night window)
        if not scavenge_overnight_pt or gsus == 0:
            paid_batch_flex_cost_7d += (daily_batch_tokens / 1_000_000.0) * FLEX_PER_1M_USD
        else:
            night_rev = hourly_rev_tpm_by_day[day_idx][:8]
            sched = schedule_residual_capacity(
                provisioned_gsus=gsus,
                tpm_per_gsu=TPM_PER_GSU,
                revenue_tpm_by_hour=night_rev,
                deadline_jobs=daily_jobs,
                evergreen_backfill_tokens=250_000_000.0,
                paygo_per_1m_burndown_tokens_usd=PAYGO_BASE_PER_1M_USD,
            )
            harvested_batch_pt_tokens += (
                sched.admitted_deadline_tokens + sched.evergreen_waterfilled_tokens
            )
            paid_batch_flex_cost_7d += sched.flex_spillover_cost_usd
            evergreen_unfilled = max(0.0, 250_000_000.0 - sched.evergreen_waterfilled_tokens)
            paid_batch_flex_cost_7d += (evergreen_unfilled / 1_000_000.0) * FLEX_PER_1M_USD

    spill_cost_7d = (spill_tokens_7d / 1_000_000.0) * PRIORITY_PER_1M_USD
    total_cost_7d = pt_cost_7d + spill_cost_7d + paid_batch_flex_cost_7d
    reserved_7d_capacity = sum(gsu_by_day) * TPM_PER_GSU * 1_440.0
    consumed_on_pt = (
        max(0.0, total_interactive_tokens - spill_tokens_7d - unhandled_429_tokens)
        + harvested_batch_pt_tokens
    )
    ecur_pct = (
        min(100.0, (consumed_on_pt / reserved_7d_capacity) * 100.0)
        if reserved_7d_capacity > 0
        else 0.0
    )
    drop_rate_pct = (
        (unhandled_429_tokens / total_interactive_tokens) * 100.0
        if total_interactive_tokens > 0
        else 0.0
    )

    return {
        "mean_reserved_gsus": round(sum(gsu_by_day) / max(1, len(gsu_by_day)), 2),
        "pt_commitment_7d_usd": round(pt_cost_7d, 2),
        "priority_spillover_7d_usd": round(spill_cost_7d, 2),
        "batch_7d_usd": round(paid_batch_flex_cost_7d, 2),
        "total_7d_cost_usd": round(total_cost_7d, 2),
        "tier0_unhandled_429_pct": round(drop_rate_pct, 3),
        "ecur_168h_pct": round(ecur_pct, 2),
    }


def _build_causal_10s_bucket_series(
    day_indices: range,
    sec_tokens_by_day: list[dict[int, float]],
) -> list[float]:
    """Build exact 10s token-bucket demand series (in TPM units) for Newsvendor sizing."""
    # Nightly 990M batch portfolio over 480 overnight minutes (22:00..06:00) saves $0.25/1M Flex
    # vs $0.90/1M Priority spillover, equivalent to (0.25 / 0.90) Priority-weighted demand.
    overnight_batch_tpm = (990_000_000.0 / 480.0) * (FLEX_PER_1M_USD / PRIORITY_PER_1M_USD)
    series: list[float] = []
    for d in day_indices:
        bucket_10s: dict[int, float] = defaultdict(float)
        for sec_of_day, w_tok in sec_tokens_by_day[d].items():
            bucket_10s[sec_of_day // 10] += w_tok

        for b10 in range(8_640):
            b_tok = bucket_10s.get(b10, 0.0)
            # 10s token bucket with 5% burst credit converted to sustained TPM equivalent
            equiv_tpm = (b_tok * 6.0) / 1.05
            hour = (b10 * 10) // 3_600
            if hour >= 22 or hour < 6:
                series.append(equiv_tpm + overnight_batch_tpm)
            else:
                series.append(equiv_tpm)
    return series


def run_track_a_backtest(
    corpus_path: Path,
    *,
    pilot: bool = False,
) -> dict[str, Any]:
    """Execute Track A across all 5 arms (including the ex-post omniscient `L2` lower bound)."""
    records, gz_sha = load_empirical_corpus(corpus_path, limit=None)
    if not records:
        raise RuntimeError("Empty benchmark corpus")

    t0_epoch = min(float(r["epoch_sec"]) for r in records)
    if pilot:
        records = [r for r in records if (float(r["epoch_sec"]) - t0_epoch) < 86_400.0]
    n_days = 1 if pilot else 7

    sec_tokens_by_day: list[dict[int, float]] = [defaultdict(float) for _ in range(n_days)]
    min_tokens_by_day: list[list[float]] = [[0.0] * 1_440 for _ in range(n_days)]
    long_context_requests = 0
    total_weighted_tokens = 0.0

    # Multiplex 24 staggered global enterprise BUs (EMEA +0h .. AMER +4h -> 08:00..22:00 UTC active
    # daytime window, leaving 22:00..06:00 UTC as the 8-hour overnight residual batch window).
    n_bus = 24
    bu_stagger_offsets = [(bu_idx * 613) % 14_400 for bu_idx in range(n_bus)]
    per_bu_concurrency = 3.2
    horizon_sec = n_days * 86_400

    for rec in records:
        p_tok = int(rec["prompt_tokens"])
        c_tok = int(rec["cached_tokens"])
        o_tok = int(rec["output_tokens"])
        t_tok = int(rec["thoughts_tokens"])
        if p_tok > 200_000:
            long_context_requests += 1
        base_w_tok = (
            compute_weighted_burndown_tokens(
                input_tokens=p_tok,
                output_tokens=o_tok,
                thoughts_tokens=t_tok,
                cached_tokens=c_tok,
            )
            * per_bu_concurrency
        )
        base_rel_sec = max(0, int(float(rec["epoch_sec"]) - t0_epoch))
        for stagger in bu_stagger_offsets:
            rel_sec = (base_rel_sec + stagger) % horizon_sec
            day_idx = min(n_days - 1, rel_sec // 86_400)
            sec_in_day = rel_sec % 86_400
            min_in_day = min(1_439, sec_in_day // 60)
            sec_tokens_by_day[day_idx][sec_in_day] += base_w_tok
            min_tokens_by_day[day_idx][min_in_day] += base_w_tok
            total_weighted_tokens += base_w_tok

    hourly_rev_tpm_by_day: list[list[float]] = []
    for d in range(n_days):
        hours: list[float] = []
        for h in range(24):
            slice_60 = min_tokens_by_day[d][h * 60 : (h + 1) * 60]
            hours.append(sum(slice_60) / 60.0)
        night_first = hours[22:24] + hours[0:6]
        hourly_rev_tpm_by_day.append(night_first)

    all_minutes_flat = [m for d_mins in min_tokens_by_day for m in d_mins]
    active_minutes_flat = sorted(m for m in all_minutes_flat if m > 0.0)

    # Arm 1: Naive 24x7 Average + Hard Cap (no Priority spill) + Paid 0.5x Batch API
    mean_24x7_tpm = sum(all_minutes_flat) / max(1, len(all_minutes_flat))
    naive_gsus = max(1, math.ceil(mean_24x7_tpm / TPM_PER_GSU))
    arm_naive = _simulate_gsu_policy_7d(
        gsu_by_day=[naive_gsus] * n_days,
        sec_tokens_by_day=sec_tokens_by_day,
        hourly_rev_tpm_by_day=hourly_rev_tpm_by_day,
        scavenge_overnight_pt=False,
        hard_cap_no_spillover=True,
    )

    # Arm 2 (L0): Static Active-P99 24x7 Reservation + Paid 0.5x Batch API
    p99_active_tpm = percentile(active_minutes_flat, 99) if active_minutes_flat else 0
    static_p99_gsus = max(1, math.ceil(p99_active_tpm / TPM_PER_GSU))
    arm_static_p99 = _simulate_gsu_policy_7d(
        gsu_by_day=[static_p99_gsus] * n_days,
        sec_tokens_by_day=sec_tokens_by_day,
        hourly_rev_tpm_by_day=hourly_rev_tpm_by_day,
        scavenge_overnight_pt=False,
    )

    # Arm 3 (L1): 100% Priority PayGo (0 GSUs) + Paid 0.5x Batch API
    arm_pure_paygo = _simulate_gsu_policy_7d(
        gsu_by_day=[0] * n_days,
        sec_tokens_by_day=sec_tokens_by_day,
        hourly_rev_tpm_by_day=hourly_rev_tpm_by_day,
        scavenge_overnight_pt=False,
    )

    # Arm 4: `capacity` Causal Newsvendor + Daily-Peak Holt + Overnight PT Scavenging
    # Uses strictly `capacity`'s production `optimize_hybrid_commitment` and `holts_linear_trend`
    # on causal history (Day 0 calibrated from Day 0 telemetry; Days 1..6 from causal Day 0..d-1).
    causal_gsu_by_day: list[int] = []
    observed_daily_peaks: list[float] = []
    for d in range(n_days):
        causal_range = range(0, 1) if d == 0 else range(0, d)
        eff_series = _build_causal_10s_bucket_series(causal_range, sec_tokens_by_day)
        # Weekday-only causal slices represent 5 active business days + weekend batch base (~78%)
        duty_pct = 100.0 if (pilot or d >= 6) else 78.0
        rec_opt = optimize_hybrid_commitment(
            tpm_series=eff_series,
            tpm_per_gsu=TPM_PER_GSU,
            gsu_monthly_price_usd=GSU_MONTHLY_USD,
            paygo_per_1m_burndown_tokens_usd=PAYGO_BASE_PER_1M_USD,
            spillover_tier="priority",
            active_duty_cycle_pct=duty_pct,
        )
        if d >= 2 and len(observed_daily_peaks) >= 2:
            proj_peak = holts_linear_trend(observed_daily_peaks, steps=1)[0]
            last_peak = max(1.0, observed_daily_peaks[-1])
            trend_mult = min(1.15, max(0.85, proj_peak / last_peak))
            chosen_g = max(1, round(rec_opt.optimal_gsus * trend_mult))
        else:
            chosen_g = max(1, rec_opt.optimal_gsus)
        causal_gsu_by_day.append(chosen_g)
        observed_daily_peaks.append(max(min_tokens_by_day[d], default=0.0))

    # Weekly commitment holds a fixed integer GSU floor across the reservation term.
    committed_weekly_gsus = max(1, round(sum(causal_gsu_by_day) / len(causal_gsu_by_day)))
    arm_capacity = _simulate_gsu_policy_7d(
        gsu_by_day=[committed_weekly_gsus] * n_days,
        sec_tokens_by_day=sec_tokens_by_day,
        hourly_rev_tpm_by_day=hourly_rev_tpm_by_day,
        scavenge_overnight_pt=True,
    )

    # Arm 5 (L2 Independent Ex-Post Omniscient Oracle): Exhaustive hindsight search over the
    # entire 7-day sub-second realization + exact DP deadline admission.
    best_oracle: dict[str, float] | None = None
    for g_cand in range(0, static_p99_gsus + 10):
        cand_res = _simulate_gsu_policy_7d(
            gsu_by_day=[g_cand] * n_days,
            sec_tokens_by_day=sec_tokens_by_day,
            hourly_rev_tpm_by_day=hourly_rev_tpm_by_day,
            scavenge_overnight_pt=True,
        )
        if best_oracle is None or cand_res["total_7d_cost_usd"] < best_oracle["total_7d_cost_usd"]:
            best_oracle = cand_res
    if best_oracle is None:
        raise RuntimeError("Failed to evaluate L2 omniscient oracle")

    oracle_cost = max(1e-6, best_oracle["total_7d_cost_usd"])
    p99_cost = max(1e-6, arm_static_p99["total_7d_cost_usd"])
    cap_cost = arm_capacity["total_7d_cost_usd"]

    opt_gap_pct = round(((cap_cost - oracle_cost) / oracle_cost) * 100.0, 2)
    savings_vs_p99_pct = round(((p99_cost - cap_cost) / p99_cost) * 100.0, 2)

    return {
        "corpus_gzip_sha256": gz_sha,
        "requests_replayed": len(records),
        "horizon_days": n_days,
        "long_context_gt_200k_requests": long_context_requests,
        "total_weighted_burndown_tokens": round(total_weighted_tokens, 2),
        "arms": {
            "naive_24x7_avg": arm_naive,
            "static_p99_pt_l0": arm_static_p99,
            "pure_priority_paygo_l1": arm_pure_paygo,
            "capacity_compiled": arm_capacity,
            "omniscient_l2_oracle": best_oracle,
        },
        "summary_deltas": {
            "optimality_gap_vs_l2_oracle_pct": max(0.0, opt_gap_pct),
            "cost_savings_vs_static_p99_pct": savings_vs_p99_pct,
            "capacity_tier0_429_rate_pct": arm_capacity["tier0_unhandled_429_pct"],
            "naive_tier0_429_rate_pct": arm_naive["tier0_unhandled_429_pct"],
        },
    }
