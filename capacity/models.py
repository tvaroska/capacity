from __future__ import annotations

from pydantic import BaseModel


class PercentileDetail(BaseModel):
    tpm: int
    recommended_gsus: int | None


class ModelRecommendation(BaseModel):
    model: str
    percentiles: dict[str, PercentileDetail]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "model": "gemini-3.5-flash",
                    "percentiles": {
                        "p50": {"tpm": 12000, "recommended_gsus": 2},
                        "p90": {"tpm": 45000, "recommended_gsus": 5},
                    },
                }
            ]
        }
    }


class RecommendationResponse(BaseModel):
    project: str
    period_days: int
    percentiles: list[int]
    models: list[ModelRecommendation]
