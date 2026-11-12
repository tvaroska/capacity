"""Unit, Cloud Monitoring, telemetry, gateway, FinOps, CLI, and API tests for 3P Claude PT."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from capacity.api import app as api_app
from capacity.burst import (
    compute_weighted_burndown_tokens,
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
)
from capacity.capacity import DEFAULT_GSU_CONFIG, normalize_model_id, require_capacity
from capacity.cli import app as cli_app
from capacity.core import PARTNER_TOKEN_METRIC, PT_METRIC, fetch_throughput
from capacity.finops import evaluate_folder_pooling
from capacity.forecasting import UnknownModelError
from capacity.gateway import generate_gateway_bundle
from capacity.model_families import CLAUDE_GSU_CONFIG, is_partner_claude, resolve_model_family
from capacity.telemetry_parsers import parse_inference_server_telemetry

runner = CliRunner()
client = TestClient(api_app)


class _AsyncItems:
    def __init__(self, items: list[SimpleNamespace]) -> None:
        self._items = items
        self._i = 0

    def __aiter__(self) -> _AsyncItems:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._i >= len(self._items):
            raise StopAsyncIteration
        val = self._items[self._i]
        self._i += 1
        return val


def _partner_ts(
    model: str,
    loc: str,
    req_type: str,
    token_type: str,
    pts: list[tuple[datetime, int]],
) -> SimpleNamespace:
    return SimpleNamespace(
        resource=SimpleNamespace(labels={"model_id": model, "location": loc}),
        metric=SimpleNamespace(labels={"request_type": req_type, "type": token_type}),
        points=[
            SimpleNamespace(
                interval=SimpleNamespace(end_time=dt),
                value=SimpleNamespace(int64_value=v, double_value=0.0),
            )
            for dt, v in pts
        ],
    )


def _pt_ts(
    model: str, loc: str, req_type: str, pts: list[tuple[datetime, int]]
) -> SimpleNamespace:
    return SimpleNamespace(
        resource=SimpleNamespace(labels={"model_id": model, "location": loc}),
        metric=SimpleNamespace(labels={"request_type": req_type}),
        points=[
            SimpleNamespace(
                interval=SimpleNamespace(end_time=dt),
                value=SimpleNamespace(int64_value=v, double_value=0.0),
            )
            for dt, v in pts
        ],
    )


def test_claude_gsu_capacities_and_unknown_guard() -> None:
    expected = {
        "claude-3-5-sonnet-v2@20241022": 21_000,
        "claude-3-7-sonnet@20250219": 21_000,
        "publishers/anthropic/models/claude-sonnet-4-6@20260217": 21_000,
        "claude-opus-4-5": 12_600,
        "claude-opus-4-6": 12_600,
        "claude-sonnet-5-5": 31_500,
        "claude-opus-5-5": 15_660,
        "claude-haiku-5-5": 625_020,
    }
    for raw_model, tpm_per_gsu in expected.items():
        norm = normalize_model_id(raw_model)
        assert is_partner_claude(raw_model) is True
        assert require_capacity(norm, DEFAULT_GSU_CONFIG) == tpm_per_gsu

    assert set(CLAUDE_GSU_CONFIG).issubset(set(DEFAULT_GSU_CONFIG))
    with pytest.raises(UnknownModelError):
        require_capacity("claude-sonnet-9", DEFAULT_GSU_CONFIG)


def test_claude_burndown_weights_flat_across_200k_with_cache_writes() -> None:
    # 10,000 total input = 5,000 uncached (1.0x) + 2,000 read (0.1x) + 2,000 5m write (1.25x)
    # + 1,000 1h write (2.0x); 1,000 output + 500 thoughts (5.0x)
    burndown = compute_weighted_burndown_tokens(
        input_tokens=10_000,
        output_tokens=1_000,
        thoughts_tokens=500,
        cached_tokens=2_000,
        cache_write_5m_tokens=2_000,
        cache_write_1h_tokens=1_000,
        model="claude-sonnet-4-6",
    )
    assert burndown == pytest.approx(5_000.0 + 200.0 + 2_500.0 + 2_000.0 + 7_500.0)

    # Confirm 3P Claude stays flat above 200K tokens (no 1P 2.0x/7.5x cliff)
    long_burndown = compute_weighted_burndown_tokens(
        input_tokens=250_000,
        output_tokens=2_000,
        cached_tokens=200_000,
        model="claude-sonnet-4-6",
    )
    assert long_burndown == pytest.approx(50_000.0 + 20_000.0 + 10_000.0)

    # 1P Gemini default retains >200K cliff when model is None or gemini-*
    gemini_long = compute_weighted_burndown_tokens(
        input_tokens=250_000,
        output_tokens=2_000,
        cached_tokens=200_000,
        model="gemini-3.8-flash",
    )
    assert gemini_long == pytest.approx(100_000.0 + 40_000.0 + 15_000.0)


def test_claude_dual_bucket_30s_60s_and_zero_burst_credit() -> None:
    spec = resolve_model_family("claude-sonnet-4-6")
    assert spec.bucket_short_seconds == 30
    assert spec.bucket_long_seconds == 60
    assert spec.burst_credit_pct == 0.0

    # 10 GSUs * 350 TPS/GSU = 3,500 committed TPS (210,000 TPM).
    # 140,000 TPM average (~66.7% 60s util) concentrated in a 30s burst = 4,666.7 TPS (> 3,500).
    audit = evaluate_micro_burst_window(
        sixty_sec_tpm=140_000.0,
        peak_1s_tps=0.0,
        provisioned_gsus=10,
        tps_per_gsu=350.0,
        peak_30s_tps=4_600.0,
        model="claude-sonnet-4-6",
    )
    assert audit.refill_window_seconds == 30
    assert audit.long_window_seconds == 60
    assert audit.enforcement_mode == "3p_dual_bucket_30s_60s"
    assert audit.burst_credit_pct == 0.0
    assert audit.apparent_60s_utilization_pct == pytest.approx(66.67, abs=0.05)
    assert audit.peak_window_utilization_pct > 100.0
    assert audit.triggers_429_or_spillover is True
    assert audit.illusion_detected is True
    # Conservation holds: active 30s tokens <= 60s TPM
    assert audit.peak_window_tps * 30 <= 140_000.0 + 1e-6


def test_claude_prefix_cache_tiers_and_explicit_cache_control() -> None:
    sonnet = evaluate_model_tier_prefix_cache(
        model="claude-sonnet-4-6",
        total_input_tokens=8_000,
        static_prefix_tokens=6_000,
        output_tokens=500,
        thoughts_tokens=0,
        dynamic_header_offset_tokens=6_000,
    )
    assert sonnet.checkpoint_granularity == "token_boundary"
    assert sonnet.min_prefix_floor_tokens == 1_024
    assert any("cache_control" in r for r in sonnet.remediation_steps)

    opus4 = evaluate_model_tier_prefix_cache(
        model="claude-opus-4-6",
        total_input_tokens=8_000,
        static_prefix_tokens=2_000,
        dynamic_header_offset_tokens=2_000,
    )
    assert opus4.min_prefix_floor_tokens == 4_096
    assert opus4.effective_cache_hit_rate == 0.0

    v55 = evaluate_model_tier_prefix_cache(
        model="claude-sonnet-5-5",
        total_input_tokens=2_000,
        static_prefix_tokens=800,
        dynamic_header_offset_tokens=800,
    )
    assert v55.min_prefix_floor_tokens == 512
    assert v55.effective_cache_hit_rate > 0.0


@pytest.mark.asyncio
async def test_fetch_throughput_partner_token_count_weighting_and_mixed_models() -> None:
    t0 = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    captured_filters: list[str] = []

    claude_series = [
        _partner_ts("claude-sonnet-4-6", "us-east5", "dedicated", "input", [(t0, 10_000)]),
        _partner_ts("claude-sonnet-4-6", "us-east5", "dedicated", "cache_read", [(t0, 5_000)]),
        _partner_ts("claude-sonnet-4-6", "us-east5", "dedicated", "cache_write_5m", [(t0, 4_000)]),
        _partner_ts("claude-sonnet-4-6", "us-east5", "dedicated", "cache_write_1h", [(t0, 1_000)]),
        _partner_ts("claude-sonnet-4-6", "us-east5", "dedicated", "output", [(t0, 2_000)]),
    ]
    gemini_series = [
        _pt_ts("gemini-3.8-flash", "us-central1", "dedicated", [(t0, 120_900)]),
    ]

    class _FakeMonitoringClient:
        async def list_time_series(self, request: dict[str, Any]) -> _AsyncItems:
            flt = request["filter"]
            captured_filters.append(flt)
            if PARTNER_TOKEN_METRIC in flt:
                return _AsyncItems(claude_series)
            if PT_METRIC in flt:
                return _AsyncItems(gemini_series if '"dedicated"' in flt else [])
            return _AsyncItems([])

    with patch(
        "capacity.core.monitoring_v3.MetricServiceAsyncClient",
        return_value=_FakeMonitoringClient(),
    ):
        sparse_claude, _ = await fetch_throughput(
            "demo-proj", days=1, models=["claude-sonnet-4-6"]
        )

    # Expected: 10000*1.0 + 5000*0.1 + 4000*1.25 + 1000*2.0 + 2000*5.0 = 27,500
    assert sparse_claude == {"claude-sonnet-4-6": {t0: 27_500}}
    assert len(captured_filters) == 1
    assert PARTNER_TOKEN_METRIC in captured_filters[0]

    captured_filters.clear()
    with patch(
        "capacity.core.monitoring_v3.MetricServiceAsyncClient",
        return_value=_FakeMonitoringClient(),
    ):
        sparse_mixed, _ = await fetch_throughput(
            "demo-proj", days=1, models=["gemini-3.8-flash", "claude-sonnet-4-6"]
        )

    assert sparse_mixed["gemini-3.8-flash"][t0] == 120_900
    assert sparse_mixed["claude-sonnet-4-6"][t0] == 27_500
    assert any(PT_METRIC in f for f in captured_filters)
    assert any(PARTNER_TOKEN_METRIC in f for f in captured_filters)


def test_parse_anthropic_messages_jsonl_additive_usage_and_cache_writes() -> None:
    lines = [
        json.dumps(
            {
                "timestamp": "2026-11-12T10:00:00Z",
                "model": "claude-sonnet-4-6",
                "tenant": "risk-desk",
                "usage": {
                    "input_tokens": 2_000,
                    "cache_read_input_tokens": 8_000,
                    "cache_creation_input_tokens": 3_000,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 2_000,
                        "ephemeral_1h_input_tokens": 1_000,
                    },
                    "output_tokens": 1_000,
                },
            }
        )
    ]
    snap = parse_inference_server_telemetry("\n".join(lines))
    assert snap.model == "claude-sonnet-4-6"
    # Additive Anthropic input: 2000 + 8000 + 3000 = 13,000 total prompt tokens
    assert snap.prompt_tokens_total == 13_000
    assert snap.cached_tokens_total == 8_000
    assert snap.cache_write_5m_tokens_total == 2_000
    assert snap.cache_write_1h_tokens_total == 1_000
    assert snap.completion_tokens_total == 1_000
    # 2000*1.0 + 8000*0.1 + 2000*1.25 + 1000*2.0 + 1000*5.0 = 12,300
    assert snap.weighted_burndown_tokens_total == pytest.approx(12_300.0)


@pytest.mark.asyncio
async def test_claude_gateway_bundle_yaml_cel_and_executable_callback(tmp_path: Path) -> None:
    bundle = generate_gateway_bundle(
        model="claude-sonnet-4-6",
        project_id="hub-genai-prod",
        location="us-east5",
        provisioned_gsus=25,
        target_rps=400,
    )
    assert bundle.refill_window_seconds == 30
    assert bundle.committed_burndown_tpm == 25 * 21_000
    assert any("30s/60s" in w for w in bundle.litellm_audit.bottleneck_warnings)
    assert any("Folder RHPT" in w for w in bundle.litellm_audit.bottleneck_warnings)

    litellm_doc = yaml.safe_load(bundle.litellm_config_yaml)
    model_names = [m["model_name"] for m in litellm_doc["model_list"]]
    assert "claude-sonnet-4-6-pt" in model_names
    assert "claude-sonnet-4-6-standard-spillover" in model_names
    spill_entry = next(
        m for m in litellm_doc["model_list"] if m["model_name"].endswith("-standard-spillover")
    )
    assert (
        spill_entry["litellm_params"]["extra_headers"]["X-Vertex-AI-LLM-Shared-Request-Type"]
        == "standard"
    )

    envoy_docs = list(yaml.safe_load_all(bundle.envoy_ai_gateway_yaml))
    assert len(envoy_docs) == 2
    cel_entry = next(
        c
        for c in envoy_docs[0]["spec"]["rules"][0]["llmRequestCosts"]
        if c["metadataKey"] == "vertex_burndown_tokens"
    )
    assert "5.0" in cel_entry["cel"]
    assert "200000" not in cel_entry["cel"]

    # Compile and execute generated callback against Anthropic usage payload
    fake_litellm = types.ModuleType("litellm")
    fake_litellm_integrations = types.ModuleType("litellm.integrations")
    fake_custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class _BaseCustomLogger:
        pass

    fake_custom_logger.CustomLogger = _BaseCustomLogger  # type: ignore[attr-defined]

    cb_file = tmp_path / "litellm_burndown_callback.py"
    cb_file.write_text(bundle.litellm_burndown_callback_py, encoding="utf-8")
    mod_spec = importlib.util.spec_from_file_location("claude_cb_mod", cb_file)
    assert mod_spec is not None and mod_spec.loader is not None
    cb_mod = importlib.util.module_from_spec(mod_spec)

    with patch.dict(
        sys.modules,
        {
            "litellm": fake_litellm,
            "litellm.integrations": fake_litellm_integrations,
            "litellm.integrations.custom_logger": fake_custom_logger,
        },
    ):
        mod_spec.loader.exec_module(cb_mod)

    cb = cb_mod.BurndownAndTrafficTypeLogger(time_fn=lambda: 1_763_000_000.0)

    class _MemCache:
        def __init__(self) -> None:
            self.store: dict[str, float] = {}

        async def async_get_cache(self, key: str) -> float:
            return self.store.get(key, 0.0)

        async def async_increment(self, key: str, value: float = 1.0, ttl: int = 120) -> float:
            self.store[key] = self.store.get(key, 0.0) + value
            return self.store[key]

    cache = _MemCache()
    api_key = SimpleNamespace(team_id="bu-a", max_budget_tpm=5_000.0)
    resp = SimpleNamespace(
        usage={
            "input_tokens": 1_000,
            "cache_read_input_tokens": 2_000,
            "cache_creation_input_tokens": 1_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000,
                "ephemeral_1h_input_tokens": 0,
            },
            "output_tokens": 1_000,
        }
    )
    # Expected burndown: 1000*1.0 + 2000*0.1 + 1000*1.25 + 1000*5.0 = 7,450
    data1: dict[str, Any] = {}
    await cb.async_pre_call_hook(
        user_api_key_dict=api_key, cache=cache, data=data1, call_type="completion"
    )
    assert "extra_headers" not in data1
    await cb.async_log_success_event(
        kwargs={"litellm_params": data1["litellm_params"]},
        response_obj=resp,
        start_time=0.0,
        end_time=1.0,
    )
    assert sum(cache.store.values()) == pytest.approx(7_450.0)

    # Next request sees team over 5,000 TPM limit and flips to shared/standard spillover
    data2: dict[str, Any] = {}
    await cb.async_pre_call_hook(
        user_api_key_dict=api_key, cache=cache, data=data2, call_type="completion"
    )
    assert data2["extra_headers"]["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert data2["extra_headers"]["X-Vertex-AI-LLM-Shared-Request-Type"] == "standard"


def test_claude_folder_pooling_surfaces_1p_only_warning() -> None:
    report = evaluate_folder_pooling(
        hourly_tpm_by_project={
            "spoke-a": [200_000.0, 40_000.0],
            "spoke-b": [40_000.0, 200_000.0],
        },
        model="claude-sonnet-4-6",
        tpm_per_gsu=21_000,
    )
    joined = " ".join(report.governance_notes)
    assert "Folder-Level RHPT" in joined
    assert "hub project" in joined


def test_claude_cli_and_api_endpoints_plus_portal() -> None:
    for cmd in (
        [
            "optimize",
            "--model",
            "claude-sonnet-4-6",
            "--cache-write-5m-tokens",
            "4000",
            "--peak-30s-tps",
            "4200",
        ],
        ["gateway", "--target", "all", "--model", "claude-sonnet-4-6", "--gsus", "25"],
        ["schedule", "--model", "claude-sonnet-4-6", "--gsus", "25"],
        ["chargeback", "--model", "claude-sonnet-4-6", "--gsus", "25"],
    ):
        res = runner.invoke(cli_app, cmd)
        assert res.exit_code == 0, f"CLI command {cmd} failed: {res.output}"

    opt_res = client.post(
        "/api/v1/optimize",
        json={
            "model": "claude-sonnet-4-6",
            "tpm_series": [210_000.0, 180_000.0, 150_000.0, 90_000.0],
            "total_input_tokens": 10_000,
            "cached_tokens": 2_000,
            "cache_write_5m_tokens": 2_000,
            "output_tokens": 1_000,
            "thoughts_tokens": 0,
            "peak_30s_tps": 4_200.0,
        },
    )
    assert opt_res.status_code == 200
    body = opt_res.json()
    assert body["micro_burst"]["refill_window_seconds"] == 30
    assert body["micro_burst"]["long_window_seconds"] == 60
    assert body["micro_burst"]["enforcement_mode"] == "3p_dual_bucket_30s_60s"

    gw_res = client.post(
        "/api/v1/gateway/config",
        json={"model": "claude-sonnet-4-6", "provisioned_gsus": 25},
    )
    assert gw_res.status_code == 200
    assert gw_res.json()["refill_window_seconds"] == 30

    fp_res = client.post(
        "/api/v1/finops/folder-pooling",
        json={
            "model": "claude-sonnet-4-6",
            "hourly_tpm_by_project": {"spoke-a": [210_000.0, 42_000.0], "spoke-b": [42_000.0, 210_000.0]},
        },
    )
    assert fp_res.status_code == 200
    assert any("Folder-Level RHPT" in n for n in fp_res.json()["governance_notes"])

    portal_res = client.get("/")
    assert portal_res.status_code == 200
    assert "claude-sonnet-4-6" in portal_res.text
    assert "claude-opus-4-6" in portal_res.text
    assert "claude-sonnet-5-5" in portal_res.text
