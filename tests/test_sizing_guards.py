"""Guards against wrong sizing numbers and unsafe generated configs."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from capacity.api import app as api_app
from capacity.capacity import compute_recommendations, require_capacity
from capacity.cli import app as cli_app
from capacity.core import UnknownModelError, build_demo_throughput
from capacity.gateway import generate_gateway_bundle
from capacity.self_hosted import resolve_self_hosted_profile

client = TestClient(api_app)
runner = CliRunner()


def test_forecast_tracks_daily_peak_not_last_minutes() -> None:
    sparse, total_minutes = build_demo_throughput(days=7, by_region=True)
    recs = {
        r.model + "@" + str(r.region): r
        for r in compute_recommendations(sparse, total_minutes, by_region=True)
    }
    expected = {
        "gemini-3.8-flash@us-central1": (483_600, 4),
        "gemini-3.8-flash@europe-west4": (241_800, 2),
        "gemini-4-argon@us-central1": (90_000, 3),
    }
    for key, (peak_tpm, gsus) in expected.items():
        assert recs[key].forecast_24h_peak_tpm == peak_tpm
        assert recs[key].forecast_24h_recommended_gsus == gsus
        assert recs[key].forecast_24h_peak_tpm >= recs[key].active_hours["P95"].tpm


def test_forecast_follows_rising_daily_peaks() -> None:
    sparse, total_minutes = build_demo_throughput(days=5, by_region=False)
    series = sparse["gemini-3.8-flash"]
    first_day = min(series).date()
    grown = {ts: int(v * (1.0 + 0.1 * (ts.date() - first_day).days)) for ts, v in series.items()}
    rec = compute_recommendations({"gemini-3.8-flash": grown}, total_minutes)[0]
    assert rec.forecast_24h_peak_tpm > max(grown.values())


def test_unknown_model_raises_instead_of_defaulting() -> None:
    with pytest.raises(UnknownModelError, match="GSU_CAPACITY_CLAUDE_SONNET_9"):
        require_capacity("claude-sonnet-9")


def test_unknown_model_env_override_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSU_CAPACITY_ACME_LLM_2", "77000")
    assert require_capacity("acme-llm-2") == 77_000
    bundle = generate_gateway_bundle(model="acme-llm-2", provisioned_gsus=2, target_rps=10)
    assert "154,000 TPM" in bundle.litellm_config_yaml


@pytest.mark.parametrize("profile", ["deepseek-r1", "g", "mixtral-8x22b"])
def test_unknown_self_hosted_profiles_raise(profile: str) -> None:
    with pytest.raises(UnknownModelError):
        resolve_self_hosted_profile(profile)


def test_suffixed_self_hosted_profile_still_resolves() -> None:
    assert resolve_self_hosted_profile("llama-3.3-70b-fp8-v2@us-central1").profile_id == (
        "llama-3.3-70b-fp8"
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/optimize", {"model": "claude-sonnet-9", "tpm_series": [1.0]}),
        (
            "post",
            "/api/v1/chargeback",
            {
                "model": "claude-sonnet-9",
                "hourly_consumed_tpm_by_tenant": {"a": [1.0]},
                "provisioned_gsus": 1,
            },
        ),
        ("post", "/api/v1/finops/scavenger", {"model": "claude-sonnet-9"}),
        ("post", "/api/v1/finops/schedule", {"model": "claude-sonnet-9"}),
        ("post", "/api/v1/finops/folder-pooling", {"model": "claude-sonnet-9"}),
        ("post", "/api/v1/gateway/config", {"model": "claude-sonnet-9"}),
        ("post", "/api/v1/self-hosted/audit", {"profile": "deepseek-r1"}),
        ("post", "/api/v1/self-hosted/disaggregated", {"profile": "deepseek-r1"}),
    ],
)
def test_api_returns_422_for_unknown_models(
    method: str, path: str, body: dict[str, object]
) -> None:
    resp = getattr(client, method)(path, json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("No "), resp.text


def test_api_rejects_unknown_mode() -> None:
    resp = client.get("/api/v1/recommendations?demo=true&mode=gpu")
    assert resp.status_code == 422


def test_cli_reports_unknown_model_without_traceback() -> None:
    res = runner.invoke(cli_app, ["optimize", "--model", "claude-sonnet-9"])
    assert res.exit_code == 1
    assert "No capacity table entry" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_generated_gateway_configs_parse_and_compile() -> None:
    bundle = generate_gateway_bundle(model="gemini-3.8-flash-001", provisioned_gsus=20)
    assert len(list(yaml.safe_load_all(bundle.litellm_config_yaml))) == 1
    assert len(list(yaml.safe_load_all(bundle.envoy_ai_gateway_yaml))) == 2
    assert len(list(yaml.safe_load_all(bundle.gke_epp_inference_pool_yaml))) == 3
    compile(bundle.litellm_burndown_callback_py, "litellm_burndown_callback.py", "exec")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", 'x"\n      api_base: "https://evil.example'),
        ("project_id", "Prod_Project"),
        ("location", "us-central1\nkind: Secret"),
        ("model", 'gemini-3.8-flash"'),
        ("self_hosted_model", "Llama/70B"),
    ],
)
def test_gateway_rejects_yaml_injection(field: str, value: str) -> None:
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValueError, match=f"Invalid {field}"):
        generate_gateway_bundle(**kwargs)
    resp = client.post("/api/v1/gateway/config", json={field: value})
    assert resp.status_code == 422
