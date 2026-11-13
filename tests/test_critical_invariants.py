"""One exact-value test per rule in CRITICAL.md and the ADRs.

Each test is written so that inverting the guard it names (a sign, a boundary, a clamp) fails
it. The benchmark receipt's source hash cannot do that job: it fails on any edit, and one
command resets it.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from google.cloud import monitoring_v3

from capacity import finops
from capacity.burst import compute_weighted_burndown_tokens, evaluate_micro_burst_window
from capacity.capacity import (
    DEFAULT_GSU_CONFIG,
    compute_recommendations,
    evaluate_gsu_optimization,
    find_capacity,
)
from capacity.core import (
    GROUP_BY_FIELDS,
    PARTNER_GROUP_BY_FIELDS,
    SELF_HOSTED_GROUP_BY_FIELDS,
    fetch_throughput,
)
from capacity.finops import (
    MINUTES_PER_MONTH,
    compute_focus_chargeback,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.models import DeadlineJobSpec, OptimizeRequest
from capacity.telemetry_parsers import _normalize_epoch_seconds
from capacity.telemetry_parsers import parse_inference_server_telemetry as parse

GSU_TPM = 120_900
PROMPT = 'vllm:prompt_tokens_total{model_name="llama-3.3-70b-fp8"}'


class _NoSeries:
    def __aiter__(self) -> _NoSeries:
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_every_live_query_sums_series_server_side() -> None:
    """CRITICAL #3: multi-region series are summed per minute before any percentile."""
    requests: list[dict[str, Any]] = []

    class _Client:
        async def list_time_series(self, request: dict[str, Any]) -> _NoSeries:
            requests.append(request)
            return _NoSeries()

    with patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_Client()):
        await fetch_throughput("demo-project", 1, models=["gemini-3.8-flash", "claude-sonnet-4-6"])
        await fetch_throughput("demo-project", 1, mode="self-hosted")

    for request in requests:
        agg = request["aggregation"]
        assert agg.cross_series_reducer == monitoring_v3.Aggregation.Reducer.REDUCE_SUM
        assert agg.per_series_aligner == monitoring_v3.Aggregation.Aligner.ALIGN_SUM
        assert agg.alignment_period.seconds == 60
    assert [list(request["aggregation"].group_by_fields) for request in requests] == [
        GROUP_BY_FIELDS,
        GROUP_BY_FIELDS,
        PARTNER_GROUP_BY_FIELDS,
        SELF_HOSTED_GROUP_BY_FIELDS,
    ]


def test_window_ceiling_is_the_committed_rate_plus_five_percent() -> None:
    """CRITICAL #5: 10 GSUs commit 20,150 tokens/s and may burst to 21,157.5, not below."""
    inside_credit = evaluate_micro_burst_window(600_000, 21_000, 10, tps_per_gsu=2_015.0)
    assert inside_credit.committed_tps_ceiling == 20_150.0
    assert inside_credit.effective_window_tps_ceiling == 21_157.5
    assert inside_credit.window_token_budget == 211_575.0
    assert inside_credit.triggers_429_or_spillover is False

    past_credit = evaluate_micro_burst_window(600_000, 21_200, 10, tps_per_gsu=2_015.0)
    assert past_credit.triggers_429_or_spillover is True
    assert past_credit.burst_spillover_tps == 42.5


def test_bucket_window_is_ten_seconds_below_fifty_gsus_and_one_second_from_fifty() -> None:
    """CRITICAL #5: the refill window switches at exactly 50 GSUs."""
    windows = [
        evaluate_micro_burst_window(600_000, 20_000, gsus).refill_window_seconds
        for gsus in (49, 50)
    ]
    assert windows == [10, 1]


def test_measured_ten_second_peak_replaces_the_one_second_spike() -> None:
    with_measurement = evaluate_micro_burst_window(600_000, 50_000, 10, peak_10s_tps=5_000.0)
    without = evaluate_micro_burst_window(600_000, 50_000, 10)
    assert with_measurement.peak_window_tps == 10_000.0
    assert without.peak_window_tps == 50_000.0


