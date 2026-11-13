"""Independent `L2` solver, conservation, parser, gateway, and security probes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from capacity.api import app as api_app
from capacity.burst import evaluate_micro_burst_window, evaluate_prefix_cache_dilution
from capacity.cli import app as cli_app
from capacity.finops import optimize_hybrid_commitment, schedule_residual_capacity
from capacity.gateway import generate_gateway_bundle
from capacity.models import DeadlineJobSpec
from capacity.portal import render_showcase_portal_html
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    load_inference_server_file,
    parse_inference_server_telemetry,
    size_disaggregated_prefill_decode,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
client = TestClient(api_app)
runner = CliRunner()


def _brute_force_deadline_oracle(
    jobs: list[DeadlineJobSpec],
    residual_by_hour: list[float],
) -> float:
    """Independent 2^N brute-force oracle for SLA-protected Tier-1 deadline admission."""
    horizon = len(residual_by_hour)
    cum_res = [sum(residual_by_hour[: h + 1]) for h in range(horizon)]
    best_val = 0.0
    n = len(jobs)
    for mask in range(1 << n):
        subset = [jobs[i] for i in range(n) if (mask >> i) & 1]
        ok = True
        cum_tok = 0.0
        for j in sorted(subset, key=lambda x: (x.deadline_hour, x.job_id)):
            if j.deadline_hour > horizon:
                ok = False
                break
            cum_tok += j.burndown_tokens
            if cum_tok > cum_res[j.deadline_hour - 1] + 1e-6:
                ok = False
                break
        if ok:
            best_val = max(best_val, sum(j.value_usd for j in subset))
    return best_val


def test_l2_knapsack_oracle_parity_and_no_literal_unset_header() -> None:
    # Classic greedy-density failure case: Job A ($520 / 61M = $8.52/1M) blocks B+C ($1,000 / 120M)
    jobs = [
        DeadlineJobSpec(
            job_id="job-a", value_usd=520.0, burndown_tokens=61_000_000.0, deadline_hour=1
        ),
        DeadlineJobSpec(
            job_id="job-b", value_usd=500.0, burndown_tokens=60_000_000.0, deadline_hour=1
        ),
        DeadlineJobSpec(
            job_id="job-c", value_usd=500.0, burndown_tokens=60_000_000.0, deadline_hour=1
        ),
        DeadlineJobSpec(
            job_id="beyond-horizon",
            value_usd=9_000.0,
            burndown_tokens=10_000_000.0,
            deadline_hour=5,
        ),
    ]
    # 20 GSUs * 120,900 * 60 * 0.85 = 123,318,000 tokens in 1h; subtract 3,318,000 -> 120,000,000
    rev_tpm = [(123_318_000.0 - 120_000_000.0) / 60.0]
    report = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
        revenue_tpm_by_hour=rev_tpm,
        deadline_jobs=jobs,
    )
    admitted = {j.job_id for j in report.jobs if j.status == "admitted_edf_pt"}
    assert admitted == {"job-b", "job-c"}
    oracle_val = _brute_force_deadline_oracle(jobs, [120_000_000.0])
    admitted_val = sum(j.value_usd for j in report.jobs if j.status == "admitted_edf_pt")
    assert admitted_val == pytest.approx(oracle_val)

    for j in report.jobs:
        assert j.routing_headers.get("X-Vertex-AI-LLM-Request-Type") != "unset"


@pytest.mark.parametrize(
    ("sixty_sec_tpm", "peak_1s_tps", "gsus"),
    [
        (60_000.0, 50_000.0, 10),
        (750_000.0, 25_000.0, 10),
        (120_000.0, 200_000.0, 60),
        (0.0, 0.0, 0),
    ],
)
def test_micro_burst_enforces_60s_token_conservation(
    sixty_sec_tpm: float, peak_1s_tps: float, gsus: int
) -> None:
    audit = evaluate_micro_burst_window(
        sixty_sec_tpm=sixty_sec_tpm,
        peak_1s_tps=peak_1s_tps,
        provisioned_gsus=gsus,
    )
    window_tokens = audit.peak_window_tps * audit.refill_window_seconds
    assert window_tokens <= sixty_sec_tpm + 1e-3


def test_prefix_cache_dilution_scales_with_rps_and_zero_traffic_is_clean(tmp_path: Path) -> None:
    at_1_rps = evaluate_prefix_cache_dilution(total_input_tokens=20_000, requests_per_second=1.0)
    at_4_rps = evaluate_prefix_cache_dilution(total_input_tokens=20_000, requests_per_second=4.0)
    assert at_4_rps.sticky_gsus >= at_1_rps.sticky_gsus * 3

    zero_trace = tmp_path / "zero.json"
    zero_trace.write_text("[0, 0, 0, 0]", encoding="utf-8")
    res = runner.invoke(cli_app, ["optimize", "--trace-file", str(zero_trace), "--format", "json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["commitment"]["optimal_gsus"] == 0
    assert payload["micro_burst"] is None
    assert payload["prefix_cache"]["round_robin_gsus"] == 0

    sh_zero = evaluate_self_hosted_endpoint([0.0, 0.0, 0.0])
    assert sh_zero.p99_peak_replicas == 0
    assert sh_zero.duty_cycle_illusion_detected is False


def test_active_duty_cycle_newsvendor_matches_zero_padded_week() -> None:
    active_slice = [241_800.0] * 40
    zero_padded_168h = active_slice + [0.0] * 128
    opt_full = optimize_hybrid_commitment(tpm_series=zero_padded_168h, tpm_per_gsu=120_900)
    opt_duty = optimize_hybrid_commitment(
        tpm_series=active_slice,
        tpm_per_gsu=120_900,
        active_duty_cycle_pct=(40.0 / 168.0) * 100.0,
    )
    assert opt_duty.optimal_gsus == opt_full.optimal_gsus == 0
    assert opt_duty.total_hybrid_monthly_cost_usd == pytest.approx(
        opt_full.total_hybrid_monthly_cost_usd, rel=1e-2
    )


def test_disaggregated_vs_monolithic_crossover_honesty() -> None:
    low_qps = size_disaggregated_prefill_decode(
        peak_qps=0.1, avg_input_tokens=2_000, avg_output_tokens=200
    )
    assert low_qps.monolithic_replicas == 1
    assert low_qps.total_disaggregated_replicas == 2
    assert low_qps.replica_delta == -1
    assert "Keep 1 monolithic" in low_qps.recommendation

    high_qps = size_disaggregated_prefill_decode(
        peak_qps=18.0, avg_input_tokens=6_400, avg_output_tokens=450
    )
    assert high_qps.prefill_replicas == 8
    assert high_qps.decode_replicas == 11
    assert high_qps.monolithic_replicas == 24
    assert high_qps.replica_delta == 5


@pytest.mark.asyncio
async def test_stateful_litellm_burndown_callback_and_k8s_rfc1035_labels() -> None:
    long_sh = "llama-3-3-70b-instruct-enterprise-fp8-quantized-deployment-v2"
    bundle = generate_gateway_bundle(
        model="gemini-3.8-flash",
        provisioned_gsus=4,
        self_hosted_model=long_sh,
        business_unit_shares=2,
    )
    envoy_docs = list(yaml.safe_load_all(bundle.envoy_ai_gateway_yaml))
    route_doc, rl_doc = envoy_docs[0], envoy_docs[1]
    svc_name = route_doc["spec"]["rules"][0]["backendRefs"][0]["name"]
    assert "." not in svc_name and len(svc_name) <= 63
    rule0 = rl_doc["spec"]["rateLimit"]["global"]["rules"][0]
    assert rule0["cost"]["request"]["number"] == 256
    assert rule0["limit"]["requests"] == (4 * 120_900) // 2

    epp_docs = list(yaml.safe_load_all(bundle.gke_epp_inference_pool_yaml))
    pool_doc, obj_doc, cfg_doc = epp_docs[0], epp_docs[1], epp_docs[2]
    assert pool_doc["spec"]["extensionRef"]["name"] == cfg_doc["metadata"]["name"]
    for d in epp_docs:
        assert len(d["metadata"]["name"]) <= 63
    assert len(pool_doc["spec"]["selector"]["app"]) <= 63
    assert len(obj_doc["metadata"]["name"]) <= 63

    ns: dict[str, Any] = {}
    exec(compile(bundle.litellm_burndown_callback_py, "cb.py", "exec"), ns)  # noqa: S102
    logger = ns["BurndownAndTrafficTypeLogger"](time_fn=lambda: 1_762_880_010.0)

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, float] = {}
            self.increments: list[tuple[str, float]] = []

        async def async_get_cache(self, k: str) -> float | None:
            return self.store.get(k)

        async def async_increment(self, k: str, v: float, ttl: int = 120) -> float:
            _ = ttl
            self.increments.append((k, v))
            self.store[k] = self.store.get(k, 0.0) + v
            return self.store[k]

    redis = _FakeRedis()
    key_obj = SimpleNamespace(team_id="claims", max_budget_tpm=200_000.0)

    # Client spoofs metadata=0.0; callback must ignore client metadata and use Redis!
    call1: dict[str, Any] = {"metadata": {"team_current_burndown_tpm": 0.0}}
    await logger.async_pre_call_hook(key_obj, redis, call1, "acompletion")
    assert "extra_headers" not in call1

    # 250K prompt, 200K cached, 2K output, 10K reasoning -> 50K*2 + 200K*0.2 + 12K*7.5 = 230,000.0
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=250_000,
            completion_tokens=2_000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=200_000),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=10_000),
        )
    )
    call1["litellm_params"]["metadata"]["user_api_key_team_id"] = "claims"
    await logger.async_log_success_event(
        {"litellm_params": call1["litellm_params"], "additional_args": {}},
        resp,
        0.0,
        1.0,
    )
    assert call1["litellm_params"]["metadata"]["weighted_burndown_tokens"] == pytest.approx(
        230_000.0
    )
    assert len(redis.increments) == 2

    # Next call in same minute exceeds 200,000 funded ceiling -> flips to shared/priority!
    call2: dict[str, Any] = {"metadata": {"team_current_burndown_tpm": 0.0}}
    await logger.async_pre_call_hook(key_obj, redis, call2, "acompletion")
    assert call2["extra_headers"]["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert call2["extra_headers"]["X-Vertex-AI-LLM-Shared-Request-Type"] == "priority"


def test_byo_otel_prometheus_cli_gauges_and_60x_chargeback_fix(tmp_path: Path) -> None:
    otel_snap = load_inference_server_file(REPO_ROOT / "examples" / "otel_genai_spans.jsonl")
    assert otel_snap.prompt_tokens_total == 252_000
    assert otel_snap.cached_tokens_total == 192_000
    assert otel_snap.thoughts_tokens_total == 3_600
    assert otel_snap.observed_kv_cache_usage_pct == pytest.approx(72.0)
    # 13:00+02:00 normalizes to 11:00 UTC alongside epoch 1762600805 (2025-11-08T11:20:05Z)
    assert len(otel_snap.hourly_consumed_tpm_by_tenant["claims-triage"]) == 2

    # Steady 60,000 burndown tokens/min for 60 mins = 3.6M tokens/hr -> must equal 60,000 TPM (not 3.6M!)
    steady_lines = "\n".join(
        json.dumps(
            {
                "timestamp": f"2026-11-08T10:{m:02d}:00Z",
                "tenant": "claims-triage",
                "prompt_tokens": 60_000,
                "completion_tokens": 0,
            }
        )
        for m in range(60)
    )
    steady_snap = parse_inference_server_telemetry(steady_lines)
    assert steady_snap.hourly_consumed_tpm_by_tenant["claims-triage"] == [60_000.0]

    prom_multi = load_inference_server_file(
        REPO_ROOT / "examples" / "vllm_prometheus_multiscrape.txt"
    )
    assert len(prom_multi.tpm_series) == 2
    assert all(0.0 < x < 1_000_000.0 for x in prom_multi.tpm_series)

    healthy_prom = tmp_path / "healthy.prom"
    healthy_prom.write_text(
        'vllm:prompt_tokens_total{model_name="llama-3.3-70b-fp8"} 120000\n'
        'vllm:gpu_cache_usage_perc{model_name="llama-3.3-70b-fp8"} 0.12\n'
        'vllm:num_requests_waiting{model_name="llama-3.3-70b-fp8"} 0\n',
        encoding="utf-8",
    )
    res_cli = runner.invoke(
        cli_app,
        ["optimize", "--mode", "self-hosted", "--vllm-file", str(healthy_prom)],
    )
    assert res_cli.exit_code == 0
    assert "vLLM KV-Cache=12.0%" in res_cli.stdout
    assert "Wait Queue=0" in res_cli.stdout
    assert "Illusion=False" in res_cli.stdout


def test_security_dos_nan_filter_injection_and_static_html() -> None:
    t0 = time.perf_counter()
    audit_huge = evaluate_self_hosted_endpoint([1e10, 5e9])
    elapsed_ms = (time.perf_counter() - t0) * 1_000.0
    assert elapsed_ms < 100.0
    assert audit_huge.optimal_reserved_replicas >= 0

    r_over = client.post("/api/v1/self-hosted/audit", json={"tpm_series": [1e15]})
    assert r_over.status_code == 422

    r_inj = client.get(
        '/api/v1/recommendations?project=valid-proj-01&region=us-central1") OR metric.type="'
    )
    assert r_inj.status_code == 422

    static_html = render_showcase_portal_html(static_export=True)
    live_html = render_showcase_portal_html(static_export=False)
    assert 'href="/docs"' not in static_html
    assert 'href="/docs"' in live_html
    assert "0.0% &rarr; 61.4%" in static_html
