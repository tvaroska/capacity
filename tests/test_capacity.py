from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from capacity.api import app as fastapi_app
from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import (
    DEFAULT_GSU_CONFIG,
    build_response,
    compute_recommendations,
    find_capacity,
    load_gsu_config,
)
from capacity.cli import app as cli_app
from capacity.core import (
    GROUP_BY_FIELDS,
    _build_filter,
    build_demo_throughput,
    fetch_throughput,
    holts_linear_trend,
    percentile,
)
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.gateway import audit_litellm_scaling, generate_gateway_bundle
from capacity.model_families import normalize_model_id
from capacity.models import DeadlineJobSpec
from capacity.self_hosted import (
    DEFAULT_SELF_HOSTED_PROFILES,
    evaluate_self_hosted_endpoint,
    load_inference_server_file,
    parse_inference_server_telemetry,
    size_disaggregated_prefill_decode,
)

runner = CliRunner()
client = TestClient(fastapi_app)


class _AsyncSeriesIterator:
    def __init__(self, items: list[SimpleNamespace]) -> None:
        self._items = list(items)
        self._idx = 0

    def __aiter__(self) -> _AsyncSeriesIterator:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


def _make_fake_ts(
    model_id: str,
    location: str,
    request_type: str,
    points: list[tuple[datetime, int]],
    endpoint_id: str | None = None,
) -> SimpleNamespace:
    labels = {"model_id": model_id, "location": location}
    if endpoint_id is not None:
        labels["endpoint_id"] = endpoint_id
    return SimpleNamespace(
        resource=SimpleNamespace(labels=labels),
        metric=SimpleNamespace(labels={"request_type": request_type}),
        points=[
            SimpleNamespace(
                interval=SimpleNamespace(end_time=dt),
                value=SimpleNamespace(int64_value=val, double_value=0.0),
            )
            for dt, val in points
        ],
    )


def test_normalize_model_id_strips_suffixes_and_prefixes() -> None:
    assert normalize_model_id("gemini-2.5-pro-001") == "gemini-2.5-pro"
    assert normalize_model_id("gemini-2.5-flash-002") == "gemini-2.5-flash"
    assert normalize_model_id("gemini-3.1-pro-preview-05-06") == "gemini-3.1-pro"
    assert normalize_model_id("gemini-3.8-flash-rc1") == "gemini-3.8-flash"
    assert normalize_model_id("gemini-4-argon-qcd") == "gemini-4-argon"
    assert normalize_model_id("gemini-4.0-pro-ga") == "gemini-4.0-pro"
    assert normalize_model_id("gemini-3.5-flash-exp-0827") == "gemini-3.5-flash"
    assert (
        normalize_model_id("publishers/google/models/gemini-3.8-flash-001@us-central1")
        == "gemini-3.8-flash"
    )


def test_default_gsu_config_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for required_model, expected_tpm in (
        ("gemini-2.5-pro", 39000),
        ("gemini-2.5-flash", 161400),
        ("gemini-2.5-flash-lite", 484200),
        ("gemini-3.1-pro", 30000),
        ("gemini-3-flash", 120900),
        ("gemini-3.1-flash-lite", 241800),
        ("gemini-3.5-flash", 120900),
        ("gemini-3.5-flash-lite", 241800),
        ("gemini-3.8-flash", 120900),
        ("gemini-4-argon", 30000),
        ("gemini-4.0-pro", 30000),
    ):
        assert DEFAULT_GSU_CONFIG[required_model] == expected_tpm
        assert find_capacity(f"{required_model}-001") == expected_tpm

    monkeypatch.setenv("GSU_CAPACITY_GEMINI_3_8_FLASH", "150000")
    cfg = load_gsu_config()
    assert find_capacity("gemini-3.8-flash-002", cfg) == 150000


