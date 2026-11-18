"""The request contract the CLI and the API share: bad input fails, and both ports match."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from capacity.api import app as api_app
from capacity.cli import app as cli_app
from capacity.contract import common

REPO_ROOT = Path(__file__).resolve().parent.parent
client = TestClient(api_app)
runner = CliRunner()

POST_ROUTES = sorted(
    route.path
    for route in api_app.routes
    if isinstance(route, APIRoute) and "POST" in route.methods
)
REQUIRED_BODY: dict[str, dict[str, Any]] = {
    "/api/v1/optimize": {"tpm_series": [241_800.0]},
    "/api/v1/self-hosted/audit": {"tpm_series": [384_000.0]},
    "/api/v1/vllm/parse": {"raw_content": ""},
    "/api/v1/chargeback": {"hourly_consumed_tpm_by_tenant": {"a": [1.0]}},
    "/api/v1/finops/schedule": {
        "deadline_jobs": [
            {"job_id": "j1", "value_usd": 100.0, "burndown_tokens": 1_000.0, "deadline_hour": 1}
        ]
    },
    "/api/v1/finops/folder-pooling": {"hourly_tpm_by_project": {"spoke-a": [120_900.0]}},
    "/api/v1/scenario/evaluate": {
        "gsu_sizing": {"minute_tpm_series": {"gemini-3.8-flash@global": [120_900.0]}}
    },
}
DUPLICATE_JOBS = [
    {"job_id": "same", "value_usd": 100.0, "burndown_tokens": 6_000_000.0, "deadline_hour": 1},
    {"job_id": "same", "value_usd": 100.0, "burndown_tokens": 6_000_000.0, "deadline_hour": 1},
]


def test_all_twelve_post_routes_are_under_test() -> None:
    assert len(POST_ROUTES) == 12


@pytest.mark.parametrize("path", POST_ROUTES)
def test_minimal_body_is_accepted(path: str) -> None:
    assert client.post(path, json=REQUIRED_BODY.get(path, {})).status_code == 200


@pytest.mark.parametrize("path", sorted(REQUIRED_BODY))
def test_empty_body_is_rejected_on_data_routes(path: str) -> None:
    assert client.post(path, json={}).status_code == 422


@pytest.mark.parametrize("path", POST_ROUTES)
def test_unknown_field_is_rejected_not_ignored(path: str) -> None:
    body = {**REQUIRED_BODY.get(path, {}), "definitely_not_a_field": 1}
    resp = client.post(path, json=body)
    assert resp.status_code == 422, resp.text
    assert [err["type"] for err in resp.json()["detail"]] == ["extra_forbidden"]


def test_misspelt_series_no_longer_gets_the_sample_answer() -> None:
    right = client.post("/api/v1/optimize", json={"tpm_series": [9_000_000.0] * 4})
    assert right.json()["commitment"]["optimal_gsus"] == 75
    assert client.post("/api/v1/optimize", json={"tpm_serie": [9_000_000.0] * 4}).status_code == 422


def test_repeated_job_id_is_rejected_on_api_and_cli(tmp_path: Path) -> None:
    resp = client.post("/api/v1/finops/schedule", json={"deadline_jobs": DUPLICATE_JOBS})
    assert resp.status_code == 422
    assert "Duplicate job_id 'same'" in resp.text

    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(DUPLICATE_JOBS), encoding="utf-8")
    res = runner.invoke(cli_app, ["schedule", "--jobs-file", str(jobs_file)])
    assert res.exit_code == 1
    assert "Duplicate job_id 'same'" in res.output


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/vllm/parse", {"raw_content": "{"}),
        ("/api/v1/vllm/parse", {"raw_content": '{"prompt_tokens": 5}'}),
        ("/api/v1/optimize", {"tpm_series": [100_000.0], "spillover_tier": "gold"}),
        ("/api/v1/prefix-cache/model-tier", {"model": "gpt-4o"}),
    ],
)
def test_bad_input_is_a_422_on_every_route(path: str, body: dict[str, Any]) -> None:
    assert client.post(path, json=body).status_code == 422


def test_schedule_accepts_200_jobs_and_rejects_201() -> None:
    def jobs(count: int) -> dict[str, Any]:
        spec = {"value_usd": 1.0, "burndown_tokens": 1.0, "deadline_hour": 1}
        return {"deadline_jobs": [{"job_id": f"j{i}", **spec} for i in range(count)]}

    assert client.post("/api/v1/finops/schedule", json=jobs(200)).status_code == 200
    assert client.post("/api/v1/finops/schedule", json=jobs(201)).status_code == 422


def test_requests_that_expand_to_a_huge_matrix_are_rejected() -> None:
    too_many = {f"t{i}": 1.0 for i in range(common.MAX_SERIES_KEYS + 1)}
    too_long = {"a": [1.0] * (common.MAX_HOURLY_POINTS + 1)}
    wide_and_long = {f"t{i}": 1.0 for i in range(999)} | {"long": [1.0] * 2_000}
    for tenants in (too_many, too_long, wide_and_long):
        resp = client.post("/api/v1/chargeback", json={"hourly_consumed_tpm_by_tenant": tenants})
        assert resp.status_code == 422, len(tenants)
    for projects in (too_long, {f"p{i}": [1.0] * 1_001 for i in range(1_000)}):
        resp = client.post(
            "/api/v1/finops/folder-pooling", json={"hourly_tpm_by_project": projects}
        )
        assert resp.status_code == 422, len(projects)

    at_the_limit = {f"t{i}": [1.0] * 1_000 for i in range(1_000)}
    ok = client.post("/api/v1/chargeback", json={"hourly_consumed_tpm_by_tenant": at_the_limit})
    assert ok.status_code == 200


def test_cli_rejects_missing_data_without_demo_and_matches_api_rejections() -> None:
    for cmd in (["optimize"], ["schedule"], ["chargeback"]):
        missing = runner.invoke(cli_app, cmd)
        assert missing.exit_code == 1
        assert missing.output.startswith("Error: ")

    res = runner.invoke(cli_app, ["gateway", "--gsus", "-5", "--pods", "0"])
    assert res.exit_code == 1
    assert "provisioned_gsus" in res.output and "pods" in res.output
    resp = client.post("/api/v1/gateway/config", json={"provisioned_gsus": -5, "pods": 0})
    assert resp.status_code == 422


def test_self_hosted_chargeback_matches_between_cli_and_api(tmp_path: Path) -> None:
    tenants = {"a": [192_000.0, 96_000.0], "b": [96_000.0, 0.0]}
    tenant_file = tmp_path / "sh_tenants.json"
    tenant_file.write_text(json.dumps(tenants), encoding="utf-8")

    def cli_report(*extra: str) -> dict[str, Any]:
        res = runner.invoke(
            cli_app,
            [
                "chargeback",
                "--mode",
                "self-hosted",
                "--model",
                "llama-3.3-70b-fp8",
                "--gsus",
                "4",
                "--tenant-file",
                str(tenant_file),
                *extra,
                "--format",
                "json",
            ],
        )
        assert res.exit_code == 0, res.output
        out: dict[str, Any] = json.loads(res.stdout)
        return out

    default_cli = cli_report()
    assert default_cli["total_commitment_cost_usd"] == pytest.approx(4 * 18_500.0)
    api_default = client.post(
        "/api/v1/chargeback",
        json={
            "mode": "self-hosted",
            "model": "llama-3.3-70b-fp8",
            "provisioned_gsus": 4,
            "hourly_consumed_tpm_by_tenant": tenants,
        },
    )
    assert api_default.status_code == 200
    assert default_cli == api_default.json()

    priced_cli = cli_report("--gsu-price", "2000.01")
    assert priced_cli["total_commitment_cost_usd"] == pytest.approx(8_000.04)
    api_priced = client.post(
        "/api/v1/chargeback",
        json={
            "mode": "self-hosted",
            "model": "llama-3.3-70b-fp8",
            "provisioned_gsus": 4,
            "gsu_monthly_price_usd": 2000.01,
            "hourly_consumed_tpm_by_tenant": tenants,
        },
    )
    assert priced_cli == api_priced.json()


def test_cli_and_api_return_identical_results_across_operations(tmp_path: Path) -> None:
    tenants = {"a": [90_000.0, 30_000.0], "b": [10_000.0, 0.0]}
    tenant_file = tmp_path / "tenants.json"
    tenant_file.write_text(json.dumps(tenants), encoding="utf-8")
    cb_cli = runner.invoke(
        cli_app, ["chargeback", "--tenant-file", str(tenant_file), "--format", "json"]
    )
    cb_api = client.post("/api/v1/chargeback", json={"hourly_consumed_tpm_by_tenant": tenants})
    assert json.loads(cb_cli.stdout) == cb_api.json()

    trace = [362_700.0, 241_800.0, 120_900.0]
    trace_file = tmp_path / "trace.json"
    trace_file.write_text(json.dumps(trace), encoding="utf-8")
    opt_cli = runner.invoke(
        cli_app, ["optimize", "--trace-file", str(trace_file), "--format", "json"]
    )
    opt_api = client.post("/api/v1/optimize", json={"tpm_series": trace})
    assert json.loads(opt_cli.stdout) == opt_api.json()

    sh_cli = runner.invoke(
        cli_app,
        ["optimize", "--mode", "self-hosted", "--trace-file", str(trace_file), "--format", "json"],
    )
    sh_api = client.post("/api/v1/self-hosted/audit", json={"tpm_series": trace})
    assert json.loads(sh_cli.stdout) == sh_api.json()

    jobs = [
        {"job_id": "j1", "value_usd": 500.0, "burndown_tokens": 5_000_000.0, "deadline_hour": 2}
    ]
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    sched_cli = runner.invoke(
        cli_app, ["schedule", "--jobs-file", str(jobs_file), "--format", "json"]
    )
    sched_api = client.post("/api/v1/finops/schedule", json={"deadline_jobs": jobs})
    assert json.loads(sched_cli.stdout) == sched_api.json()

    gw_cli = runner.invoke(cli_app, ["gateway", "--gsus", "12", "--format", "json"])
    gw_api = client.post("/api/v1/gateway/config", json={"provisioned_gsus": 12})
    assert json.loads(gw_cli.stdout) == gw_api.json()


@pytest.mark.parametrize(
    ("command", "content"),
    [
        (["optimize", "--trace-file"], "{"),
        (["optimize", "--trace-file"], '{"not": "a list"}'),
        (["optimize", "--trace-file"], '["abc"]'),
        (["schedule", "--jobs-file"], "{"),
        (["schedule", "--jobs-file"], '{"job_id": "x"}'),
        (["chargeback", "--tenant-file"], "[1, 2]"),
        (["chargeback", "--tenant-file"], '{"a": {"x": 1}}'),
        (["chargeback", "--tenant-file"], '{"a": [-5]}'),
        (["recommend", "--vllm-file"], "{"),
        (["chargeback", "--vllm-file"], '{"prompt_tokens": 5}'),
        (["recommend", "--trace-file"], '"text"'),
    ],
)
def test_bad_input_file_is_one_error_line(tmp_path: Path, command: list[str], content: str) -> None:
    path = tmp_path / "input"
    path.write_text(content, encoding="utf-8")
    res = runner.invoke(cli_app, [*command, str(path)])
    assert res.exit_code == 1, res.output
    assert res.output.startswith("Error: ")
    assert isinstance(res.exception, SystemExit)


def test_missing_input_file_is_one_error_line(tmp_path: Path) -> None:
    res = runner.invoke(cli_app, ["optimize", "--trace-file", str(tmp_path / "absent.json")])
    assert res.exit_code == 1
    assert res.output.startswith("Error: ")


def test_demo_honours_the_model_filter_on_both_ports() -> None:
    res = runner.invoke(
        cli_app, ["recommend", "--demo", "--model", "gemini-4-argon", "--format", "json"]
    )
    assert res.exit_code == 0, res.output
    assert {m["model"] for m in json.loads(res.stdout)["models"]} == {"gemini-4-argon"}

    resp = client.get("/api/v1/recommendations?demo=true&model=gemini-4-argon")
    assert {m["model"] for m in resp.json()["models"]} == {"gemini-4-argon"}


def test_serve_listens_on_loopback_unless_told_otherwise() -> None:
    res = runner.invoke(cli_app, ["serve", "--help"])
    assert "127.0.0.1" in res.output


def test_importing_cli_and_api_neither_loads_cloud_sdk_nor_reads_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GSU_CAPACITY_SYNTHETIC_DOTENV_PROBE=999999\n", encoding="utf-8")
    probe = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import capacity.cli, capacity.api\n"
        "sdk_mods = sorted(m for m in sys.modules if m.startswith(('google.cloud', 'grpc')))\n"
        "print(json.dumps({'sdk': sdk_mods, 'env': 'GSU_CAPACITY_SYNTHETIC_DOTENV_PROBE' in os.environ}))\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout.strip())
    assert data["sdk"] == []
    assert data["env"] is False
