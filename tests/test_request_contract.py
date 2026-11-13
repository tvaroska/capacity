"""The request contract the CLI and the API share: bad input fails, it is never answered."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from typer.testing import CliRunner

from capacity import models
from capacity.api import app as api_app
from capacity.burst import evaluate_model_tier_prefix_cache
from capacity.cli import app as cli_app
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    schedule_residual_capacity,
)
from capacity.gateway import generate_gateway_bundle
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    parse_inference_server_telemetry,
    size_disaggregated_prefill_decode,
)

client = TestClient(api_app)
runner = CliRunner()

POST_ROUTES = sorted(
    route.path
    for route in api_app.routes
    if isinstance(route, APIRoute) and "POST" in route.methods
)
# The only body fields without a default.
REQUIRED_BODY: dict[str, dict[str, Any]] = {
    "/api/v1/vllm/parse": {"raw_content": ""},
    "/api/v1/chargeback": {"hourly_consumed_tpm_by_tenant": {"a": [1.0]}},
}
DUPLICATE_JOBS = [
    {"job_id": "same", "value_usd": 100.0, "burndown_tokens": 6_000_000.0, "deadline_hour": 1},
    {"job_id": "same", "value_usd": 100.0, "burndown_tokens": 6_000_000.0, "deadline_hour": 1},
]
# Request model, the domain function it feeds, and the fields the port resolves itself.
OPERATIONS: list[tuple[type[BaseModel], Callable[..., Any], set[str]]] = [
    (models.ModelTierPrefixCacheRequest, evaluate_model_tier_prefix_cache, set()),
    (models.SelfHostedAuditRequest, evaluate_self_hosted_endpoint, set()),
    (models.DisaggregatedPoolRequest, size_disaggregated_prefill_decode, set()),
    (models.VllmParseRequest, parse_inference_server_telemetry, set()),
    (models.GatewayConfigRequest, generate_gateway_bundle, set()),
    (models.ChargebackRequest, compute_focus_chargeback, {"model"}),
    (models.BatchScavengerRequest, evaluate_batch_scavenger_economics, set()),
    (models.ResidualScheduleRequest, schedule_residual_capacity, {"model"}),
    (models.FolderPoolingRequest, evaluate_folder_pooling, set()),
    (models.FinOpsKpiRequest, compute_finops_kpis, set()),
]


def test_all_eleven_post_routes_are_under_test() -> None:
    assert len(POST_ROUTES) == 11


@pytest.mark.parametrize("path", POST_ROUTES)
def test_minimal_body_is_accepted(path: str) -> None:
    assert client.post(path, json=REQUIRED_BODY.get(path, {})).status_code == 200


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
        ("/api/v1/optimize", {"spillover_tier": "gold"}),
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
    too_many = {f"t{i}": 1.0 for i in range(models.MAX_SERIES_KEYS + 1)}
    too_long = {"a": [1.0] * (models.MAX_HOURLY_POINTS + 1)}
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


def test_cli_rejects_what_the_api_rejects() -> None:
    res = runner.invoke(cli_app, ["gateway", "--gsus", "-5", "--pods", "0"])
    assert res.exit_code == 1
    assert "provisioned_gsus" in res.output and "pods" in res.output
    resp = client.post("/api/v1/gateway/config", json={"provisioned_gsus": -5, "pods": 0})
    assert resp.status_code == 422


def test_self_hosted_chargeback_uses_the_price_it_was_given() -> None:
    def total(*price: str) -> float:
        res = runner.invoke(
            cli_app,
            [
                *("chargeback", "--mode", "self-hosted", "--model", "llama-3.3-70b-fp8"),
                *("--gsus", "4", *price, "--format", "json"),
            ],
        )
        assert res.exit_code == 0, res.output
        return float(json.loads(res.stdout)["total_commitment_cost_usd"])

    assert total("--gsu-price", "2000") == pytest.approx(8_000.0)
    assert total("--gsu-price", "2000.01") == pytest.approx(8_000.04)
    assert total() == pytest.approx(4 * 18_500.0)


def test_chargeback_with_no_options_is_the_same_on_both_ports(tmp_path: Path) -> None:
    tenants = {"a": [90_000.0, 30_000.0], "b": [10_000.0, 0.0]}
    tenant_file = tmp_path / "tenants.json"
    tenant_file.write_text(json.dumps(tenants), encoding="utf-8")
    res = runner.invoke(
        cli_app, ["chargeback", "--tenant-file", str(tenant_file), "--format", "json"]
    )
    assert res.exit_code == 0, res.output
    resp = client.post("/api/v1/chargeback", json={"hourly_consumed_tpm_by_tenant": tenants})
    assert json.loads(res.stdout) == resp.json()


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


@pytest.mark.parametrize(("request_model", "function", "resolved_by_port"), OPERATIONS)
def test_request_fields_are_the_parameters_of_their_function(
    request_model: type[BaseModel], function: Callable[..., Any], resolved_by_port: set[str]
) -> None:
    params = inspect.signature(function).parameters
    for name in set(request_model.model_fields) - resolved_by_port:
        assert name in params, f"{request_model.__name__}.{name} is not a parameter"
        model_default = request_model.model_fields[name].default
        if model_default is PydanticUndefined or params[name].default is inspect.Parameter.empty:
            continue
        assert model_default == params[name].default, (
            f"{request_model.__name__}.{name} defaults to {model_default!r}, "
            f"{function.__name__} to {params[name].default!r}"
        )


@pytest.mark.parametrize(("request_model", "function"), [(m, f) for m, f, _ in OPERATIONS])
def test_function_takes_nothing_the_cli_and_api_cannot_set(
    request_model: type[BaseModel], function: Callable[..., Any]
) -> None:
    """Only the capacity a port looks up from the model may sit outside the request."""
    outside = set(inspect.signature(function).parameters) - set(request_model.model_fields)
    assert outside <= {"tpm_per_gsu"}, f"{function.__name__}: {sorted(outside)}"