def test_active_business_hours_vs_all_hours_percentiles_and_holt_forecast() -> None:
    total_minutes = 7 * 1440
    base_time = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    sparse = {
        "claims/gemini-3.8-flash-001": {
            datetime.fromtimestamp(base_time.timestamp() + i * 60, tz=UTC): 241_800
            for i in range(2400)
        }
    }

    recs_all = compute_recommendations(
        sparse, total_minutes=total_minutes, percentiles=[50, 75, 90], active_only=False
    )
    assert len(recs_all) == 1
    r_all = recs_all[0]
    assert r_all.folder == "claims"
    assert r_all.duty_cycle_pct == pytest.approx(23.81, abs=0.01)
    assert r_all.all_hours["P50"].tpm == 0
    assert r_all.all_hours["P50"].recommended_gsus == 0
    assert r_all.all_hours["P75"].tpm == 0
    assert r_all.active_hours["P50"].tpm == 241_800
    assert r_all.active_hours["P50"].recommended_gsus == 2
    assert r_all.forecast_24h_peak_tpm == 241_800
    assert r_all.forecast_24h_recommended_gsus == 2

    recs_active = compute_recommendations(
        sparse, total_minutes=total_minutes, percentiles=[50, 75, 90], active_only=True
    )
    r_act = recs_active[0]
    assert r_act.percentiles["P50"].tpm == 241_800
    assert r_act.percentiles["P50"].recommended_gsus == 2


def test_holts_linear_trend_forecasting() -> None:
    assert holts_linear_trend([], steps=4) == [0.0, 0.0, 0.0, 0.0]
    assert holts_linear_trend([100.0], steps=2) == [100.0, 100.0]
    upward = holts_linear_trend([100.0, 120.0, 140.0, 160.0], steps=6)
    assert len(upward) == 6
    assert upward[-1] > 160.0


@pytest.mark.asyncio
async def test_fetch_throughput_regional_bucketing_and_folder_map() -> None:
    t0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    fake_dedicated = [
        _make_fake_ts("gemini-3.8-flash", "us-central1", "dedicated", [(t0, 100_000)]),
        _make_fake_ts("gemini-3.8-flash", "europe-west4", "dedicated", [(t0, 50_000)]),
    ]
    fake_shared = [
        _make_fake_ts("gemini-3.8-flash", "us-central1", "shared", [(t0, 20_900)]),
    ]

    captured_requests: list[dict[str, Any]] = []

    class _FakeClient:
        async def list_time_series(self, request: dict[str, Any]) -> _AsyncSeriesIterator:
            captured_requests.append(request)
            if '"dedicated"' in request["filter"]:
                return _AsyncSeriesIterator(fake_dedicated)
            return _AsyncSeriesIterator(fake_shared)

    with patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_FakeClient()):
        sparse_reg, total_mins = await fetch_throughput(
            ["demo-project"],
            days=1,
            by_region=True,
            folder_map={"demo-project": "retail-banking"},
        )

    assert total_mins == 1440
    minute = t0 - timedelta(minutes=1)
    assert sparse_reg["retail-banking/gemini-3.8-flash@us-central1"][minute] == 120_900
    assert sparse_reg["retail-banking/gemini-3.8-flash@europe-west4"][minute] == 50_000
    assert len(captured_requests) == 2
    agg = captured_requests[0]["aggregation"]
    assert list(agg.group_by_fields) == GROUP_BY_FIELDS

    resp = build_response(
        "demo-project",
        1,
        sparse_reg,
        total_mins,
        percentiles=[95],
        active_only=True,
        by_region=True,
    )
    by_reg = {m.region: m for m in resp.models}
    assert by_reg["us-central1"].percentiles["P95"].recommended_gsus == 1
    assert by_reg["us-central1"].folder == "retail-banking"
    assert by_reg["europe-west4"].percentiles["P95"].recommended_gsus == 1


@pytest.mark.asyncio
async def test_fetch_throughput_self_hosted_mode() -> None:
    t0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    fake_preds = [
        _make_fake_ts(
            "llama-3.3-70b-fp8",
            "us-central1",
            "prediction",
            [(t0, 200)],
            endpoint_id="llama-3.3-70b-fp8",
        )
    ]

    class _FakeClient:
        async def list_time_series(self, request: dict[str, Any]) -> _AsyncSeriesIterator:
            return _AsyncSeriesIterator(fake_preds)

    with patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_FakeClient()):
        sparse, total_mins = await fetch_throughput(
            "demo-project",
            days=1,
            by_region=True,
            mode="self-hosted",
            tokens_per_prediction=2_000,
        )

    assert total_mins == 1440
    assert sparse["llama-3.3-70b-fp8@us-central1"][t0 - timedelta(minutes=1)] == 400_000


