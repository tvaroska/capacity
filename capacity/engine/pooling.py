"""Shared Folder RHPT and central hub project pooling analysis (`ADR-003`)."""

from __future__ import annotations

import math

from capacity.contract.requests import FolderPoolingRequest
from capacity.contract.results import FolderPoolingReport
from capacity.engine.catalog import ModelCatalog, build_model_catalog, normalize_model_id
from capacity.engine.commitment import pad_series_matrix
from capacity.engine.stats import percentile


def evaluate_folder_pooling(
    req: FolderPoolingRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> FolderPoolingReport:
    """Quantify GSU savings from pooling siloed spoke projects under a flat Folder RHPT pool."""
    cat = catalog if catalog is not None else build_model_catalog()
    record = cat.require(req.model)
    norm_model = normalize_model_id(req.model)
    gsu_cap = record.gsu_capacity_tpm
    pct = req.percentile_target

    raw_projects: dict[str, list[float] | float] = {
        proj: list(series) for proj, series in req.hourly_tpm_by_project.items()
    }
    normalized, max_len = pad_series_matrix(raw_projects)

    siloed_by_proj: dict[str, int] = {}
    for proj in sorted(normalized):
        p_tpm = percentile(sorted(normalized[proj]), pct)
        siloed_by_proj[proj] = math.ceil(p_tpm / gsu_cap) if p_tpm > 0 else 0

    siloed_total = sum(siloed_by_proj.values())
    combined_series = sorted(
        sum(normalized[proj][i] for proj in normalized) for i in range(max_len)
    )
    pooled_p_tpm = percentile(combined_series, pct)
    pooled_gsus = math.ceil(pooled_p_tpm / gsu_cap) if pooled_p_tpm > 0 else 0

    stranded_saved = max(0, siloed_total - pooled_gsus)
    diversity = round(siloed_total / pooled_gsus, 2) if pooled_gsus > 0 else 1.0
    monthly_savings = stranded_saved * req.gsu_monthly_price_usd

    spec = record.family_spec
    if spec.supports_folder_rhpt:
        notes = [
            "Place spoke projects as direct children of a single flat Prod-GenAI Folder "
            "(nested subfolders do not inherit Folder RHPT).",
            "Assign an independent GCP Project Spend Cap to each spoke project so a runaway loop "
            "in one business unit only triggers HTTP 403 on its own spoke project.",
            "Aggregate child consumed_token_throughput in the Folder Host Project Metrics Scope "
            "against the folder's dedicated_token_limit.",
        ]
    else:
        notes = [
            f"Folder-Level RHPT is 1P Gemini-only and unsupported for {norm_model}; realize these "
            "pooling savings by consolidating traffic into a central BU-segmented hub project "
            "with gateway-enforced per-BU weighted burndown token buckets.",
            "3P Claude Provisioned Throughput uses 30s/60s dual enforcement buckets with no "
            "1-week term and Standard PayGo spillover (Priority PayGo is 1P-only).",
            f"Monitor unweighted {spec.monitoring_metric} grouped by model_id, location, "
            "request_type, and type, applying client-side burndown weights "
            "(1.0x input, 0.1x cache read, 1.25x 5m / 2.0x 1h cache write, 5.0x output).",
        ]

    return FolderPoolingReport(
        model=norm_model,
        percentile_target=pct,
        project_count=len(normalized),
        siloed_gsus_by_project=siloed_by_proj,
        siloed_total_gsus=siloed_total,
        pooled_folder_gsus=pooled_gsus,
        stranded_gsus_saved=stranded_saved,
        coincidence_diversity_factor=diversity,
        monthly_savings_usd=round(monthly_savings, 2),
        annual_savings_usd=round(monthly_savings * 12.0, 2),
        governance_notes=notes,
    )
