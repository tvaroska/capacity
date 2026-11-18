"""Critical spillover window detection and intra-provider GSU reallocation planning."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from capacity.contract.common import SeriesKey
from capacity.contract.reallocation import (
    GsuReallocationMove,
    GsuReallocationPlan,
    ModelPoolGap,
    PoolActionLiteral,
    SpilloverWindow,
    TargetPercentileLiteral,
)
from capacity.contract.results import ModelRecommendation
from capacity.engine.catalog import ModelCatalog, build_model_catalog, is_partner_claude

ProviderLiteral = Literal["1p_gemini", "3p_claude", "other"]


@dataclass
class _PoolAcc:
    model_id: str
    region: str
    provider: ProviderLiteral
    contracted: int
    p50: int
    p75: int
    p90: int
    p95: int
    target: int
    surplus: int
    deficit: int
    delta: int = 0


def _classify_provider(model_id: str, catalog: ModelCatalog) -> ProviderLiteral:
    if is_partner_claude(model_id, catalog=catalog):
        return "3p_claude"
    low = model_id.lower()
    if low.startswith("gemini"):
        return "1p_gemini"
    return "other"


def _pct_gsus(rec: ModelRecommendation, pct_key: str) -> int:
    bucket = rec.active_hours if rec.active_hours else rec.all_hours
    detail = bucket.get(pct_key) or rec.percentiles.get(pct_key)
    return detail.recommended_gsus if detail is not None else 0


def detect_critical_spillover_windows(
    sparse_series: Mapping[SeriesKey, Mapping[datetime, int]],
    contracted_by_key: Mapping[SeriesKey, int],
    catalog: ModelCatalog,
    *,
    top_n: int = 10,
    window_minutes: int = 60,
) -> list[SpilloverWindow]:
    """Find the top-N hourly UTC windows where demand exceeded contracted PT ceiling."""
    windows: list[SpilloverWindow] = []
    step_td = timedelta(minutes=window_minutes)

    for skey, minute_map in sparse_series.items():
        cap = catalog.capacity_for(skey.model)
        if not cap or cap <= 0:
            continue
        contracted = max(0, int(contracted_by_key.get(skey, 0)))
        ceiling_tpm = contracted * cap
        threshold_tpm = ceiling_tpm if contracted > 0 else cap

        buckets: dict[datetime, list[int]] = defaultdict(list)
        for ts, tpm in minute_map.items():
            if tpm <= threshold_tpm:
                continue
            ts_utc = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
            floored_min = (ts_utc.minute // window_minutes) * window_minutes
            bucket_start = ts_utc.replace(minute=floored_min, second=0, microsecond=0)
            buckets[bucket_start].append(int(tpm))

        for bucket_start, vals in buckets.items():
            peak_tpm = max(vals)
            peak_gsus = math.ceil(peak_tpm / cap)
            excess_gsus = max(0, peak_gsus - contracted)
            excess_tokens = sum(max(0, v - ceiling_tpm) for v in vals)
            if excess_gsus <= 0 or excess_tokens <= 0:
                continue
            bucket_end = bucket_start + step_td
            windows.append(
                SpilloverWindow(
                    model_id=skey.model,
                    region=skey.region or "global",
                    window_start_utc=bucket_start.strftime("%Y-%m-%d %H:%MZ"),
                    window_end_utc=bucket_end.strftime("%H:%MZ"),
                    duration_minutes=len(vals),
                    contracted_gsus=contracted,
                    peak_tpm=peak_tpm,
                    peak_gsus=peak_gsus,
                    excess_gsus=excess_gsus,
                    excess_tokens=excess_tokens,
                )
            )

    windows.sort(
        key=lambda w: (w.excess_tokens, w.excess_gsus, w.duration_minutes, w.model_id),
        reverse=True,
    )
    return windows[:top_n]


def build_gsu_reallocation_plan(
    recommendations: Sequence[ModelRecommendation],
    contracted_by_key: Mapping[SeriesKey, int],
    *,
    target_percentile: TargetPercentileLiteral = "P75",
    catalog: ModelCatalog | None = None,
) -> GsuReallocationPlan:
    """Compute intra-provider GSU reallocation moves and net-new GSUs needed for a target."""
    cat = catalog if catalog is not None else build_model_catalog()
    rec_by_key: dict[tuple[str, str], ModelRecommendation] = {
        (rec.model, rec.region or "global"): rec for rec in recommendations
    }
    all_keys: set[tuple[str, str]] = set(rec_by_key.keys())
    for skey in contracted_by_key:
        all_keys.add((skey.model, skey.region or "global"))

    contracted_lookup: dict[tuple[str, str], int] = {
        (skey.model, skey.region or "global"): max(0, int(val))
        for skey, val in contracted_by_key.items()
    }

    rows_meta: list[_PoolAcc] = []
    for model_id, region in sorted(all_keys):
        rec = rec_by_key.get((model_id, region))
        contracted = contracted_lookup.get((model_id, region), 0)
        p50 = _pct_gsus(rec, "P50") if rec is not None else 0
        p75 = _pct_gsus(rec, "P75") if rec is not None else 0
        p90 = _pct_gsus(rec, "P90") if rec is not None else 0
        p95 = _pct_gsus(rec, "P95") if rec is not None else 0
        target_map = {"P50": p50, "P75": p75, "P90": p90, "P95": p95}
        target_gsus = target_map[target_percentile]
        if contracted == 0 and target_gsus == 0 and p95 == 0:
            continue
        provider = _classify_provider(model_id, cat)
        # 3P pools with zero minutely telemetry in 1P views are not treated as idle donors.
        can_donate = not (provider == "3p_claude" and p95 == 0)
        surplus = max(0, contracted - target_gsus) if can_donate else 0
        deficit = max(0, target_gsus - contracted)
        rows_meta.append(
            _PoolAcc(
                model_id=model_id,
                region=region,
                provider=provider,
                contracted=contracted,
                p50=p50,
                p75=p75,
                p90=p90,
                p95=p95,
                target=target_gsus,
                surplus=surplus,
                deficit=deficit,
            )
        )

    donors = sorted(
        (r for r in rows_meta if r.surplus > 0),
        key=lambda r: r.surplus,
        reverse=True,
    )
    deficits = sorted(
        (r for r in rows_meta if r.deficit > 0),
        key=lambda r: r.deficit,
        reverse=True,
    )

    moves: list[GsuReallocationMove] = []
    for d_row in deficits:
        need = d_row.deficit
        if need <= 0:
            continue
        for s_row in donors:
            if need <= 0:
                break
            if s_row.provider != d_row.provider or s_row.surplus <= 0:
                continue
            moved = min(s_row.surplus, need)
            s_row.surplus -= moved
            s_row.delta -= moved
            d_row.delta += moved
            need -= moved
            moves.append(
                GsuReallocationMove(
                    from_model=s_row.model_id,
                    from_region=s_row.region,
                    to_model=d_row.model_id,
                    to_region=d_row.region,
                    gsus_moved=moved,
                    same_provider=True,
                    rationale=(
                        f"Shift surplus above {target_percentile} "
                        f"to cover {target_percentile} deficit"
                    ),
                )
            )

    pool_gaps: list[ModelPoolGap] = []
    total_contracted = 0
    total_surplus = 0
    total_deficit = 0
    total_reallocated = sum(m.gsus_moved for m in moves)
    total_new_needed = 0

    for r in rows_meta:
        post = max(0, r.contracted + r.delta)
        new_needed = max(0, r.target - post)
        initial_gap = r.target - r.contracted
        total_contracted += r.contracted
        if initial_gap < 0 and (r.provider != "3p_claude" or r.p95 > 0):
            total_surplus += -initial_gap
        elif initial_gap > 0:
            total_deficit += initial_gap
        total_new_needed += new_needed

        action: PoolActionLiteral
        if r.delta < 0:
            action = "donor_right_size"
        elif new_needed > 0:
            action = "buy_new"
        elif r.delta > 0:
            action = "reallocate_in"
        else:
            action = "balanced"

        pool_gaps.append(
            ModelPoolGap(
                model_id=r.model_id,
                region=r.region,
                provider=r.provider,
                contracted_gsus=r.contracted,
                active_p50_gsus=r.p50,
                active_p75_gsus=r.p75,
                active_p90_gsus=r.p90,
                active_p95_gsus=r.p95,
                target_percentile=target_percentile,
                target_gsus=r.target,
                initial_gap_gsus=initial_gap,
                reallocated_delta_gsus=r.delta,
                post_reallocation_gsus=post,
                new_gsus_needed=new_needed,
                action=action,
            )
        )

    pool_gaps.sort(
        key=lambda g: (g.new_gsus_needed, g.target_gsus, g.contracted_gsus),
        reverse=True,
    )
    return GsuReallocationPlan(
        target_percentile=target_percentile,
        total_contracted_gsus=total_contracted,
        total_surplus_gsus=total_surplus,
        total_deficit_gsus=total_deficit,
        total_reallocated_gsus=total_reallocated,
        total_new_gsus_needed=total_new_needed,
        moves=moves,
        pool_gaps=pool_gaps,
    )