def test_weighted_burndown_tokens_standard_and_long_context_cliff() -> None:
    std = compute_weighted_burndown_tokens(
        input_tokens=100_000,
        output_tokens=1_000,
        thoughts_tokens=500,
        cached_tokens=80_000,
    )
    assert std == pytest.approx(34_000.0)

    long_ctx = compute_weighted_burndown_tokens(
        input_tokens=250_000,
        output_tokens=1_000,
        thoughts_tokens=1_000,
        cached_tokens=50_000,
    )
    assert long_ctx == pytest.approx(425_000.0)


def test_micro_burst_window_1s_vs_10s_and_60s_illusion() -> None:
    burst_small = evaluate_micro_burst_window(
        sixty_sec_tpm=750_000,
        peak_1s_tps=25_000,
        provisioned_gsus=10,
        tps_per_gsu=2015.0,
    )
    assert burst_small.refill_window_seconds == 10
    assert burst_small.apparent_60s_utilization_pct < 70.0
    assert burst_small.triggers_429_or_spillover is True
    assert burst_small.illusion_detected is True
    assert burst_small.gsus_required_for_zero_spillover == 12

    burst_large = evaluate_micro_burst_window(
        sixty_sec_tpm=5_000_000,
        peak_1s_tps=95_000,
        provisioned_gsus=50,
        tps_per_gsu=2015.0,
    )
    assert burst_large.refill_window_seconds == 1
    assert burst_large.triggers_429_or_spillover is False


def test_prefix_cache_dilution_round_robin_vs_sticky() -> None:
    audit = evaluate_prefix_cache_dilution(
        total_input_tokens=20_000,
        shared_prefix_ratio=0.80,
        baseline_cache_hit_rate=0.80,
        num_round_robin_endpoints=8,
        tps_per_gsu=2015.0,
    )
    assert audit.effective_cache_hit_rate == pytest.approx(0.10)
    assert audit.sticky_gsus == 5
    assert audit.round_robin_gsus == 10
    assert audit.wasted_gsus == 5
    assert audit.gsu_savings_pct > 54.0


def test_self_hosted_endpoint_audit_and_phase2_parsers() -> None:
    assert "llama-3.3-70b-fp8" in DEFAULT_SELF_HOSTED_PROFILES
    audit = evaluate_self_hosted_endpoint(
        tpm_series=[520_000, 480_000, 380_000, 290_000, 180_000],
        profile="llama-3.3-70b-fp8",
        min_replica_count=2,
        max_replica_count=8,
        observed_accelerator_duty_cycle_pct=52.0,
        observed_vllm_kv_cache_usage_pct=91.5,
        observed_vllm_requests_waiting=14.0,
    )
    assert audit.locked_regional_accelerators == 64
    assert audit.active_baseline_accelerators == 16
    assert audit.stranded_quota_accelerators == 48
    assert audit.scale_to_zero_nonprod_savings_usd == pytest.approx(
        2 * 38.8 * 720 * 108 / 168, abs=0.01
    )
    assert audit.duty_cycle_illusion_detected is True
    assert audit.kv_preemption_risk is True
    assert audit.optimal_reserved_replicas >= 1
    assert audit.scale_to_zero_nonprod_savings_usd > 0.0

    repo_root = Path(__file__).resolve().parents[1]
    prom_snap = load_inference_server_file(repo_root / "examples" / "vllm_prometheus_metrics.txt")
    assert prom_snap.source_format == "prometheus"
    assert prom_snap.model == "llama-3.3-70b-fp8"
    assert prom_snap.prompt_tokens_total == 480_000
    assert prom_snap.completion_tokens_total == 64_000
    assert prom_snap.observed_kv_cache_usage_pct == pytest.approx(91.5)
    assert prom_snap.observed_requests_waiting == pytest.approx(14.0)
    assert prom_snap.observed_cache_hit_rate == pytest.approx(0.125)

    jsonl_snap = load_inference_server_file(repo_root / "examples" / "vllm_requests.jsonl")
    assert jsonl_snap.source_format == "jsonl"
    assert jsonl_snap.prompt_tokens_total == 72_000
    assert jsonl_snap.cached_tokens_total == 59_000
    assert "claims-triage" in jsonl_snap.hourly_consumed_tpm_by_tenant

    empty_snap = parse_inference_server_telemetry("")
    assert empty_snap.source_format == "empty"


