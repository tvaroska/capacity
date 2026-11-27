"""Telemetry ingestion: the same workload must size the same however it was recorded."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from typer.testing import CliRunner

from capacity.cli import app as cli_app
from capacity.contract.common import SeriesKey
from capacity.contract.requests import ChargebackRequest, VllmParseRequest
from capacity.contract.results import VllmTelemetrySnapshot
from capacity.contract.scenario import ScenarioGsuSizing, ShowcaseScenario
from capacity.engine.catalog import build_model_catalog
from capacity.engine.chargeback import compute_focus_chargeback
from capacity.engine.sizing import compute_recommendations
from capacity.engine.stats import minute_list_to_sparse
from capacity.ingest.request_logs import (
    parse_bq_eval_capacity_export,
    parse_inference_server_telemetry,
)
from capacity.showcase.evaluate import evaluate_showcase_scenario
from capacity.telemetry import cloud_monitoring
from capacity.telemetry.cloud_monitoring import fetch_throughput
from capacity.telemetry.local import build_demo_throughput
from tests.conftest import FakeMonitoringClient, make_time_series

PROMPT = 'vllm:prompt_tokens_total{model_name="llama-3.3-70b-fp8"}'
GENERATED = 'vllm:generation_tokens_total{model_name="llama-3.3-70b-fp8"}'
runner = CliRunner()


def _parse(raw: str) -> VllmTelemetrySnapshot:
    return parse_inference_server_telemetry(VllmParseRequest(raw_content=raw))


def _scrapes(every_seconds: int, count: int, prompt_per_min: int, generated_per_min: int) -> str:
    lines: list[str] = []
    for i in range(count):
        elapsed_min = i * every_seconds / 60
        lines += [
            f"# scrape_timestamp_seconds {i * every_seconds}",
            f"{PROMPT} {round(prompt_per_min * elapsed_min)}",
            f"{GENERATED} {round(generated_per_min * elapsed_min)}",
        ]
    return "\n".join(lines)


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(rec) for rec in records)


def test_scrape_interval_does_not_change_the_rate() -> None:
    every_minute = _parse(_scrapes(60, 3, 210_000, 6_000))
    every_half_minute = _parse(_scrapes(30, 5, 210_000, 6_000))
    assert every_minute.tpm_series == [234_000.0, 234_000.0]
    assert every_half_minute.tpm_series == [234_000.0] * 4
    assert every_minute.weighted_burndown_tokens_total == 468_000.0


def test_single_scrape_total_is_not_priced_as_one_long_prompt() -> None:
    snap = _parse(f"{PROMPT} 420000\n{GENERATED} 12000\nprocess_uptime_seconds 120\n")
    assert snap.tpm_series == [234_000.0]


def test_lone_scrape_without_uptime_is_read_as_one_minute() -> None:
    assert _parse(f"{PROMPT} 120000\n").tpm_series == [120_000.0]


def test_idle_hours_between_records_count_as_zero() -> None:
    records = [
        {"timestamp": f"2026-11-08T{hour:02d}:{minute:02d}:00Z", "tenant": "claims"}
        | {"prompt_tokens": 60_000, "completion_tokens": 0}
        for hour in (9, 17)
        for minute in range(60)
    ]
    hourly = _parse(_jsonl(records)).hourly_consumed_tpm_by_tenant
    assert hourly == {"claims": [60_000.0] + [0.0] * 7 + [60_000.0]}

    hourly_map: dict[str, list[float] | float] = {k: list(v) for k, v in hourly.items()}
    report = compute_focus_chargeback(
        ChargebackRequest(hourly_consumed_tpm_by_tenant=hourly_map, provisioned_gsus=1),
        catalog=build_model_catalog(overrides={"gemini-3.8-flash": 120_000}),
    )
    assert report.active_utilization_pct == pytest.approx(100 * 2 * 0.5 / 9, abs=0.01)


def test_record_without_a_timestamp_is_rejected() -> None:
    records = [{"ts": "2026-11-08T10:00:00Z", "prompt_tokens": 60_000}]
    with pytest.raises(ValueError, match="record 1 has no timestamp"):
        _parse(_jsonl(records))


@pytest.mark.parametrize("stamp", ["yesterday", 1e30])
def test_unreadable_timestamp_is_rejected(stamp: object) -> None:
    with pytest.raises(ValueError, match="record 1 has an unreadable timestamp"):
        _parse(_jsonl([{"timestamp": stamp, "prompt_tokens": 5}]))


def test_log_spanning_years_is_rejected_instead_of_padded() -> None:
    records = [
        {"timestamp": "2020-01-01T00:00:00Z", "prompt_tokens": 5},
        {"timestamp": "2026-01-01T00:00:00Z", "prompt_tokens": 5},
    ]
    with pytest.raises(ValueError, match="records span 52,609 hours"):
        _parse(_jsonl(records))


def test_one_minute_list_produces_one_forecast_and_caps_duty_cycle_at_100_pct(
    tmp_path: Path,
) -> None:
    # Reproduce the exact P1.2 drift case: 2000 minutes spanning the 24h midnight boundary.
    values = [100_000.6] * 1_100 + [300_000.6] * 900
    trace_file = tmp_path / "list_trace.json"
    trace_file.write_text(json.dumps(values), encoding="utf-8")

    cli_res = runner.invoke(
        cli_app,
        ["recommend", "--trace-file", str(trace_file), "--days", "1", "--format", "json"],
    )
    assert cli_res.exit_code == 0, cli_res.output
    cli_model = json.loads(cli_res.stdout)["models"][0]

    scen = ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="same-list",
            days=1,
            by_region=False,
            minute_tpm_series={"gemini-3.8-flash": values},
        )
    )
    scen_model = evaluate_showcase_scenario(scen).gsu_recommendations.models[0]

    direct_model = compute_recommendations(
        {SeriesKey("gemini-3.8-flash"): minute_list_to_sparse(values)},
        total_minutes=max(1_440, len(values)),
    )[0]

    assert (
        cli_model["forecast_24h_peak_tpm"]
        == scen_model.forecast_24h_peak_tpm
        == direct_model.forecast_24h_peak_tpm
    )
    assert cli_model["active_hours"]["P50"]["tpm"] == 100_001
    assert cli_model["duty_cycle_pct"] == 100.0

    map_trace = tmp_path / "map_trace.json"
    map_trace.write_text(json.dumps({"gemini-3.8-flash": [120_900] * 2_000}), encoding="utf-8")
    map_res = runner.invoke(
        cli_app,
        ["recommend", "--trace-file", str(map_trace), "--days", "1", "--format", "json"],
    )
    assert map_res.exit_code == 0, map_res.output
    assert json.loads(map_res.stdout)["models"][0]["duty_cycle_pct"] == 100.0


@pytest.mark.asyncio
@pytest.mark.parametrize("query_hour", [0, 9, 12, 15, 23])
async def test_forecast_and_live_window_do_not_depend_on_query_hour(
    query_hour: int,
    monkeypatch: pytest.MonkeyPatch,
    patch_monitoring_client: Callable[..., FakeMonitoringClient],
) -> None:
    demo_sparse, _ = build_demo_throughput(days=21, by_region=False)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2026, 11, 16, query_hour, 37, 12, tzinfo=tz)

    monkeypatch.setattr(cloud_monitoring, "datetime", _FixedDatetime)

    def _serve_window(req: dict[str, Any]) -> list[SimpleNamespace]:
        if '"dedicated"' not in req["filter"]:
            return []
        iv = req["interval"]
        pts = [
            (ts + timedelta(minutes=1), val)
            for ts, val in sorted(demo_sparse[SeriesKey("gemini-3.8-flash")].items())
            if iv.start_time <= ts < iv.end_time
        ]
        return [make_time_series("gemini-3.8-flash", "global", "dedicated", pts)]

    fake = patch_monitoring_client(_serve_window)
    sparse, total_minutes = await fetch_throughput("demo-project", days=7)

    iv = fake.requests[0]["interval"]
    assert iv.start_time == datetime(2026, 11, 9, tzinfo=UTC)
    assert iv.end_time == datetime(2026, 11, 16, tzinfo=UTC)
    rec = compute_recommendations(sparse, total_minutes)[0]
    assert rec.forecast_24h_peak_tpm == 483_600
    assert rec.forecast_24h_recommended_gsus == 4


@pytest.mark.asyncio
async def test_live_query_dates_last_minute_correctly_for_datetime_and_proto_timestamps(
    patch_monitoring_client: Callable[..., FakeMonitoringClient],
) -> None:
    midnight = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    proto_stamp = SimpleNamespace(timestamp=(midnight + timedelta(minutes=1)).timestamp)
    series = make_time_series(
        "gemini-3.8-flash",
        "global",
        "dedicated",
        [(midnight, 100), (proto_stamp, 200)],
    )
    fake = patch_monitoring_client(lambda req: [series] if '"dedicated"' in req["filter"] else [])
    sparse, total_minutes = await fetch_throughput("demo-project", days=3)

    interval = fake.requests[0]["interval"]
    assert (interval.end_time.hour, interval.end_time.minute, interval.end_time.second) == (0, 0, 0)
    assert interval.end_time - interval.start_time == timedelta(days=3)
    assert total_minutes == 3 * 1_440
    assert sparse == {
        SeriesKey("gemini-3.8-flash"): {
            datetime(2026, 11, 9, 23, 59, tzinfo=UTC): 100,
            datetime(2026, 11, 10, 0, 0, tzinfo=UTC): 200,
        }
    }


def test_sparse_multi_day_jsonl_preserves_idle_minutes_and_daily_peaks(tmp_path: Path) -> None:
    log_path = tmp_path / "three_day_sparse.jsonl"
    records = [
        {
            "timestamp": "2026-11-10T12:00:00Z",
            "model": "gemini-3.8-flash",
            "prompt_tokens": 120_900,
            "completion_tokens": 0,
        },
        {
            "timestamp": "2026-11-11T12:00:00Z",
            "model": "gemini-3.8-flash",
            "prompt_tokens": 120_900,
            "completion_tokens": 30_225,
        },
        {
            "timestamp": "2026-11-12T12:04:00Z",
            "model": "gemini-3.8-flash",
            "prompt_tokens": 120_900,
            "completion_tokens": 60_450,
        },
    ]
    log_path.write_text(_jsonl(records), encoding="utf-8")

    rec_res = runner.invoke(
        cli_app,
        ["recommend", "--vllm-file", str(log_path), "--days", "1", "--format", "json"],
    )
    assert rec_res.exit_code == 0, rec_res.output
    rec_model = json.loads(rec_res.stdout)["models"][0]
    assert rec_model["total_minutes"] >= 2_885
    assert rec_model["active_minutes"] == 3
    assert rec_model["duty_cycle_pct"] < 1.0
    assert rec_model["forecast_24h_peak_tpm"] == 483_600

    opt_res = runner.invoke(
        cli_app,
        ["optimize", "--vllm-file", str(log_path), "--format", "json"],
    )
    assert opt_res.exit_code == 0, opt_res.output
    opt_payload = json.loads(opt_res.stdout)
    assert opt_payload["commitment"]["optimal_gsus"] == 0


def test_single_model_commands_reject_mixed_model_jsonl_unless_model_selected(
    tmp_path: Path,
) -> None:
    mixed_path = tmp_path / "mixed_models.jsonl"
    records = [
        {
            "timestamp": "2026-11-10T10:00:00Z",
            "model": "llama-3.3-70b-fp8",
            "prompt_tokens": 384_000,
            "completion_tokens": 0,
        },
        {
            "timestamp": "2026-11-10T10:00:00Z",
            "model": "gemma-3-27b-bf16",
            "prompt_tokens": 87_000,
            "completion_tokens": 0,
        },
    ]
    mixed_path.write_text(_jsonl(records), encoding="utf-8")

    opt_fail = runner.invoke(
        cli_app,
        ["optimize", "--mode", "self-hosted", "--vllm-file", str(mixed_path)],
    )
    assert opt_fail.exit_code == 1
    assert opt_fail.output.startswith("Error: ")
    assert len(opt_fail.output.strip().splitlines()) == 1
    assert "gemma-3-27b-bf16" in opt_fail.output
    assert "llama-3.3-70b-fp8" in opt_fail.output

    cb_fail = runner.invoke(
        cli_app,
        ["chargeback", "--mode", "self-hosted", "--vllm-file", str(mixed_path)],
    )
    assert cb_fail.exit_code == 1
    assert "gemma-3-27b-bf16" in cb_fail.output
    assert "llama-3.3-70b-fp8" in cb_fail.output

    opt_filtered = runner.invoke(
        cli_app,
        [
            "optimize",
            "--mode",
            "self-hosted",
            "--vllm-file",
            str(mixed_path),
            "--model",
            "gemma-3-27b-bf16",
            "--format",
            "json",
        ],
    )
    assert opt_filtered.exit_code == 0, opt_filtered.output
    audit = json.loads(opt_filtered.stdout)
    assert audit["profile_id"] == "gemma-3-27b-bf16"
    assert audit["p99_peak_replicas"] == 1


def test_bq_eval_hourly_export_and_schema_v1_1_round_trip() -> None:
    records = [
        {
            "usage_hour": "2026-11-22T10:00:00Z",
            "apphub_application_id": "underwriting-agent",
            "model_name": "gemini-3.8-flash",
            "status_code": "OK",
            "input_tokens": 250_000,
            "cached_input_tokens": 50_000,
            "cache_write_5m_tokens": 40_000,
            "cache_write_1h_tokens": 10_000,
            "output_tokens": 1_000,
            "thinking_tokens": 0,
            "weighted_burndown_tokens": 417_500.0,
        },
        {
            "usage_hour": "2026-11-22T11:00:00Z",
            "apphub_application_id": "underwriting-agent",
            "model_name": "gemini-3.8-flash",
            "status_code": "RATE_LIMITED",
            "input_tokens": 10_000,
            "cached_input_tokens": 2_000,
            "output_tokens": 500,
            "thinking_tokens": 100,
            "weighted_burndown_tokens": 10_600.0,
        },
        {
            "usage_hour": "2026-11-22T12:00:00Z",
            "apphub_application_id": "claims-agent",
            "model_name": "gemini-3.8-flash",
            "status_code": "TIMEOUT",
            "input_tokens": 5_000,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "weighted_burndown_tokens": 5_000.0,
        },
    ]
    snap = _parse(_jsonl(records))
    assert snap.model == "gemini-3.8-flash"
    assert snap.http_429_count == 1
    assert snap.prompt_tokens_total == 265_000
    assert snap.cached_tokens_total == 52_000
    assert snap.thoughts_tokens_total == 100
    assert snap.weighted_burndown_tokens_total == pytest.approx(433_100.0)
    assert snap.hourly_consumed_tpm_by_tenant["underwriting-agent"] == [
        round(417_500.0 / 60.0, 2),
        round(10_600.0 / 60.0, 2),
        0.0,
    ]
    assert snap.hourly_consumed_tpm_by_tenant["claims-agent"] == [
        0.0,
        0.0,
        round(5_000.0 / 60.0, 2),
    ]

    export_v11 = parse_bq_eval_capacity_export(
        json.dumps(
            {
                "schema_version": "1.1",
                "corpus_id": "m2-live",
                "model": "gemini-3.8-flash",
                "hours": ["2026-11-22T10:00:00+00:00"],
                "three_pool_chargeback": {"reconciliation_drift_usd": 0.0, "rows": []},
                "residual_schedule_request": {
                    "model": "gemini-3.8-flash",
                    "provisioned_gsus": 4,
                    "safety_headroom_ratio": 0.85,
                    "revenue_tpm_by_hour": [1000.0],
                },
                "burst_metrics_1m": [
                    {"pool_id": "pool-1", "peak_burst_ratio": 2.4, "gateway_failover_count": 1}
                ],
                "app_risk_governance": [
                    {"apphub_application_id": "underwriting-agent", "risk_tier": "RL-1"}
                ],
                "lob_model_lifecycle": [
                    {"business_unit": "commercial_lines", "model_name": "gemini-3.8-flash"}
                ],
            }
        )
    )
    assert export_v11["schema_version"] == "1.1"
    assert len(export_v11["burst_metrics_1m"]) == 1  # type: ignore[arg-type]
    assert len(export_v11["app_risk_governance"]) == 1  # type: ignore[arg-type]
    assert len(export_v11["lob_model_lifecycle"]) == 1  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Unsupported bq-eval capacity-export schema_version"):
        parse_bq_eval_capacity_export(
            {
                "schema_version": "9.9",
                "three_pool_chargeback": {},
                "residual_schedule_request": {},
            }
        )
