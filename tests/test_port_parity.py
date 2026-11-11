"""Hexagonal `@pytest.mark.port` parity tests across `LocalTraceAdapter` and `CloudMonitoringAdapter`."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from capacity.capacity import compute_recommendations
from capacity.core import CloudMonitoringAdapter
from capacity.forecasting import LocalTraceAdapter, TelemetryPort


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


def _ts(model: str, loc: str, req_type: str, pts: list[tuple[datetime, int]]) -> SimpleNamespace:
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


@pytest.mark.port
@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["local", "cloud"])
async def test_telemetry_port_adapters_satisfy_identical_contract(adapter_kind: str) -> None:
    t0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

    if adapter_kind == "local":
        port: TelemetryPort = LocalTraceAdapter(
            sparse_series={
                "gemini-3.8-flash@us-central1": {t0: 241_800, t1: 362_700},
                "gemini-3.8-flash@europe-west4": {t0: 120_900},
            },
            total_minutes=2 * 1_440,
        )
        assert isinstance(port, TelemetryPort)
        sparse, total_mins = await port.fetch_series(
            project=["banking-host-01"],
            days=2,
            regions=["us-central1"],
            by_region=True,
            folder_map={"banking-host-01": "wealth"},
        )
    else:
        dedicated = [
            _ts("gemini-3.8-flash", "us-central1", "dedicated", [(t0, 200_000), (t1, 300_000)])
        ]
        shared = [_ts("gemini-3.8-flash", "us-central1", "shared", [(t0, 41_800), (t1, 62_700)])]

        class _FakeMonitoringClient:
            async def list_time_series(self, request: dict[str, Any]) -> _AsyncItems:
                if '"dedicated"' in request["filter"]:
                    return _AsyncItems(dedicated)
                return _AsyncItems(shared)

        port = CloudMonitoringAdapter()
        assert isinstance(port, TelemetryPort)
        with patch(
            "capacity.core.monitoring_v3.MetricServiceAsyncClient",
            return_value=_FakeMonitoringClient(),
        ):
            sparse, total_mins = await port.fetch_series(
                project=["banking-host-01"],
                days=2,
                regions=["us-central1"],
                by_region=True,
                folder_map={"banking-host-01": "wealth"},
            )

    assert total_mins == 2_880
    assert list(sparse.keys()) == ["wealth/gemini-3.8-flash@us-central1"]
    recs = compute_recommendations(
        sparse, total_minutes=total_mins, percentiles=[50, 95], active_only=True, by_region=True
    )
    assert len(recs) == 1
    assert recs[0].folder == "wealth"
    assert recs[0].region == "us-central1"
    assert recs[0].active_hours["P95"].recommended_gsus == 3
    assert recs[0].forecast_24h_peak_tpm > 362_700