def test_optimize_hybrid_commitment_newsvendor() -> None:
    series = [362_700.0] * 50 + [120_900.0] * 50
    res = optimize_hybrid_commitment(
        tpm_series=series,
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2000.0,
        paygo_per_1m_burndown_tokens_usd=0.50,
        spillover_tier="priority",
        fsp_discount_pct=10.0,
    )
    assert res.optimal_gsus >= 1
    assert res.p99_gsus == 3
    assert res.total_hybrid_monthly_cost_usd <= res.paygo_100pct_monthly_cost_usd
    assert res.total_hybrid_monthly_cost_usd <= res.p99_pt_monthly_cost_usd
    assert 0.0 < res.breakeven_utilization_pct < 100.0


def test_compute_focus_chargeback_and_raw_vs_weighted_tokens() -> None:
    tenants = {
        "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
        "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
    }
    report = compute_focus_chargeback(
        hourly_consumed_tpm_by_tenant=tenants,
        provisioned_gsus=10,
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2000.0,
        async_batch_waterfill_tpm=362_700.0,
    )
    assert report.total_commitment_cost_usd == pytest.approx(20_000.0)
    assert report.active_consumed_cost_usd < report.total_commitment_cost_usd
    assert report.reclaimed_waterfill_cost_usd > 0.0
    assert report.net_unallocated_idle_cost_usd < report.unallocated_idle_cost_before_waterfill_usd
    claims = next(t for t in report.tenants if t.tenant == "claims-triage")
    assert claims.idle_penalty_avoided_usd > 0.0
    assert claims.active_allocated_cost_usd > claims.unweighted_raw_token_cost_usd


def test_cli_commands_recommend_optimize_chargeback(tmp_path: Path) -> None:
    res_demo = runner.invoke(
        cli_app,
        ["recommend", "--demo", "--active-only", "--by-region"],
    )
    assert res_demo.exit_code == 0
    assert "gemini-3.8-flash" in res_demo.stdout
    assert "Forecast+24h TPM" in res_demo.stdout

    res_sh_demo = runner.invoke(
        cli_app,
        ["recommend", "--demo", "--mode", "self-hosted", "--active-only", "--by-region"],
    )
    assert res_sh_demo.exit_code == 0
    assert "llama-3.3-70b-fp8" in res_sh_demo.stdout

    res_default_opt = runner.invoke(cli_app, ["optimize"])
    assert res_default_opt.exit_code == 0
    assert "Micro-Burst:     not evaluated" in res_default_opt.stdout
    assert "Round-Robin(8)" in res_default_opt.stdout

    res_burst_opt = runner.invoke(
        cli_app, ["optimize", "--sixty-sec-tpm", "150000", "--peak-1s-tps", "4750"]
    )
    assert res_burst_opt.exit_code == 0
    assert "60s Util=62.0%" in res_burst_opt.stdout
    assert "Illusion=True" in res_burst_opt.stdout

    res_sh_opt = runner.invoke(
        cli_app,
        ["optimize", "--mode", "self-hosted", "--model", "llama-3.3-70b-fp8"],
    )
    assert res_sh_opt.exit_code == 0
    assert "Self-Hosted GPU Endpoint Audit" in res_sh_opt.stdout
    assert "Upfront GPU Quota Lock" in res_sh_opt.stdout
    assert "Reps at an assumed 1 request/s (" in res_sh_opt.stdout

    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps([362_700, 241_800, 120_900]), encoding="utf-8")
    res_opt = runner.invoke(
        cli_app,
        [
            "optimize",
            "--model",
            "gemini-3.8-flash-001",
            "--trace-file",
            str(trace_path),
            "--format",
            "json",
        ],
    )
    assert res_opt.exit_code == 0
    payload = json.loads(res_opt.stdout)
    assert payload["normalized_model"] == "gemini-3.8-flash"
    assert payload["weighted_burndown_tokens_per_request"] == pytest.approx(11_600.0)
    assert payload["commitment"]["optimal_gsus"] >= 1

    res_cb = runner.invoke(
        cli_app,
        ["chargeback", "--model", "gemini-3.8-flash", "--gsus", "10"],
    )
    assert res_cb.exit_code == 0
    assert "FOCUS 1.2 Two-Pool Chargeback" in res_cb.stdout
    assert "Unweighted Raw $" in res_cb.stdout


