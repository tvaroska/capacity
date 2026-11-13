"""Telemetry ingestion: the same workload must size the same however it was recorded."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from capacity.capacity import compute_recommendations
from capacity.core import _minute_start, _whole_day_window, fetch_throughput
from capacity.finops import compute_focus_chargeback
from capacity.forecasting import build_demo_throughput
from capacity.telemetry_parsers import parse_inference_server_telemetry as parse

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
    # 210,000 prompt + 6,000 generated tokens a minute = 210,000 + 4 x 6,000 burndown TPM.
    # A minute's total is above 200,000 tokens, yet it is not one long-context request.
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


def test_live_window_is_the_last_whole_utc_days() -> None:
    start, end = _whole_day_window(datetime(2026, 11, 16, 15, 37, 12, tzinfo=UTC), days=7)
    assert start == datetime(2026, 11, 9, tzinfo=UTC)
    assert end == datetime(2026, 11, 16, tzinfo=UTC)


def test_point_is_keyed_by_the_minute_it_covers() -> None:
    midnight = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    assert _minute_start(midnight) == datetime(2026, 11, 9, 23, 59, tzinfo=UTC)
    assert _minute_start(SimpleNamespace(timestamp=midnight.timestamp)) == _minute_start(midnight)


@pytest.mark.parametrize("query_hour", [0, 9, 12, 15, 23])
def test_forecast_does_not_depend_on_the_hour_of_the_query(query_hour: int) -> None:
    sparse, _ = build_demo_throughput(days=21, by_region=False)
    now = datetime(2026, 11, 16, query_hour, 0, tzinfo=UTC)
    start, end = _whole_day_window(now, days=7)
    window = {ts: v for ts, v in sparse["gemini-3.8-flash"].items() if start <= ts < end}
    rec = compute_recommendations({"gemini-3.8-flash": window}, 7 * 1_440)[0]
    assert rec.forecast_24h_peak_tpm == 483_600
    assert rec.forecast_24h_recommended_gsus == 4


class _Series:
    def __init__(self, items: list[SimpleNamespace]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _Series:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


@pytest.mark.asyncio
async def test_live_query_asks_for_whole_days_and_dates_the_last_minute_correctly() -> None:
    requests: list[dict[str, Any]] = []
    last_minute_of_the_9th = SimpleNamespace(
        resource=SimpleNamespace(labels={"model_id": "gemini-3.8-flash", "location": "global"}),
        metric=SimpleNamespace(labels={"request_type": "dedicated"}),
        points=[
            SimpleNamespace(
                interval=SimpleNamespace(end_time=datetime(2026, 11, 10, 0, 0, tzinfo=UTC)),
                value=SimpleNamespace(int64_value=100, double_value=0.0),
            )
        ],
    )

    class _Client:
        async def list_time_series(self, request: dict[str, Any]) -> _Series:
            requests.append(request)
            dedicated = '"dedicated"' in request["filter"]
            return _Series([last_minute_of_the_9th] if dedicated else [])

    with patch("capacity.core.monitoring_v3.MetricServiceAsyncClient", return_value=_Client()):
        sparse, total_minutes = await fetch_throughput("demo-project", days=3)

    interval = requests[0]["interval"]
    assert (interval.end_time.hour, interval.end_time.minute, interval.end_time.second) == (0, 0, 0)
    assert interval.end_time - interval.start_time == timedelta(days=3)
    assert total_minutes == 3 * 1_440
    assert sparse == {"gemini-3.8-flash": {datetime(2026, 11, 9, 23, 59, tzinfo=UTC): 100}}
