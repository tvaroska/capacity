"""The shareable HTML scorecard: telemetry text is data, never markup, and engine values match."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from capacity.capacity import build_response
from capacity.models import (
    DisaggregatedPoolRequest,
    GatewayConfigRequest,
    ModelTierPrefixCacheRequest,
)
from capacity.portal import render_recommendations_html, render_showcase_portal_html
from capacity.scenario import (
    ModelInventoryAnnotation,
    ScenarioBurstAndCache,
    ScenarioGateway,
    ScenarioGsuSizing,
    ScenarioSelfHosted,
    ShowcaseScenario,
    evaluate_showcase_scenario,
)


def _recs_page_for(
    model_name: str,
    percentiles: tuple[int, ...] = (50, 75, 90, 95),
    active_minutes: int = 5,
    total_minutes: int = 1_440,
    forecast_hours: int = 24,
) -> str:
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    series = {start + timedelta(minutes=i): 241_800 for i in range(active_minutes)}
    recs = build_response(
        "demo-project",
        1,
        {model_name: series},
        total_minutes,
        list(percentiles),
        forecast_steps=forecast_hours,
    )
    return render_recommendations_html(recs, static_export=True)


def _scenario_page_with_scope(scope_label: str) -> str:
    scen = ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="clean-proj",
            minute_tpm_series={"gemini-3.8-flash@global": [241_800.0] * 10},
            inventory_annotations={
                "gemini-3.8-flash@global": ModelInventoryAnnotation(
                    contracted_gsus=4, scope_label=scope_label
                )
            },
        )
    )
    return render_showcase_portal_html(static_export=True, scenario=scen)


def test_model_name_cannot_add_a_script_element() -> None:
    clean = _recs_page_for("gemini-3.8-flash")
    page = _recs_page_for("a</script><script>alert(1)</script>")
    assert "<script>alert(1)" not in page
    assert page.count("<script") == clean.count("<script")
    assert page.count("</script") == clean.count("</script")


def test_model_name_is_escaped_in_the_table() -> None:
    payload = "<img src=x onerror=alert(1)>"
    page = _recs_page_for(payload)
    assert payload not in page
    assert f"<code>{html.escape(payload)}</code>" in page


def test_placeholder_text_in_data_is_not_expanded() -> None:
    recs_page = _recs_page_for("__GSU_TABLE_HEADERS__")
    assert "<code>__GSU_TABLE_HEADERS__</code>" in recs_page
    assert recs_page.count("<th>Model</th>") == 1

    clean_scen = _scenario_page_with_scope("prod-scope")
    hostile_scen = _scenario_page_with_scope("__HEADER_ROWS__")
    assert "(__HEADER_ROWS__)" in hostile_scen
    tier_8 = "Pure Off-Peak / Batch PayGo (Projects Without PT)"
    assert hostile_scen.count(tier_8) == clean_scen.count(tier_8) == 1


def test_page_carries_no_unread_data_blob_and_no_leftover_placeholder() -> None:
    for rendered in (
        _recs_page_for("gemini-3.8-flash"),
        render_showcase_portal_html(static_export=True),
    ):
        assert "CAPACITY_SHOWCASE_DATA" not in rendered
        assert re.findall(r"__[A-Z0-9_]+__", rendered) == []


def test_recommend_html_out_renders_sizing_only_with_real_duty_horizon_and_unknown_qmark() -> None:
    page = _recs_page_for(
        "unlisted-custom-model",
        percentiles=(90,),
        active_minutes=720,
        total_minutes=1_440,
        forecast_hours=72,
    )
    assert "tab-burst" not in page
    assert "tab-gateway" not in page
    assert "tab-finops" not in page
    assert "23.8%" not in page
    assert ">50.0%<" in page
    assert "+72h" in page
    assert "Active P90" in page
    assert "Active P50" not in page
    row = page[page.index("<code>unlisted-custom-model</code>") :]
    row = row[: row.index("</tr>")]
    assert "<td class='num'>?</td>" in row
    assert "(? GSU)" in row


def test_scenario_page_reflects_custom_gateway_disagg_cache_and_capacity_overrides() -> None:
    scen = ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="override-check",
            minute_tpm_series={"gemini-3.8-flash@us-central1": [240_000.0] * 12},
            gsu_capacity_overrides={"gemini-3.8-flash": 60_000},
        ),
        self_hosted=ScenarioSelfHosted(
            enabled=True,
            disaggregated=DisaggregatedPoolRequest(
                profile="llama-3.3-70b-fp8",
                peak_qps=0.25,
                avg_input_tokens=1_024,
                avg_output_tokens=128,
            ),
        ),
        burst_and_cache=ScenarioBurstAndCache(
            default_model="gemini-3.8-flash",
            provisioned_gsus=4,
            sixty_sec_tpm=180_000.0,
            peak_1s_tps=3_500.0,
            flash_tier_audit=ModelTierPrefixCacheRequest(
                model="gemini-3.8-flash",
                total_input_tokens=16_384,
                static_prefix_tokens=12_288,
                dynamic_header_offset_tokens=None,
                single_turn_request=False,
            ),
        ),
        gateway=ScenarioGateway(
            presentation_mode="oss_stack",
            config=GatewayConfigRequest(
                model="gemini-3.8-flash",
                provisioned_gsus=4,
                target_rps=120,
                concurrent_sse_streams=40,
                pods=3,
                uvicorn_workers_per_pod=2,
                business_unit_shares=2,
            ),
        ),
    )
    report = evaluate_showcase_scenario(scen)
    assert report.gsu_recommendations.models[0].gsu_capacity_tpm == 60_000
    assert report.micro_burst.tps_per_gsu == 1_000.0
    assert report.gateway.committed_burndown_tpm == 240_000
    assert report.commitment.tpm_per_gsu == 60_000

    rendered = render_showcase_portal_html(static_export=True, report=report)
    assert "6 Engines" in rendered
    assert "Healthy Scale Envelope" in rendered
    assert "value='120'" in rendered
    assert "Monolithic Cheaper (1 vs 2 Reps)" in rendered
    assert "badge-warn' id='kpi-disagg-badge'" in rendered
    assert "Prefix Cache Healthy" in rendered
    assert "<td class='num'>60,000</td>" in rendered