def test_fastapi_endpoints() -> None:
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert '<link rel="icon"' in r_root.text

    r_health = client.get("/healthz")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok"}

    r_rec = client.get(
        "/api/v1/recommendations?project=demo-proj&demo=true&active_only=true&by_region=true"
    )
    assert r_rec.status_code == 200
    rec_data = r_rec.json()
    assert rec_data["active_only"] is True
    assert rec_data["by_region"] is True
    assert rec_data["models"][0]["forecast_24h_peak_tpm"] > 0

    r_opt = client.post(
        "/api/v1/optimize",
        json={
            "model": "gemini-3.8-flash-001",
            "tpm_series": [362_700, 241_800, 120_900],
            "spillover_tier": "priority",
            "fsp_discount_pct": 15.0,
        },
    )
    assert r_opt.status_code == 200
    opt_data = r_opt.json()
    assert opt_data["normalized_model"] == "gemini-3.8-flash"
    assert opt_data["micro_burst"] is None
    assert opt_data["prefix_cache"]["num_round_robin_endpoints"] == 8

    r_opt_burst = client.post(
        "/api/v1/optimize",
        json={
            "tpm_series": [362_700, 241_800, 120_900],
            "sixty_sec_tpm": 150_000,
            "peak_1s_tps": 4_750,
        },
    )
    assert r_opt_burst.status_code == 200
    assert r_opt_burst.json()["micro_burst"]["illusion_detected"] is True

    r_sh = client.post(
        "/api/v1/self-hosted/audit",
        json={"profile": "llama-3.3-70b-fp8", "min_replica_count": 2, "max_replica_count": 6},
    )
    assert r_sh.status_code == 200
    sh_data = r_sh.json()
    assert sh_data["locked_regional_accelerators"] == 48
    assert sh_data["duty_cycle_illusion_detected"] is True

    r_vllm = client.post(
        "/api/v1/vllm/parse",
        json={
            "raw_content": 'vllm:prompt_tokens_total{model_name="llama-3.3-70b-fp8"} 100000\n'
            'vllm:gpu_cache_usage_perc{model_name="llama-3.3-70b-fp8"} 0.88\n'
        },
    )
    assert r_vllm.status_code == 200
    assert r_vllm.json()["observed_kv_cache_usage_pct"] == pytest.approx(88.0)

    r_cb = client.post(
        "/api/v1/chargeback",
        json={
            "model": "gemini-3.8-flash",
            "provisioned_gsus": 10,
            "hourly_consumed_tpm_by_tenant": {
                "claims-triage": [300_000, 100_000],
                "policy-audit": [200_000, 50_000],
            },
            "async_batch_waterfill_tpm": 200_000,
        },
    )
    assert r_cb.status_code == 200
    cb_data = r_cb.json()
    assert cb_data["total_commitment_cost_usd"] == pytest.approx(20_000.0)
    assert len(cb_data["tenants"]) == 2

    r_mt = client.post(
        "/api/v1/prefix-cache/model-tier",
        json={"model": "gemini-3.8-flash", "dynamic_header_offset_tokens": 120},
    )
    assert r_mt.status_code == 200
    assert r_mt.json()["block_zero_contaminated"] is True

    r_disagg = client.post(
        "/api/v1/self-hosted/disaggregated",
        json={"profile": "llama-3.3-70b-fp8", "peak_qps": 18.0},
    )
    assert r_disagg.status_code == 200
    assert r_disagg.json()["prefill_replicas"] >= 1

    r_gw = client.post(
        "/api/v1/gateway/config",
        json={"model": "gemini-3.8-flash", "provisioned_gsus": 20, "target_rps": 650},
    )
    assert r_gw.status_code == 200
    assert "litellm_config_yaml" in r_gw.json()

    r_scav = client.post("/api/v1/finops/scavenger", json={"provisioned_gsus": 20})
    assert r_scav.status_code == 200
    assert r_scav.json()["double_payment_waste_avoided_usd"] > 0.0

    r_sched = client.post("/api/v1/finops/schedule", json={"provisioned_gsus": 20})
    assert r_sched.status_code == 200
    assert r_sched.json()["admitted_deadline_tokens"] > 0.0

    r_fpool = client.post("/api/v1/finops/folder-pooling", json={"percentile_target": 90})
    assert r_fpool.status_code == 200
    assert r_fpool.json()["stranded_gsus_saved"] > 0

    r_kpis = client.post("/api/v1/finops/kpis", json={"verified_outcomes": 100_000})
    assert r_kpis.status_code == 200
    assert r_kpis.json()["cpvo_usd"] > 0.0