def test_claude_buckets_are_thirty_and_sixty_seconds_with_no_burst_credit() -> None:
    audit = evaluate_micro_burst_window(
        600_000, 20_000, 10, tps_per_gsu=350.0, model="claude-sonnet-4-6"
    )
    assert (audit.refill_window_seconds, audit.long_window_seconds) == (30, 60)
    assert audit.burst_credit_pct == 0.0
    assert audit.effective_window_tps_ceiling == audit.committed_tps_ceiling == 3_500.0


def test_long_context_rate_starts_above_200k_input_tokens() -> None:
    """CRITICAL #5: 1x/0.1x/4x up to and including 200,000 input tokens, 2x/0.2x/7.5x above."""
    assert compute_weighted_burndown_tokens(200_000, 1_000) == 204_000.0
    assert compute_weighted_burndown_tokens(200_001, 1_000) == 407_502.0
    assert compute_weighted_burndown_tokens(250_000, 0, cached_tokens=50_000) == 410_000.0


def test_claude_burndown_is_flat_and_prices_cache_writes() -> None:
    assert compute_weighted_burndown_tokens(250_000, 1_000, model="claude-sonnet-4-6") == 255_000.0
    with_writes = compute_weighted_burndown_tokens(
        10_000,
        1_000,
        cached_tokens=2_000,
        model="claude-sonnet-4-6",
        cache_write_5m_tokens=1_000,
        cache_write_1h_tokens=1_000,
    )
    assert with_writes == 14_450.0


def test_water_fill_reclaims_spare_capacity_and_never_more() -> None:
    """CRITICAL #6: batch demand above the spare is capped at the spare, to the token."""
    demand_above_spare = compute_focus_chargeback(
        {"a": [90_000.0]}, provisioned_gsus=1, tpm_per_gsu=GSU_TPM, async_batch_waterfill_tpm=5e5
    )
    assert demand_above_spare.waterfill_reclaimed_tokens == (GSU_TPM - 90_000) * MINUTES_PER_MONTH
    assert demand_above_spare.effective_utilization_with_waterfill_pct == 100.0
    assert demand_above_spare.reclaimed_waterfill_cost_usd == 511.17
    assert demand_above_spare.net_unallocated_idle_cost_usd == 0.0

    demand_below_spare = compute_focus_chargeback(
        {"a": [90_000.0]}, provisioned_gsus=1, tpm_per_gsu=GSU_TPM, async_batch_waterfill_tpm=1e4
    )
    assert demand_below_spare.waterfill_reclaimed_tokens == 10_000 * MINUTES_PER_MONTH
    assert demand_below_spare.reclaimed_waterfill_cost_usd == 165.43
    assert demand_below_spare.net_unallocated_idle_cost_usd == 345.74


def test_active_pool_is_split_by_weighted_burndown_not_raw_tokens() -> None:
    """CRITICAL #6: twice the weighted tokens costs twice as much, whatever the raw token count."""
    report = compute_focus_chargeback(
        {"a": [60_450.0], "b": [30_225.0]},
        provisioned_gsus=1,
        tpm_per_gsu=GSU_TPM,
        tenant_raw_token_weights={"a": 2.0, "b": 0.5},
    )
    a, b = report.tenants
    assert report.active_consumed_cost_usd == 1_500.0
    assert (a.active_allocated_cost_usd, b.active_allocated_cost_usd) == (1_000.0, 500.0)
    assert (a.usage_share_pct, b.usage_share_pct) == (66.67, 33.33)
    assert (a.naive_full_pool_cost_usd, b.naive_full_pool_cost_usd) == (1_333.33, 666.67)
    assert (a.unweighted_raw_token_cost_usd, b.unweighted_raw_token_cost_usd) == (500.0, 1_000.0)


