"""Hexagonal `@pytest.mark.port` parity tests across `LocalTraceAdapter` and `CloudMonitoringAdapter`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from capacity.capacity import compute_recommendations
from capacity.core import CloudMonitoringAdapter
from capacity.forecasting import LocalTraceAdapter, TelemetryPort
from capacity.models import SeriesKey
from tests.conftest import FakeMonitoringClient, make_time_series


@pytest.mark.port
@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["local", "cloud"])
async def test_telemetry_port_adapters_satisfy_identical_contract(
    adapter_kind: str,
    patch_monitoring_client: Callable[..., FakeMonitoringClient],
) -> None:
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
            make_time_series(
                "gemini-3.8-flash", "us-central1", "dedicated", [(t0, 200_000), (t1, 300_000)]
            )
        ]
        shared = [
            make_time_series(
                "gemini-3.8-flash", "us-central1", "shared", [(t0, 41_800), (t1, 62_700)]
            )
        ]
        patch_monitoring_client(lambda req: dedicated if '"dedicated"' in req["filter"] else shared)
        port = CloudMonitoringAdapter()
        assert isinstance(port, TelemetryPort)
        sparse, total_mins = await port.fetch_series(
            project=["banking-host-01"],
            days=2,
            regions=["us-central1"],
            by_region=True,
            folder_map={"banking-host-01": "wealth"},
        )

    assert total_mins == 2_880
    assert list(sparse.keys()) == [
        SeriesKey(model="gemini-3.8-flash", region="us-central1", folder="wealth")
    ]
    recs = compute_recommendations(
        sparse, total_minutes=total_mins, percentiles=[50, 95], active_only=True, by_region=True
    )
    assert len(recs) == 1
    assert recs[0].folder == "wealth"
    assert recs[0].region == "us-central1"
    assert recs[0].active_hours["P95"].recommended_gsus == 3
    assert recs[0].forecast_24h_peak_tpm > 362_700