def test_model_tier_prefix_cache_block0_and_argon_checkpointing() -> None:
    flash_bad = evaluate_model_tier_prefix_cache(
        model="gemini-3.8-flash-rc1",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=120,
        single_turn_request=True,
        dummy_turn_anchor_applied=False,
    )
    assert flash_bad.normalized_model == "gemini-3.8-flash"
    assert flash_bad.checkpoint_granularity == "block_4096"
    assert flash_bad.min_prefix_floor_tokens == 6_144
    assert flash_bad.block_zero_contaminated is True
    assert flash_bad.aligned_cacheable_tokens == 0
    assert flash_bad.effective_cache_hit_rate == 0.0
    assert flash_bad.burndown_waste_pct > 40.0

    argon_good = evaluate_model_tier_prefix_cache(
        model="gemini-4-argon",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=None,
        single_turn_request=False,
        strip_thought_signatures=False,
    )
    assert argon_good.checkpoint_granularity == "token_boundary"
    assert argon_good.min_prefix_floor_tokens == 2_048
    assert argon_good.block_zero_contaminated is False
    assert argon_good.aligned_cacheable_tokens == 14_000
    assert argon_good.effective_cache_hit_rate == pytest.approx(0.70, abs=0.01)

    stripped_sig = evaluate_model_tier_prefix_cache(
        model="gemini-4-argon",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=None,
        strip_thought_signatures=True,
    )
    assert stripped_sig.routing_affinity_hit_rate == pytest.approx(0.0077)
    assert stripped_sig.burndown_waste_pct > 45.0

    fast_tier = evaluate_model_tier_prefix_cache(
        model="gemini-3.8-flash-fast",
        total_input_tokens=18_000,
        static_prefix_tokens=12_288,
        dynamic_header_offset_tokens=None,
    )
    assert fast_tier.fast_decode_speedup_multiplier == pytest.approx(2.5)
    assert fast_tier.fast_decode_cost_multiplier == pytest.approx(2.0)


def test_disaggregated_prefill_decode_and_multi_gateway_bundle() -> None:
    disagg = size_disaggregated_prefill_decode(
        profile="llama-3.3-70b-fp8",
        peak_qps=18.0,
        avg_input_tokens=6_400,
        avg_output_tokens=450,
    )
    assert disagg.prefill_replicas >= 1
    assert disagg.decode_replicas >= 1
    assert disagg.monolithic_replicas >= disagg.total_disaggregated_replicas

    audit_high = audit_litellm_scaling(
        provisioned_gsus=60,
        target_rps=2_200,
        concurrent_sse_streams=1_200,
        pods=16,
        uvicorn_workers_per_pod=4,
    )
    assert audit_high.total_prisma_rust_engines == 64
    assert audit_high.anti_blackhole_cooldown_seconds == 2
    assert audit_high.scale_status == "migrate_dataplane_to_envoy"

    bundle = generate_gateway_bundle(
        model="gemini-3.8-flash-001",
        provisioned_gsus=20,
        target_rps=650,
    )
    assert bundle.normalized_model == "gemini-3.8-flash"
    assert len(bundle.header_policies) == 8
    assert "use_redis_transaction_buffer: true" in bundle.litellm_config_yaml
    assert "BurndownAndTrafficTypeLogger" in bundle.litellm_burndown_callback_py
    assert "vertex_burndown_tokens" in bundle.envoy_ai_gateway_yaml
    assert "prefix-cache-scorer" in bundle.gke_epp_inference_pool_yaml


