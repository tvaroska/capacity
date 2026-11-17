"""Telemetry ingestion: the same workload must size the same however it was recorded."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any, Self

import pytest

from capacity import core
from capacity.capacity import compute_recommendations
from capacity.core import fetch_throughput
from capacity.finops import compute_focus_chargeback
from capacity.forecasting import build_demo_throughput
from capacity.telemetry_parsers import parse_inference_server_telemetry as parse
from tests.conftest import FakeMonitoringClient, make_time_series

PROMPT = 'vllm:prompt_tokens_total{model_name="llama-3.3-70b-fp8"}'
GENERATED = 'vllm:generation_tokens_total{model_name="llama-3.3-70b-fp8"}'


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
    every_minute = parse(_scrapes(60, 3, 210_000, 6_000))
    every_half_minute = parse(_scrapes(30, 5, 210_000, 6_000))
    assert every_minute.tpm_series == [234_000.0, 234_000.0]
    assert every_half_minute.tpm_series == [234_000.0] * 4
    assert every_minute.weighted_burndown_tokens_total == 468_000.0


def test_single_scrape_total_is_not_priced_as_one_long_prompt() -> None:
    snap = parse(f"{PROMPT} 420000\n{GENERATED} 12000\nprocess_uptime_seconds 120\n")
    assert snap.tpm_series == [234_000.0]


def test_lone_scrape_without_uptime_is_read_as_one_minute() -> None:
    assert parse(f"{PROMPT} 120000\n").tpm_series == [120_000.0]


def test_idle_hours_between_records_count_as_zero() -> None:
    records = [
        {"timestamp": f"2026-11-08T{hour:02d}:{minute:02d}:00Z", "tenant": "claims"}
        | {"prompt_tokens": 60_000, "completion_tokens": 0}
        for hour in (9, 17)
        for minute in range(60)
    ]
    hourly = parse(_jsonl(records)).hourly_consumed_tpm_by_tenant
    assert hourly == {"claims": [60_000.0] + [0.0] * 7 + [60_000.0]}

    report = compute_focus_chargeback(hourly, provisioned_gsus=1, tpm_per_gsu=120_000)
    assert report.active_utilization_pct == pytest.approx(100 * 2 * 0.5 / 9, abs=0.01)


def test_record_without_a_timestamp_is_rejected() -> None:
    records = [{"ts": "2026-11-08T10:00:00Z", "prompt_tokens": 60_000}]
    with pytest.raises(ValueError, match="record 1 has no timestamp"):
        parse(_jsonl(records))


@pytest.mark.parametrize("stamp", ["yesterday", 1e30])
def test_unreadable_timestamp_is_rejected(stamp: object) -> None:
    with pytest.raises(ValueError, match="record 1 has an unreadable timestamp"):
        parse(_jsonl([{"timestamp": stamp, "prompt_tokens": 5}]))


def test_log_spanning_years_is_rejected_instead_of_padded() -> None:
    records = [
        {"timestamp": "2020-01-01T00:00:00Z", "prompt_tokens": 5},
        {"timestamp": "2026-01-01T00:00:00Z", "prompt_tokens": 5},
    ]
    with pytest.raises(ValueError, match="records span 52,609 hours"):
        parse(_jsonl(records))


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

    monkeypatch.setattr(core, "datetime", _FixedDatetime)

    def _serve_window(req: dict[str, Any]) -> list[SimpleNamespace]:
        if '"dedicated"' not in req["filter"]:
            return []
        iv = req["interval"]
        pts = [
            (ts + timedelta(minutes=1), val)
            for ts, val in sorted(demo_sparse["gemini-3.8-flash"].items())
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
        "gemini-3.8-flash": {
            datetime(2026, 11, 9, 23, 59, tzinfo=UTC): 100,
            datetime(2026, 11, 10, 0, 0, tzinfo=UTC): 200,
        }
    }
