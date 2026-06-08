from __future__ import annotations

from pydantic import BaseModel


class PercentileDetail(BaseModel):
    tpm: int
    recommended_gsus: int | None


class ModelRecommendation(BaseModel):
    model: str
    percentiles: dict[str, PercentileDetail]


class RecommendationResponse(BaseModel):
    project: str
    period_days: int
    percentiles: list[int]
    models: list[ModelRecommendation]
