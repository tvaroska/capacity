from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from .capacity import build_response
from .core import DEFAULT_DAYS, DEFAULT_PERCENTILES, fetch_throughput
from .models import RecommendationResponse

app = FastAPI(title="Vertex AI Capacity Planner")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/v1/recommendations", response_model=RecommendationResponse)
async def get_recommendations(
    project: str,
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=365),
    percentile: list[int] = Query(default=list(DEFAULT_PERCENTILES), ge=0, le=100),
    model: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
):
    if not percentile:
        raise HTTPException(status_code=422, detail="At least one percentile is required")
    try:
        sparse, total_minutes = await fetch_throughput(
            project,
            days=days,
            models=model or None,
            regions=region or None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return build_response(project, days, sparse, total_minutes, percentile)