def test_active_and_idle_pools_add_up_to_the_commitment() -> None:
    """CRITICAL #6: the two pools split the invoice; neither is lost or counted twice."""
    report = compute_focus_chargeback({"a": [90_000.0]}, provisioned_gsus=1, tpm_per_gsu=GSU_TPM)
    assert report.active_utilization_pct == 74.44
    assert report.active_consumed_cost_usd == 1_488.83
    assert report.unallocated_idle_cost_before_waterfill_usd == 511.17
    assert report.total_commitment_cost_usd == 2_000.0


@pytest.mark.parametrize(
    ("options", "gsu_price", "spill_per_1m"),
    [
        ({}, 2_000.0, 0.9),
        ({"fsp_discount_pct": 15.0}, 1_700.0, 0.9),
        ({"fsp_discount_pct": 80.0}, 1_000.0, 0.9),
        ({"spillover_tier": "standard"}, 2_000.0, 0.5),
        ({"spillover_tier": "flex"}, 2_000.0, 0.25),
    ],
)
def test_discount_lowers_the_price_and_tiers_scale_the_spillover_rate(
    options: dict[str, Any], gsu_price: float, spill_per_1m: float
) -> None:
    rec = optimize_hybrid_commitment([241_800.0] * 10, tpm_per_gsu=GSU_TPM, **options)
    assert rec.effective_gsu_monthly_price_usd == gsu_price
    assert rec.effective_spillover_per_1m_tokens_usd == spill_per_1m


def test_break_even_is_the_price_over_a_full_month_of_priority_spillover() -> None:
    rec = optimize_hybrid_commitment([241_800.0] * 10, tpm_per_gsu=GSU_TPM)
    assert rec.breakeven_utilization_pct == 42.55
    assert (rec.optimal_gsus, rec.total_hybrid_monthly_cost_usd) == (2, 4_000.0)


def _monthly_cost(gsus: int, series: list[float]) -> float:
    spill_tpm = sum(max(0.0, x - gsus * GSU_TPM) for x in series) / len(series)
    return gsus * 2_000.0 + spill_tpm * MINUTES_PER_MONTH / 1e6 * 0.9


def test_commitment_is_the_exact_optimum_above_500_gsus() -> None:
    """A coarse candidate grid returned 1,823 GSUs here, 62,002 USD a month over the optimum."""
    rec = optimize_hybrid_commitment(
        [GSU_TPM * k for k in (120, 1_000, 2_000, 3_000)], tpm_per_gsu=GSU_TPM
    )
    assert rec.optimal_gsus == 2_000
    assert rec.total_hybrid_monthly_cost_usd == 5_175_148.0


def test_commitment_matches_brute_force_on_heavy_tailed_series() -> None:
    rng = random.Random(7)  # noqa: S311  (seeded test data, not a secret)
    for _ in range(25):
        series = [rng.lognormvariate(0, 1.0) * GSU_TPM * 300 for _ in range(rng.randint(4, 12))]
        ceiling = math.ceil(max(series) / GSU_TPM)
        exact = min(range(ceiling + 1), key=lambda g: _monthly_cost(g, series))
        assert optimize_hybrid_commitment(series, tpm_per_gsu=GSU_TPM).optimal_gsus == exact


def _blocking_jobs(fillers: int) -> list[DeadlineJobSpec]:
    """Greedy takes job-a (best value per token) and so loses job-b plus job-c."""
    jobs = [
        DeadlineJobSpec(job_id="job-a", value_usd=520.0, burndown_tokens=61e6, deadline_hour=1),
        DeadlineJobSpec(job_id="job-b", value_usd=500.0, burndown_tokens=60e6, deadline_hour=1),
        DeadlineJobSpec(job_id="job-c", value_usd=500.0, burndown_tokens=60e6, deadline_hour=1),
    ]
    return jobs + [
        DeadlineJobSpec(job_id=f"too-big-{i}", value_usd=1.0, burndown_tokens=1e12, deadline_hour=1)
        for i in range(fillers)
    ]


