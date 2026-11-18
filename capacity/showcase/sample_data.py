"""Canonical sample datasets and default `ShowcaseScenario` builder (`capacity demo` / `--demo`)."""

from __future__ import annotations

from capacity.contract.requests import (
    ChargebackRequest,
    DeadlineJobSpec,
    FolderPoolingRequest,
    OptimizeRequest,
    ResidualScheduleRequest,
    SelfHostedAuditRequest,
)
from capacity.contract.scenario import ScenarioGsuSizing, ShowcaseScenario
from capacity.engine.stats import DEFAULT_TRACE_EPOCH_UTC
from capacity.telemetry.local import build_demo_throughput

DEMO_OPTIMIZE_SERIES: tuple[float, ...] = (
    420_000.0,
    390_000.0,
    360_000.0,
    310_000.0,
    280_000.0,
    250_000.0,
    240_000.0,
    220_000.0,
    190_000.0,
    160_000.0,
    90_000.0,
    40_000.0,
)
DEMO_SELF_HOSTED_SERIES: tuple[float, ...] = (
    520_000.0,
    480_000.0,
    410_000.0,
    340_000.0,
    290_000.0,
    180_000.0,
)

DEFAULT_TENANT_RAW_TOKEN_WEIGHTS: dict[str, float] = {
    "claims-triage": 1.85,
    "underwriting-copilot": 1.10,
    "policy-audit": 0.45,
}


def make_default_chargeback_tenants() -> dict[str, list[float] | float]:
    return {
        "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
        "underwriting-copilot": [302_250.0] * 8 + [60_450.0] * 16,
        "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
    }


def make_default_folder_pooling_projects() -> dict[str, list[float]]:
    return {
        "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
        "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
        "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
    }


def make_default_deadline_jobs() -> list[DeadlineJobSpec]:
    """Return the canonical 3-job overnight residual scheduling portfolio."""
    return [
        DeadlineJobSpec(
            job_id="premarket-portfolio-briefings",
            value_usd=18_000.0,
            burndown_tokens=240_000_000.0,
            deadline_hour=6,
        ),
        DeadlineJobSpec(
            job_id="regulatory-risk-digest",
            value_usd=12_000.0,
            burndown_tokens=180_000_000.0,
            deadline_hour=5,
        ),
        DeadlineJobSpec(
            job_id="low-density-archive-sweep",
            value_usd=900.0,
            burndown_tokens=320_000_000.0,
            deadline_hour=6,
        ),
    ]


def resolve_scenario_with_defaults(scenario: ShowcaseScenario) -> ShowcaseScenario:
    """Fill optional omitted scenario sub-blocks from canonical sample datasets."""
    sh = scenario.self_hosted
    if sh.audit is None:
        sh = sh.model_copy(
            update={"audit": SelfHostedAuditRequest(tpm_series=list(DEMO_SELF_HOSTED_SERIES))}
        )

    bc = scenario.burst_and_cache
    if bc.prefix_dilution is None:
        bc = bc.model_copy(
            update={"prefix_dilution": OptimizeRequest(tpm_series=list(DEMO_OPTIMIZE_SERIES))}
        )

    fo = scenario.finops
    fo_updates: dict[str, object] = {}
    if fo.commitment is None:
        fo_updates["commitment"] = OptimizeRequest(
            tpm_series=list(DEMO_OPTIMIZE_SERIES), fsp_discount_pct=15.0
        )
    if fo.chargeback is None:
        fo_updates["chargeback"] = ChargebackRequest(
            hourly_consumed_tpm_by_tenant=make_default_chargeback_tenants(),
            tenant_raw_token_weights=dict(DEFAULT_TENANT_RAW_TOKEN_WEIGHTS),
            async_batch_waterfill_tpm=362_700.0,
        )
    if fo.residual_schedule is None:
        fo_updates["residual_schedule"] = ResidualScheduleRequest(
            deadline_jobs=make_default_deadline_jobs()
        )
    if fo.folder_pooling is None:
        fo_updates["folder_pooling"] = FolderPoolingRequest(
            hourly_tpm_by_project=make_default_folder_pooling_projects()
        )
    if fo_updates:
        fo = fo.model_copy(update=fo_updates)

    return scenario.model_copy(update={"self_hosted": sh, "burst_and_cache": bc, "finops": fo})


def build_default_showcase_scenario() -> ShowcaseScenario:
    """Build the canonical default `ShowcaseScenario` used by `capacity demo`."""
    sparse, total_minutes = build_demo_throughput(days=7, by_region=True, mode="gsu")
    minute_series: dict[str, list[float]] = {}
    for skey in sorted(sparse):
        arr = [0.0] * total_minutes
        for ts, val in sparse[skey].items():
            idx = int((ts - DEFAULT_TRACE_EPOCH_UTC).total_seconds() // 60)
            if 0 <= idx < total_minutes:
                arr[idx] = float(val)
        minute_series[skey.format(by_region=True)] = arr
    raw = ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="demo-enterprise-ai",
            days=7,
            by_region=True,
            active_only=True,
            forecast_hours=24,
            minute_tpm_series=minute_series,
        )
    )
    return resolve_scenario_with_defaults(raw)
