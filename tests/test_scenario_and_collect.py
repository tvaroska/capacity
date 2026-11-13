"""Tests for ShowcaseScenario (`--scenario-file`), `capacity collect`, Carbon, and D5 fixes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from google.api_core.exceptions import PermissionDenied
from typer.testing import CliRunner

from capacity.burst import evaluate_micro_burst_window, evaluate_model_tier_prefix_cache
from capacity.capacity import require_capacity
from capacity.cli import app as cli_app
from capacity.core import check_gcp_project_access, fetch_inventory_telemetry
from capacity.model_families import resolve_model_family
from capacity.portal import render_showcase_portal_html
from capacity.scenario import (
    ShowcaseScenario,
    audit_scenario_public_safety,
    load_showcase_scenario,
)
from capacity.telemetry_parsers import parse_inference_server_telemetry

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_gemini_4_carbon_registered_with_pro_token_boundary_spec() -> None:
    assert require_capacity("gemini-4-carbon") == 30_000
    spec = resolve_model_family("gemini-4-carbon-001")
    assert spec.min_prefix_floor_tokens == 2_048
    assert spec.checkpoint_granularity == "token_boundary"
    audit = evaluate_model_tier_prefix_cache(
        model="gemini-4-carbon",
        static_prefix_tokens=3_000,
        dynamic_header_offset_tokens=None,
    )
    assert not audit.below_minimum_floor
    assert audit.aligned_cacheable_tokens == 3_000


def test_micro_burst_zero_spillover_accounts_for_50_gsu_window_switch() -> None:
    # 60s avg = 60,000 TPS; at 40 GSUs (<50), 10s peak = 110,000 TPS -> 52 GSUs (>=50).
    # Crossing >=50 GSUs switches 1P enforcement to the 1s bucket (1s peak = 150,000 -> 71 GSUs).
    audit = evaluate_micro_burst_window(
        sixty_sec_tpm=3_600_000.0,
        peak_1s_tps=150_000.0,
        peak_10s_tps=110_000.0,
        provisioned_gsus=40,
        tps_per_gsu=2_015.0,
        model="gemini-3.8-flash",
    )
    assert audit.refill_window_seconds == 10
    assert audit.gsus_required_for_zero_spillover == 71

    verified = evaluate_micro_burst_window(
        sixty_sec_tpm=3_600_000.0,
        peak_1s_tps=150_000.0,
        peak_10s_tps=110_000.0,
        provisioned_gsus=audit.gsus_required_for_zero_spillover,
        tps_per_gsu=2_015.0,
        model="gemini-3.8-flash",
    )
    assert verified.refill_window_seconds == 1
    assert not verified.triggers_429_or_spillover


def test_recommend_table_forecast_header_reflects_requested_horizon() -> None:
    res = runner.invoke(cli_app, ["recommend", "--demo", "--active-only", "--forecast-hours", "72"])
    assert res.exit_code == 0, res.output
    assert "Forecast+72h TPM" in res.stdout


def test_observability_2_inference_request_logs_parse_and_compile_to_scenario(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "timestamp": "2026-11-10T14:00:01.100Z",
            "resource": {
                "type": "aiplatform.googleapis.com/PublisherModel",
                "labels": {"model_user_id": "gemini-3.8-flash", "location": "global"},
            },
            "jsonPayload": {
                "prompt_token_count": 20_000,
                "cached_content_token_count": 16_000,
                "candidates_token_count": 1_000,
                "thoughts_token_count": 500,
                "traffic_type": "PROVISIONED_THROUGHPUT",
                "status_code": 200,
                "request_labels": {"tenant": "equities-desk"},
            },
        },
        {
            "timestamp": "2026-11-10T14:00:01.450Z",
            "model_id": "gemini-3.5-flash-lite",
            "location": "prod-global",
            "tenant": "wealth-coach",
            "traffic_type": "ON_DEMAND_PRIORITY",
            "status_code": 429,
            "prompt_token_count": 10_000,
            "cached_content_token_count": 8_000,
            "candidates_token_count": 500,
            "thoughts_token_count": 0,
        },
    ]
    logs_path = tmp_path / "bql_inference.jsonl"
    logs_path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    snap = parse_inference_server_telemetry(logs_path.read_text(encoding="utf-8"))
    assert snap.total_requests == 2
    assert snap.spillover_requests == 1
    assert snap.http_429_count == 1
    assert snap.peak_1s_burndown_tps > 0.0
    assert "gemini-3.8-flash@global" in snap.minute_tpm_by_model_region
    assert "gemini-3.5-flash-lite@global" in snap.minute_tpm_by_model_region
    assert set(snap.hourly_consumed_tpm_by_tenant) == {"equities-desk", "wealth-coach"}

    out_scenario = tmp_path / "compiled_scenario.json"
    res = runner.invoke(
        cli_app,
        [
            "collect",
            "--logs-file",
            str(logs_path),
            "--orders-file",
            str(REPO_ROOT / "examples/orders_overlay.yaml"),
            "--gateway-mode",
            "central_proxy",
            "--pooling-mode",
            "1p_vs_3p_hub",
            "--no-self-hosted",
            "--strict-public-check",
            "--out",
            str(out_scenario),
        ],
    )
    assert res.exit_code == 0, res.output
    assert out_scenario.is_file()

    html_path = tmp_path / "scorecard.html"
    demo_res = runner.invoke(
        cli_app,
        [
            "demo",
            "--scenario-file",
            str(out_scenario),
            "--strict-public-check",
            "--html-out",
            str(html_path),
        ],
    )
    assert demo_res.exit_code == 0, demo_res.output
    rendered = html_path.read_text(encoding="utf-8")
    assert "Central Gateway Policies &amp; Upstream Contracts" in rendered
    assert "Contracted Managed PT Fleet" in rendered
    assert "Short-Term GA (Eval/Pilot)" in rendered
    assert "LiteLLM Prisma Engines" not in rendered
    assert re.findall(r"__[A-Z][A-Z_]*__", rendered) == []


def test_shipped_enterprise_scenario_renders_clean_and_passes_strict_public_check() -> None:
    scenario_path = REPO_ROOT / "examples" / "scenarios" / "multi_tenant_enterprise.json"
    scenario = load_showcase_scenario(scenario_path, strict_public_check=True)
    assert audit_scenario_public_safety(scenario) == []
    rendered = render_showcase_portal_html(static_export=True, scenario=scenario)
    assert "Gemini Platform Capacity, Gateway &amp; FOCUS 1.2 Scorecard" in rendered
    assert "Option A / Option B" in rendered
    assert "Fixed-Term Renewal Cliff" in rendered
    assert "gemini-4-carbon" in rendered
    assert re.findall(r"__[A-Z][A-Z_]*__", rendered) == []


def test_strict_public_check_catches_leaked_project_numbers_and_suffixes(tmp_path: Path) -> None:
    raw = json.loads(
        (REPO_ROOT / "examples/scenarios/multi_tenant_enterprise.json").read_text(encoding="utf-8")
    )
    raw["gsu_sizing"]["project_label"] = "proj-1085051178019"
    raw["gsu_sizing"]["inventory_annotations"]["gemini-3.8-flash@global"]["note"] = (
        "gemini-3.8-flash-qcd"
    )
    bad_path = tmp_path / "leaked.json"
    bad_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="10-14 digit cloud project or order identifier"):
        load_showcase_scenario(bad_path, strict_public_check=True)

    res = runner.invoke(
        cli_app, ["demo", "--scenario-file", str(bad_path), "--strict-public-check"]
    )
    assert res.exit_code == 1
    assert "Error:" in res.output


def test_scenario_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ShowcaseScenario.model_validate(
            {
                "gsu_sizing": {"minute_tpm_series": {"gemini-3.8-flash": [120_900.0]}},
                "unexpected_top_field": True,
            }
        )


@pytest.mark.asyncio
async def test_check_gcp_project_access_distinguishes_disabled_api_vs_iam() -> None:
    class _EmptyPager:
        def __aiter__(self) -> _EmptyPager:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class _TypedPermissionDenied(PermissionDenied):
        def __init__(self, msg: str) -> None:
            Exception.__init__(self, msg)
            self.message = msg

    class _FakeClient:
        async def list_metric_descriptors(self, request: dict[str, Any]) -> _EmptyPager:
            name = request["name"]
            if name.endswith("proj-ok"):
                return _EmptyPager()
            if name.endswith("proj-disabled"):
                raise _TypedPermissionDenied("Cloud Monitoring API has not been used in project")
            raise _TypedPermissionDenied("Permission monitoring.metricDescriptors.list denied")

    fake_creds = SimpleNamespace(service_account_email="auditor@example.iam.gserviceaccount.com")
    with (
        patch("google.auth.default", return_value=(fake_creds, "proj-ok")),
        patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_FakeClient()),
    ):
        rep = await check_gcp_project_access(["proj-ok", "proj-disabled", "proj-noiam"])

    assert rep["authenticated"] is True
    assert rep["ok"] is False
    assert rep["projects"]["proj-ok"]["ok"] is True
    assert rep["projects"]["proj-disabled"]["monitoring_api_enabled"] is False
    assert rep["projects"]["proj-noiam"]["monitoring_api_enabled"] is True
    assert rep["projects"]["proj-noiam"]["has_viewer_role"] is False


@pytest.mark.asyncio
async def test_fetch_inventory_telemetry_converts_dedicated_limit_and_429s() -> None:
    class _Pager:
        def __init__(self, items: list[Any]) -> None:
            self._it = iter(items)

        def __aiter__(self) -> _Pager:
            return self

        async def __anext__(self) -> Any:
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    limit_ts = SimpleNamespace(
        resource=SimpleNamespace(labels={"model_id": "gemini-3.8-flash-001", "location": "global"}),
        metric=SimpleNamespace(labels={}),
        points=[SimpleNamespace(value=SimpleNamespace(int64_value=0, double_value=40_300.0))],
    )
    inv_ts = SimpleNamespace(
        resource=SimpleNamespace(labels={"model_id": "gemini-3.8-flash-001", "location": "global"}),
        metric=SimpleNamespace(labels={"response_code": "429", "request_type": "shared"}),
        points=[SimpleNamespace(value=SimpleNamespace(int64_value=250, double_value=0.0))],
    )

    class _Client:
        async def list_time_series(self, request: dict[str, Any]) -> _Pager:
            if "dedicated_token_limit" in request["filter"]:
                return _Pager([limit_ts])
            return _Pager([inv_ts])

    with patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_Client()):
        inv = await fetch_inventory_telemetry("demo-project", days=3, by_region=True)

    assert inv["gemini-3.8-flash@global"]["contracted_gsus"] == 20
    assert inv["gemini-3.8-flash@global"]["http_429_count"] == 250
    assert inv["gemini-3.8-flash@global"]["spillover_requests"] == 250
