"""Shared test fixtures: Cloud Monitoring fake, time-series builder, cache fake, callback loader."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

MONITORING_PATCH_TARGET = "capacity.telemetry.cloud_monitoring.create_monitoring_client"


class AsyncItems:
    """Async iterator emulating Cloud Monitoring's async pager."""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = list(items)
        self._idx = 0

    def __aiter__(self) -> AsyncItems:
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


def make_time_series(
    model_id: str = "gemini-3.8-flash",
    location: str = "global",
    request_type: str = "dedicated",
    points: Sequence[tuple[Any, int] | tuple[Any, int, float]] = (),
    *,
    endpoint_id: str | None = None,
    metric_type: str | None = None,
    response_code: str | None = None,
) -> SimpleNamespace:
    resource_labels: dict[str, str] = {"model_id": model_id, "location": location}
    if endpoint_id is not None:
        resource_labels["endpoint_id"] = endpoint_id
    metric_labels: dict[str, str] = {"request_type": request_type}
    if metric_type is not None:
        metric_labels["type"] = metric_type
    if response_code is not None:
        metric_labels["response_code"] = response_code

    built_points: list[SimpleNamespace] = []
    for item in points:
        if len(item) == 3:
            end_time, int_val, dbl_val = item
        else:
            end_time, int_val = item
            dbl_val = 0.0
        built_points.append(
            SimpleNamespace(
                interval=SimpleNamespace(end_time=end_time),
                value=SimpleNamespace(int64_value=int_val, double_value=dbl_val),
            )
        )
    return SimpleNamespace(
        resource=SimpleNamespace(labels=resource_labels),
        metric=SimpleNamespace(labels=metric_labels),
        points=built_points,
    )


SeriesHandler = Callable[[dict[str, Any]], Sequence[Any]] | Sequence[Any]


class FakeMonitoringClient:
    """Configurable fake `MetricServiceAsyncClient` recording requests."""

    def __init__(
        self,
        series_handler: SeriesHandler = (),
        *,
        descriptors_handler: SeriesHandler = (),
    ) -> None:
        self._series_handler = series_handler
        self._descriptors_handler = descriptors_handler
        self.requests: list[dict[str, Any]] = []
        self.descriptor_requests: list[dict[str, Any]] = []

    async def list_time_series(self, request: dict[str, Any]) -> AsyncItems:
        self.requests.append(request)
        items = (
            self._series_handler(request)
            if callable(self._series_handler)
            else self._series_handler
        )
        return AsyncItems(items)

    async def list_metric_descriptors(self, request: dict[str, Any]) -> AsyncItems:
        self.descriptor_requests.append(request)
        items = (
            self._descriptors_handler(request)
            if callable(self._descriptors_handler)
            else self._descriptors_handler
        )
        return AsyncItems(items)


class FakeAsyncCache:
    """In-memory async cache fake implementing LiteLLM's `DualCache` counter methods."""

    def __init__(self) -> None:
        self.store: dict[str, float] = {}
        self.increments: list[tuple[str, float]] = []

    async def async_get_cache(self, key: str) -> float | None:
        return self.store.get(key)

    async def async_increment(self, key: str, value: float = 1.0, ttl: int = 120) -> float:
        _ = ttl
        self.increments.append((key, value))
        self.store[key] = self.store.get(key, 0.0) + float(value)
        return self.store[key]


def load_generated_burndown_logger(source_code: str) -> type[Any]:
    """Compile and return `BurndownAndTrafficTypeLogger` from generated callback source."""
    fake_litellm = types.ModuleType("litellm")
    fake_integrations = types.ModuleType("litellm.integrations")
    fake_custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class _BaseCustomLogger:
        pass

    fake_custom_logger.CustomLogger = _BaseCustomLogger  # type: ignore[attr-defined]
    namespace: dict[str, Any] = {"__name__": "generated_litellm_burndown_callback"}
    code = compile(source_code, "litellm_burndown_callback.py", "exec")
    with patch.dict(
        sys.modules,
        {
            "litellm": fake_litellm,
            "litellm.integrations": fake_integrations,
            "litellm.integrations.custom_logger": fake_custom_logger,
        },
    ):
        exec(code, namespace)  # noqa: S102
    cls = namespace["BurndownAndTrafficTypeLogger"]
    assert isinstance(cls, type)
    return cls


@pytest.fixture
def patch_monitoring_client() -> Iterator[Callable[..., FakeMonitoringClient]]:
    active_patches: list[Any] = []

    def _install(
        series_handler: SeriesHandler = (),
        *,
        descriptors_handler: SeriesHandler = (),
        init_error: Exception | None = None,
    ) -> FakeMonitoringClient:
        fake = FakeMonitoringClient(
            series_handler=series_handler,
            descriptors_handler=descriptors_handler,
        )
        patcher = (
            patch(MONITORING_PATCH_TARGET, side_effect=init_error)
            if init_error is not None
            else patch(MONITORING_PATCH_TARGET, return_value=fake)
        )
        patcher.start()
        active_patches.append(patcher)
        return fake

    try:
        yield _install
    finally:
        for p in reversed(active_patches):
            p.stop()
