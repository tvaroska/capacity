"""Shared wire primitives, identifier patterns, series keys, and domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    PACKAGE_VERSION = version("capacity")
except PackageNotFoundError:  # pragma: no cover
    PACKAGE_VERSION = "0.6.1"

# Interpolated into generated YAML/Kubernetes manifests and Cloud Monitoring filters;
# reject quotes, newlines, spaces, or injection tokens at validation boundaries.
GCP_PROJECT_ID_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
GCP_LOCATION_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.@/_-]{0,127}$"
K8S_NAME_PATTERN = r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$"

BoundedTpmFloat = Annotated[float, Field(ge=0.0, le=1e11, allow_inf_nan=False)]
FiniteNonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FinitePosFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

# Size bounds: one request must not be able to make the service compute for seconds.
MAX_HOURLY_POINTS = 8_784  # hours in a leap year
MAX_SERIES_POINTS = 60_480  # 42 days of 1-minute buckets or 7 days of 10-second buckets
MAX_SERIES_KEYS = 1_000  # tenants or projects in one request
MAX_SERIES_CELLS = 1_000_000  # keys times the longest series: the padded matrix
HourlyTpmSeries = Annotated[list[BoundedTpmFloat], Field(max_length=MAX_HOURLY_POINTS)]

PublisherName = Literal["google", "anthropic", "self_hosted"]
ModelFamilyName = Literal["gemini_1p", "claude_3p", "self_hosted_vllm"]
LifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]
SpilloverTier = Literal["priority", "standard", "flex"]
CapacityModeLiteral = Literal["gsu", "self-hosted"]

DEFAULT_OVERNIGHT_REVENUE_TPM_BY_HOUR: tuple[float, ...] = (
    600_000.0,
    400_000.0,
    300_000.0,
    250_000.0,
    200_000.0,
    350_000.0,
    900_000.0,
    1_800_000.0,
)


class UnknownModelError(ValueError):
    """Raised when a model or self-hosted profile has no known capacity, instead of guessing."""


@dataclass(frozen=True, order=True)
class SeriesKey:
    """Typed telemetry series identifier replacing ad-hoc `folder/model@region` string splitting."""

    model: str
    region: str | None = None
    folder: str | None = None

    @classmethod
    def parse(cls, raw: str, *, by_region: bool = True) -> SeriesKey:
        cleaned = raw.strip()
        if "@" in cleaned:
            model_part, reg_raw = cleaned.split("@", 1)
            region: str | None = reg_raw if by_region else None
        else:
            model_part = cleaned
            region = "global" if by_region else None

        if "/" in model_part and not model_part.startswith("publishers/"):
            folder_part, model_id = model_part.split("/", 1)
            folder: str | None = folder_part or None
        else:
            folder, model_id = None, model_part
        return cls(model=model_id, region=region, folder=folder)

    def format(self, *, by_region: bool = True) -> str:
        prefix = f"{self.folder}/" if self.folder else ""
        if by_region:
            loc = self.region or "global"
            return f"{prefix}{self.model}@{loc}"
        return f"{prefix}{self.model}"


class StrictModel(BaseModel):
    """Base wire model: a field the schema does not define is an error, never ignored."""

    model_config = ConfigDict(extra="forbid")


def reject_oversized_matrix(series_by_key: dict[str, Any]) -> None:
    list_lengths = {len(v) for v in series_by_key.values() if isinstance(v, list)}
    if 0 in list_lengths:
        raise ValueError("Hourly series must contain at least 1 hour; got an empty list")
    if len(list_lengths) > 1:
        raise ValueError(
            f"Hourly series must all have the same length; got lengths {sorted(list_lengths)}"
        )
    longest = max(list_lengths, default=1)
    cells = len(series_by_key) * max(1, longest)
    if cells > MAX_SERIES_CELLS:
        raise ValueError(
            f"{len(series_by_key)} series padded to {longest} points is {cells:,} cells; "
            f"the limit is {MAX_SERIES_CELLS:,}"
        )