def test_scavenger_residual_scheduler_folder_pooling_and_kpis(tmp_path: Path) -> None:
    scav = evaluate_batch_scavenger_economics(
        model="gemini-3.8-flash",
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
        daytime_active_utilization_pct=24.0,
        batch_monthly_burndown_tokens=45_000_000_000.0,
    )
    assert scav.tokens_harvested_on_free_pt == pytest.approx(45_000_000_000.0)
    assert scav.double_payment_waste_avoided_usd == pytest.approx(11_250.0)
    assert scav.effective_utilization_after_scavenging_pct > 60.0

    sched = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
        deadline_jobs=[
            DeadlineJobSpec(
                job_id="high-value-briefing",
                value_usd=18_000.0,
                burndown_tokens=240_000_000.0,
                deadline_hour=6,
            ),
            DeadlineJobSpec(
                job_id="huge-low-value-sweep",
                value_usd=900.0,
                burndown_tokens=600_000_000.0,
                deadline_hour=6,
            ),
        ],
    )
    by_id = {j.job_id: j for j in sched.jobs}
    assert by_id["high-value-briefing"].status == "admitted_edf_pt"
    assert by_id["huge-low-value-sweep"].status == "spilled_or_aborted"

    fpool = evaluate_folder_pooling(
        hourly_tpm_by_project={
            "emea": [480_000.0, 40_000.0],
            "amer": [40_000.0, 480_000.0],
        },
        tpm_per_gsu=120_900,
    )
    assert fpool.siloed_total_gsus == 8
    assert fpool.pooled_folder_gsus == 5
    assert fpool.stranded_gsus_saved == 3

    kpis = compute_finops_kpis()
    assert kpis.cpvo_usd == pytest.approx(0.175)
    assert kpis.total_weighted_burndown_tokens == pytest.approx(4.36e9)
    assert kpis.csr_pct == pytest.approx(68.4)

    html_out = tmp_path / "scorecard.html"
    res_demo = runner.invoke(cli_app, ["demo", "--html-out", str(html_out)])
    assert res_demo.exit_code == 0
    assert html_out.is_file()
    assert "Capacity Showcase Workbench (v0.3.0)" in html_out.read_text(encoding="utf-8")

    res_gw = runner.invoke(cli_app, ["gateway", "--target", "litellm", "--gsus", "20"])
    assert res_gw.exit_code == 0
    assert "litellm_config.yaml" in res_gw.stdout

    res_sched = runner.invoke(cli_app, ["schedule", "--gsus", "20"])
    assert res_sched.exit_code == 0
    assert "admitted_edf_pt" in res_sched.stdout


def test_percentile_demo_and_filter_helpers() -> None:
    assert percentile([], 50) == 0
    assert percentile([10, 20, 30, 40, 50], 50) == 30
    flt = _build_filter("my.metric", ["us-central1", "us-east5"], extra='a = "b"')
    assert 'resource.labels.location = one_of("us-central1", "us-east5")' in flt
    demo_gsu, mins = build_demo_throughput(days=7, by_region=True, mode="gsu")
    assert mins == 10080
    assert "gemini-3.8-flash@us-central1" in demo_gsu


def test_adr005_repository_hygiene() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert not (repo_root / ".github" / "workflows").exists()
    assert (repo_root / "cloudbuild.yaml").exists()

    forbidden_prefix = "/" + "usr/local/" + "google"
    forbidden_pattern = re.compile(
        rf"({re.escape(forbidden_prefix)}|\bgo/[a-z0-9_-]+|\bb/\d{{6,}})",
        re.IGNORECASE,
    )
    skip_dirs = {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
    for path in repo_root.rglob("*"):
        if not path.is_file() or skip_dirs.intersection(path.parts):
            continue
        if path.suffix in (".py", ".md", ".yaml", ".yml", ".toml"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert not forbidden_pattern.search(content), f"ADR-005 violation in {path}"
