"""Milestone 1 (`P0.3` & `P0.4`) tests covering every refactor cut-point and fixture invariant."""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import GoogleAPICallError, PermissionDenied
from google.auth.exceptions import DefaultCredentialsError
from pydantic import ValidationError
from typer.testing import CliRunner

from capacity import core
from capacity.api import app as api_app
from capacity.cli import app as cli_app
from capacity.cli_formatters import load_sparse_from_trace_file, parse_folder_map
from capacity.models import OptimizeRequest, SelfHostedAuditRequest
from capacity.scenario import (
    ModelInventoryAnnotation,
    ShowcaseScenario,
    compile_scenario_from_telemetry,
    infer_lifecycle_track,
    load_showcase_scenario,
)
from tests.conftest import FakeMonitoringClient, make_time_series

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
runner = CliRunner()
client = TestClient(api_app)


class _FakeCredentialsError(DefaultCredentialsError):
    def __init__(self, msg: str) -> None:
        Exception.__init__(self, msg)


class _FakePermissionDenied(PermissionDenied):
    def __init__(self, msg: str) -> None:
        Exception.__init__(self, msg)
        self.message = msg


class _FakeAPICallError(GoogleAPICallError):
    def __init__(self, msg: str) -> None:
        Exception.__init__(self, msg)
        self.message = msg


