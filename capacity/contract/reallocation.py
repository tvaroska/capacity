"""Typed Pydantic v2 contracts for critical spillover windows and GSU reallocation plans."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from capacity.contract.common import StrictModel

TargetPercentileLiteral = Literal["P50", "P75", "P90", "P95"]
PoolActionLiteral = Literal["donor_right_size", "reallocate_in", "buy_new", "balanced"]


class SpilloverWindow(StrictModel):
    """Hourly window where model demand exceeded contracted PT capacity (or incurred PayGo)."""

    model_id: str
    region: str
    window_start_utc: str
    window_end_utc: str
    duration_minutes: int = Field(ge=1)
    contracted_gsus: int = Field(ge=0)
    peak_tpm: int = Field(ge=0)
    peak_gsus: int = Field(ge=0)
    excess_gsus: int = Field(ge=0)
    excess_tokens: int = Field(ge=0)


class GsuReallocationMove(StrictModel):
    """Recommended GSU transfer from an over-provisioned donor pool to a deficit pool."""

    from_model: str
    from_region: str
    to_model: str
    to_region: str
    gsus_moved: int = Field(ge=1)
    same_provider: bool = True
    rationale: str


class ModelPoolGap(StrictModel):
    """Per-model GSU balance before and after intra-provider reallocation."""

    model_id: str
    region: str
    provider: Literal["1p_gemini", "3p_claude", "other"]
    contracted_gsus: int = Field(ge=0)
    active_p50_gsus: int = Field(ge=0)
    active_p75_gsus: int = Field(ge=0)
    active_p90_gsus: int = Field(ge=0)
    active_p95_gsus: int = Field(ge=0)
    target_percentile: TargetPercentileLiteral
    target_gsus: int = Field(ge=0)
    initial_gap_gsus: int
    reallocated_delta_gsus: int
    post_reallocation_gsus: int = Field(ge=0)
    new_gsus_needed: int = Field(ge=0)
    action: PoolActionLiteral


class GsuReallocationPlan(StrictModel):
    """Portfolio-wide GSU reallocation + net-new GSU recommendation at a target percentile."""

    target_percentile: TargetPercentileLiteral
    total_contracted_gsus: int = Field(ge=0)
    total_surplus_gsus: int = Field(ge=0)
    total_deficit_gsus: int = Field(ge=0)
    total_reallocated_gsus: int = Field(ge=0)
    total_new_gsus_needed: int = Field(ge=0)
    moves: list[GsuReallocationMove] = Field(default_factory=list)
    pool_gaps: list[ModelPoolGap] = Field(default_factory=list)