@pytest.mark.parametrize(("job_count", "admitted"), [(24, {"job-b", "job-c"}), (25, {"job-a"})])
def test_admission_is_exact_up_to_24_jobs_and_greedy_from_25(
    job_count: int, admitted: set[str]
) -> None:
    report = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=GSU_TPM,
        revenue_tpm_by_hour=[(123_318_000.0 - 120_000_000.0) / 60.0],
        deadline_jobs=_blocking_jobs(job_count - 3),
    )
    assert {j.job_id for j in report.jobs if j.status == "admitted_edf_pt"} == admitted


@pytest.mark.parametrize("node_budget", [250_000, 50])
def test_equal_value_jobs_fill_the_capacity_within_the_search_budget(
    monkeypatch: pytest.MonkeyPatch, node_budget: int
) -> None:
    """24 tied jobs would cost 2^24 node visits; the budget stops the search, not the answer."""
    monkeypatch.setattr(finops, "_EXACT_SEARCH_NODE_BUDGET", node_budget)
    tied = [
        DeadlineJobSpec(job_id=f"j{i:02d}", value_usd=100.0, burndown_tokens=1e6, deadline_hour=8)
        for i in range(24)
    ]
    report = schedule_residual_capacity(
        provisioned_gsus=1,
        tpm_per_gsu=25_001,
        safety_headroom_ratio=1.0,
        revenue_tpm_by_hour=[0.0] * 8,
        deadline_jobs=tied,
    )
    assert report.total_residual_pt_tokens == 12_000_480.0
    assert sum(j.status == "admitted_edf_pt" for j in report.jobs) == 12


def test_search_stops_at_its_node_budget_and_falls_back_to_the_greedy_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget spent before the search reaches job-b plus job-c leaves greedy's job-a standing."""
    monkeypatch.setattr(finops, "_EXACT_SEARCH_NODE_BUDGET", 3)
    report = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=GSU_TPM,
        revenue_tpm_by_hour=[(123_318_000.0 - 120_000_000.0) / 60.0],
        deadline_jobs=_blocking_jobs(0),
    )
    assert {j.job_id for j in report.jobs if j.status == "admitted_edf_pt"} == {"job-a"}


@pytest.mark.parametrize(
    ("tokens", "status"), [(7_254_000.0, "admitted_edf_pt"), (7_254_001.0, "spilled_or_aborted")]
)
def test_job_is_admitted_only_if_it_fits_by_its_deadline(tokens: float, status: str) -> None:
    report = schedule_residual_capacity(
        provisioned_gsus=1,
        tpm_per_gsu=GSU_TPM,
        safety_headroom_ratio=1.0,
        revenue_tpm_by_hour=[0.0],
        deadline_jobs=[
            DeadlineJobSpec(job_id="x", value_usd=1e6, burndown_tokens=tokens, deadline_hour=1)
        ],
    )
    assert report.jobs[0].status == status


@pytest.mark.parametrize(("forecast_hours", "peak_tpm"), [(24, 600_000), (72, 800_000)])
def test_forecast_extends_the_daily_peak_trend_by_the_requested_days(
    forecast_hours: int, peak_tpm: int
) -> None:
    first_day = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    rising = {first_day + timedelta(days=d): 100_000 * (d + 1) for d in range(5)}
    rec = compute_recommendations(
        {"gemini-3.8-flash": rising}, 5 * 1_440, forecast_steps=forecast_hours
    )[0]
    assert rec.forecast_24h_peak_tpm == peak_tpm


@pytest.mark.parametrize(
    ("model_id", "tpm"),
    [
        ("gemini-3.8-flash-lite", None),
        ("gemini-4-argon-mini", None),
        ("gemini-2.5-flash-preview-09-2025", 161_400),
        ("gemini-2.5-pro-exp-03-25", 39_000),
        ("gemini-2.5-pro-preview-06-05", 39_000),
        ("gemini-3.8-flash-exp-0827", 120_900),
        ("publishers/google/models/gemini-3.8-flash-001@us-central1", 120_900),
    ],
)
def test_capacity_lookup_never_borrows_a_neighbours_rate(model_id: str, tpm: int | None) -> None:
    """ADR-001: a variant that is not listed is unknown, even if its name extends a listed one."""
    assert find_capacity(model_id, dict(DEFAULT_GSU_CONFIG)) == tpm