def test_patch_target_appears_once_and_tests_import_no_private_capacity_names() -> None:
    target = "capacity.core.monitoring_v3.MetricServiceAsyncClient"
    hits = [
        path.name
        for path in sorted(TESTS_DIR.glob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if target in line and path.name != "test_refactor_cutpoints.py"
    ]
    assert hits == ["conftest.py"], f"Patch target duplicated across: {hits}"
    assert "_build_filter" not in core.__all__

    private_imports: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("capacity"):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        private_imports.append(f"{path.name}: {node.module}.{alias.name}")
    assert not private_imports, f"Tests import private symbols: {private_imports}"


@pytest.mark.parametrize(
    ("kind", "exc", "expected_snippet"),
    [
        ("init_creds", _FakeCredentialsError("no ADC"), "credentials not found"),
        ("query_creds", _FakeCredentialsError("expired ADC"), "credentials not found"),
        ("permission", _FakePermissionDenied("IAM denied"), "Permission denied"),
        ("api_error", _FakeAPICallError("backend unavailable"), "Cloud Monitoring API error"),
    ],
)
def test_cloud_monitoring_errors_exit_1_on_cli_and_return_502_on_api(
    kind: str,
    exc: Exception,
    expected_snippet: str,
    patch_monitoring_client: Callable[..., FakeMonitoringClient],
) -> None:
    if kind == "init_creds":
        patch_monitoring_client(init_error=exc)
    else:

        def _raise(_req: object) -> list[object]:
            raise exc

        patch_monitoring_client(_raise)

    cli_res = runner.invoke(cli_app, ["recommend", "--project", "demo-project"])
    assert cli_res.exit_code == 1
    assert cli_res.output.startswith("Error: ")
    assert expected_snippet in cli_res.output

    collect_res = runner.invoke(cli_app, ["collect", "--project", "demo-project"])
    assert collect_res.exit_code == 1
    assert expected_snippet in collect_res.output

    api_res = client.get("/api/v1/recommendations?project=demo-project")
    assert api_res.status_code == 502
    assert expected_snippet in api_res.json()["detail"]


def test_folder_map_parsing_and_cli_integration() -> None:
    assert parse_folder_map(None) == {}
    assert parse_folder_map("   ") == {}
    assert parse_folder_map("proj-a=claims, proj-b = wealth") == {
        "proj-a": "claims",
        "proj-b": "wealth",
    }
    assert parse_folder_map('{"proj-a": "claims", "proj-b": "wealth"}') == {
        "proj-a": "claims",
        "proj-b": "wealth",
    }

    res = runner.invoke(
        cli_app,
        [
            "recommend",
            "--demo",
            "--project",
            "demo-proj",
            "--folder-map",
            "demo-proj=retail-banking",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 0, res.output
    models = json.loads(res.stdout)["models"]
    assert all(m["folder"] == "retail-banking" for m in models)


def test_mapping_shaped_trace_file_and_unsupported_structure(tmp_path: Path) -> None:
    mapping_trace = tmp_path / "mapping_trace.json"
    mapping_trace.write_text(
        json.dumps(
            {
                "claims/gemini-3.8-flash@us-central1": [241_800, 362_700, 120_900],
                "wealth/gemini-4-argon@europe-west4": [60_000, 90_000, 30_000],
            }
        ),
        encoding="utf-8",
    )
    sparse, total_mins = load_sparse_from_trace_file(mapping_trace, days=1, by_region=True)
    assert total_mins == 1_440
    assert set(sparse) == {
        "claims/gemini-3.8-flash@us-central1",
        "wealth/gemini-4-argon@europe-west4",
    }

    res = runner.invoke(
        cli_app,
        ["recommend", "--trace-file", str(mapping_trace), "--by-region", "--format", "json"],
    )
    assert res.exit_code == 0, res.output
    by_model = {m["model"]: m for m in json.loads(res.stdout)["models"]}
    assert by_model["gemini-3.8-flash"]["folder"] == "claims"
    assert by_model["gemini-4-argon"]["region"] == "europe-west4"

    scalar_trace = tmp_path / "scalar.json"
    scalar_trace.write_text("42", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported trace file structure"):
        load_sparse_from_trace_file(scalar_trace, days=1, by_region=False)


def test_all_cli_json_format_branches_return_valid_json() -> None:
    commands = [
        (["demo", "--format", "json"], "finops_kpis"),
        (["recommend", "--demo", "--format", "json"], "models"),
        (["recommend", "--demo", "--mode", "self-hosted", "--format", "json"], "models"),
        (["optimize", "--format", "json"], "commitment"),
        (
            [
                "optimize",
                "--mode",
                "self-hosted",
                "--model",
                "llama-3.3-70b-fp8",
                "--format",
                "json",
            ],
            "optimal_reserved_replicas",
        ),
        (["gateway", "--gsus", "10", "--format", "json"], "litellm_config_yaml"),
        (["schedule", "--gsus", "10", "--format", "json"], "jobs"),
        (["chargeback", "--gsus", "10", "--format", "json"], "tenants"),
        (
            [
                "collect",
                "--logs-file",
                str(REPO_ROOT / "examples/otel_genai_spans.jsonl"),
                "--format",
                "json",
            ],
            "gsu_sizing",
        ),
    ]
    for argv, expected_key in commands:
        res = runner.invoke(cli_app, argv)
        assert res.exit_code == 0, f"{argv} failed: {res.output}"
        parsed = json.loads(res.stdout)
        assert expected_key in parsed, f"{argv} missing {expected_key!r}"


def test_compile_scenario_from_live_series_and_lifecycle_tracks() -> None:
    t0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    sparse = {
        "gemini-4-carbon-001@us-central1": {t0 + timedelta(minutes=i): 90_000 for i in range(10)},
        "claude-sonnet-4-6@us-east5": {t0 + timedelta(minutes=i): 210_000 for i in range(10)},
    }
    inventory = {
        "claude-sonnet-4-6@us-east5": {
            "contracted_gsus": 12,
            "spillover_requests": 40,
            "http_429_count": 3,
        }
    }
    overlay = {
        "gemini-4-carbon-001@us-central1": ModelInventoryAnnotation(
            contracted_gsus=4,
            renewal_mode="fixed_term",
            expiry_date="2027-01-15",
        )
    }

    compiled = compile_scenario_from_telemetry(
        project_label="hub-prod",
        days=1,
        by_region=True,
        sparse_series=sparse,
        total_minutes=1_440,
        inventory_telemetry=inventory,
        orders_overlay=overlay,
    )
    assert compiled.burst_and_cache.default_model == "claude-sonnet-4-6"
    assert compiled.burst_and_cache.provisioned_gsus == 12
    assert compiled.finops.commitment.spillover_tier == "standard"
    ann = compiled.gsu_sizing.inventory_annotations
    assert ann["gemini-4-carbon-001@us-central1"].lifecycle_track == "short_term_ga"
    assert ann["claude-sonnet-4-6@us-east5"].http_429_count == 3

    with pytest.raises(ValueError, match="No throughput series found"):
        compile_scenario_from_telemetry(project_label="empty-hub")


@pytest.mark.parametrize(
    ("model_id", "expected_track"),
    [
        ("gemini-3.8-flash-preview-05-06", "preview"),
        ("gemini-3.5-flash-exp-0827", "preview"),
        ("gemini-4-carbon-001", "short_term_ga"),
        ("gemini-2.0-flash", "legacy_sunset"),
        ("gemini-2.5-pro-001", "legacy_sunset"),
        ("claude-3-5-sonnet-v2@20241022", "legacy_sunset"),
        ("gemini-3.8-flash-001", "long_term_ga"),
        ("gemini-4-argon", "long_term_ga"),
        ("claude-sonnet-4-6", "long_term_ga"),
        ("claude-opus-5-5", "long_term_ga"),
    ],
)
def test_infer_lifecycle_track_classes(model_id: str, expected_track: str) -> None:
    assert infer_lifecycle_track(model_id) == expected_track


@pytest.mark.parametrize("bad_num", [math.nan, math.inf, -math.inf])
def test_nan_and_infinity_rejected_across_models_scenario_and_cli(
    bad_num: float, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError):
        OptimizeRequest(tpm_series=[bad_num])
    with pytest.raises(ValidationError):
        SelfHostedAuditRequest(tpm_series=[bad_num])
    with pytest.raises(ValidationError):
        ShowcaseScenario.model_validate(
            {"gsu_sizing": {"minute_tpm_series": {"gemini-3.8-flash": [bad_num]}}}
        )

    json_token = "NaN" if math.isnan(bad_num) else ("Infinity" if bad_num > 0 else "-Infinity")
    yaml_token = ".nan" if math.isnan(bad_num) else (".inf" if bad_num > 0 else "-.inf")

    trace_path = tmp_path / "nonfinite.json"
    trace_path.write_text(f"[{json_token}]", encoding="utf-8")
    cli_res = runner.invoke(cli_app, ["optimize", "--trace-file", str(trace_path)])
    assert cli_res.exit_code == 1
    assert cli_res.output.startswith("Error: ")

    scen_path = tmp_path / "nonfinite.yaml"
    scen_path.write_text(
        f"gsu_sizing:\n  minute_tpm_series:\n    gemini-3.8-flash:\n      - {yaml_token}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_showcase_scenario(scen_path)
    demo_res = runner.invoke(cli_app, ["demo", "--scenario-file", str(scen_path)])
    assert demo_res.exit_code == 1
    assert demo_res.output.startswith("Error: ")


def test_recommend_html_out_writes_scorecard_with_recommended_models(tmp_path: Path) -> None:
    out_html = tmp_path / "recommend_scorecard.html"
    res = runner.invoke(
        cli_app,
        ["recommend", "--demo", "--active-only", "--by-region", "--html-out", str(out_html)],
    )
    assert res.exit_code == 0, res.output
    html = out_html.read_text(encoding="utf-8")
    assert "gemini-3.8-flash" in html
    assert "gemini-4-argon" in html
    assert re.findall(r"__[A-Z0-9_]+__", html) == []


def test_collect_cli_check_auth_and_live_project_paths(
    tmp_path: Path,
    patch_monitoring_client: Callable[..., FakeMonitoringClient],
) -> None:
    # 1. Missing both --project and --logs-file fails cleanly
    res_missing = runner.invoke(cli_app, ["collect"])
    assert res_missing.exit_code == 1
    assert "provide `--project <id>`" in res_missing.output

    # 2. --check-auth unauthenticated -> exit 1 + remediation
    with patch("google.auth.default", side_effect=_FakeCredentialsError("missing ADC")):
        res_unauth = runner.invoke(cli_app, ["collect", "--project", "proj-a", "--check-auth"])
    assert res_unauth.exit_code == 1
    assert "authenticated=False" in res_unauth.output
    assert "gcloud auth application-default login" in res_unauth.output

    # 3. --check-auth healthy -> exit 0 in both table and --format json
    fake_creds = SimpleNamespace(service_account_email="viewer@example.iam.gserviceaccount.com")
    patch_monitoring_client(descriptors_handler=())
    with patch("google.auth.default", return_value=(fake_creds, "proj-ok")):
        res_ok_tbl = runner.invoke(cli_app, ["collect", "--project", "proj-ok", "--check-auth"])
        res_ok_json = runner.invoke(
            cli_app, ["collect", "--project", "proj-ok", "--check-auth", "--format", "json"]
        )
    assert res_ok_tbl.exit_code == 0, res_ok_tbl.output
    assert "proj-ok: OK" in res_ok_tbl.output
    assert res_ok_json.exit_code == 0, res_ok_json.output
    assert json.loads(res_ok_json.stdout)["ok"] is True

    # 4. Live --project collection -> compiles ShowcaseScenario and enforces --strict-public-check
    t0 = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    pt_series = make_time_series("gemini-3.8-flash", "us-central1", "dedicated", [(t0, 241_800)])
    limit_series = make_time_series(
        "gemini-3.8-flash", "us-central1", "dedicated", [(t0, 0, 8_060.0)]
    )
    inv_series = make_time_series(
        "gemini-3.8-flash", "us-central1", "shared", [(t0, 15)], response_code="429"
    )

    def _live_handler(req: dict[str, object]) -> list[SimpleNamespace]:
        flt = str(req["filter"])
        if "dedicated_token_limit" in flt:
            return [limit_series]
        if "model_invocation_count" in flt:
            return [inv_series]
        if '"dedicated"' in flt:
            return [pt_series]
        return []

    patch_monitoring_client(_live_handler)
    out_file = tmp_path / "live_scenario.json"
    res_live = runner.invoke(
        cli_app,
        [
            "collect",
            "--project",
            "demo-project",
            "--days",
            "1",
            "--project-label",
            "clean-enterprise-hub",
            "--strict-public-check",
            "--out",
            str(out_file),
        ],
    )
    assert res_live.exit_code == 0, res_live.output
    assert "Compiled ShowcaseScenario (clean-enterprise-hub)" in res_live.output
    compiled = json.loads(out_file.read_text(encoding="utf-8"))
    ann = compiled["gsu_sizing"]["inventory_annotations"]["gemini-3.8-flash@us-central1"]
    assert ann["contracted_gsus"] == 4
    assert ann["http_429_count"] == 15

    res_leak = runner.invoke(
        cli_app,
        [
            "collect",
            "--project",
            "demo-project",
            "--days",
            "1",
            "--project-label",
            "leaked-108505117801",
            "--strict-public-check",
        ],
    )
    assert res_leak.exit_code == 1
    assert "failed --strict-public-check" in res_leak.output