def test_optimize_without_burst_inputs_gives_no_burst_verdict() -> None:
    small = evaluate_gsu_optimization(OptimizeRequest(tpm_series=[1_000.0] * 5))
    large = evaluate_gsu_optimization(OptimizeRequest(tpm_series=[60_000_000.0] * 5))
    assert small.micro_burst is None
    assert large.micro_burst is None
    assert (small.prefix_cache.sticky_gsus, small.prefix_cache.round_robin_gsus) == (1, 1)
    assert (large.prefix_cache.sticky_gsus, large.prefix_cache.round_robin_gsus) == (363, 795)


def test_burst_audit_reads_the_trace_peak_when_only_a_spike_is_given() -> None:
    resp = evaluate_gsu_optimization(
        OptimizeRequest(tpm_series=[400_000.0] * 5, peak_1s_tps=9_000.0)
    )
    burst = resp.micro_burst
    assert burst is not None
    assert resp.commitment.optimal_gsus == 3
    assert burst.sixty_sec_average_tps == 6_666.67
    assert burst.apparent_60s_utilization_pct == 110.28
    assert burst.peak_window_tps == 9_000.0
    assert (burst.triggers_429_or_spillover, burst.illusion_detected) == (True, False)


def test_missing_spike_falls_back_to_the_documented_peak_to_mean_ratio() -> None:
    """ADR-002: a 60-second load without a measured peak assumes a 1.9x peak."""
    resp = evaluate_gsu_optimization(
        OptimizeRequest(tpm_series=[400_000.0] * 5, sixty_sec_tpm=300_000.0)
    )
    burst = resp.micro_burst
    assert burst is not None
    assert burst.sixty_sec_average_tps == 5_000.0
    assert burst.peak_window_tps == 9_500.0


def test_explicit_request_rate_overrides_the_rate_implied_by_the_trace() -> None:
    resp = evaluate_gsu_optimization(
        OptimizeRequest(tpm_series=[400_000.0] * 5, requests_per_second=4.0)
    )
    assert (resp.prefix_cache.sticky_gsus, resp.prefix_cache.round_robin_gsus) == (17, 37)
    assert resp.prefix_cache.requests_per_second == 4.0


@pytest.mark.parametrize(
    ("guard", "scrape", "tpm"),
    [
        ("uptime scales a single scrape", f"{PROMPT} 120000\nprocess_uptime_seconds 600\n", 12_000),
        (
            "process start bounds the window",
            f"process_start_time_seconds 1762600000\n{PROMPT} 120000 1762600600000\n",
            12_000,
        ),
        (
            "scrape interval divides the delta",
            f"# scrape_timestamp_seconds 1762600000\n{PROMPT} 100000\n"
            f"# scrape_timestamp_seconds 1762600120\n{PROMPT} 340000\n",
            120_000,
        ),
        (
            "counter reset restarts from zero",
            f"# scrape_timestamp_seconds 1762600000\n{PROMPT} 500000\n"
            f"# scrape_timestamp_seconds 1762600060\n{PROMPT} 60000\n",
            60_000,
        ),
        (
            "sample timestamps split scrapes",
            f"{PROMPT} 100000 1762600000000\n{PROMPT} 160000 1762600060000\n",
            60_000,
        ),
        ("no window information means one minute", f"{PROMPT} 120000\n", 120_000),
    ],
)
def test_prometheus_counter_handling(guard: str, scrape: str, tpm: int) -> None:
    """ADR-006: each cumulative-counter guard, with the exact rate it must produce."""
    assert parse(scrape).tpm_series == [float(tpm)], guard


def test_epoch_is_read_in_seconds_milliseconds_or_nanoseconds() -> None:
    assert _normalize_epoch_seconds(1_762_600_000) == 1_762_600_000
    assert _normalize_epoch_seconds(1_762_600_000_000) == 1_762_600_000
    assert _normalize_epoch_seconds(1_762_600_000_000_000_000) == 1_762_600_000
