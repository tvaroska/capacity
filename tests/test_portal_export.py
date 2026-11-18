"""The shareable HTML scorecard: telemetry text is data, never markup, and engine values match."""

from __future__ import annotations

import ast
import html
import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capacity.api import app as api_app
from capacity.contract.requests import (
    ChargebackRequest,
    DeadlineJobSpec,
    DisaggregatedPoolRequest,
    FolderPoolingRequest,
    GatewayConfigRequest,
    ModelTierPrefixCacheRequest,
    ResidualScheduleRequest,
)
from capacity.contract.scenario import (
    FinOpsPoolingMode,
    GatewayPresentationMode,
    ModelInventoryAnnotation,
    ScenarioBurstAndCache,
    ScenarioFinOps,
    ScenarioGateway,
    ScenarioGsuSizing,
    ScenarioPresentation,
    ScenarioSelfHosted,
    ShowcaseScenario,
)
from capacity.engine.sizing import build_response
from capacity.showcase.evaluate import evaluate_showcase_scenario
from capacity.showcase.renderer import (
    render_page_view,
    render_recommendations_html,
    render_showcase_portal_html,
)
from capacity.showcase.view import (
    BadgeView,
    BannerView,
    CardRowView,
    CardView,
    CellView,
    ControlFieldView,
    ControlOptionView,
    KpiCardView,
    LinkView,
    PageView,
    TableHeaderView,
    TableView,
    TabView,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHOWCASE_DIR = REPO_ROOT / "capacity" / "showcase"
ASSETS_DIR = SHOWCASE_DIR / "assets"
VOID_HTML_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
client = TestClient(api_app)


class _StrictHtmlValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.script_count = 0
        self.forbidden_attrs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.script_count += 1
        for name, _ in attrs:
            if name not in {"onclick", "oninput", "onchange"} and name.startswith("on"):
                self.forbidden_attrs.append((tag, name))
        if tag not in VOID_HTML_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _ in attrs:
            if name not in {"onclick", "oninput", "onchange"} and name.startswith("on"):
                self.forbidden_attrs.append((tag, name))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_HTML_ELEMENTS:
            return
        assert self.stack and self.stack[-1] == tag, f"Unbalanced closing </{tag}> in {self.stack}"
        self.stack.pop()


def _assert_balanced_html(doc: str, *, expected_scripts: int) -> None:
    parser = _StrictHtmlValidator()
    parser.feed(doc)
    parser.close()
    assert parser.stack == []
    assert parser.forbidden_attrs == []
    assert parser.script_count == expected_scripts


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
    return render_showcase_portal_html(evaluate_showcase_scenario(scen), static_export=True)


def test_model_name_cannot_add_a_script_element() -> None:
    clean = _recs_page_for("gemini-3.8-flash")
    page = _recs_page_for("a</script><script>alert(1)</script>")
    assert "<script>alert(1)" not in page
    assert page.count("<script") == clean.count("<script") == 0
    assert page.count("</script") == clean.count("</script") == 0


def test_model_name_is_escaped_in_the_table() -> None:
    payload = "<img src=x onerror=alert(1)>"
    page = _recs_page_for(payload)
    assert payload not in page
    assert f"<code>{html.escape(payload)}</code>" in page


def test_placeholder_text_in_data_is_not_expanded() -> None:
    recs_page = _recs_page_for("__MAIN_CONTENT__")
    assert "<code>__MAIN_CONTENT__</code>" in recs_page
    assert recs_page.count("<th>Model</th>") == 1

    clean_scen = _scenario_page_with_scope("prod-scope")
    hostile_scen = _scenario_page_with_scope("__HEADER_RIGHT__")
    assert "(__HEADER_RIGHT__)" in hostile_scen
    tier_8 = "Pure Off-Peak / Batch PayGo (Projects Without PT)"
    assert hostile_scen.count(tier_8) == clean_scen.count(tier_8) == 1


def test_page_carries_no_unread_data_blob_and_no_leftover_placeholder() -> None:
    for rendered in (
        _recs_page_for("gemini-3.8-flash"),
        render_showcase_portal_html(evaluate_showcase_scenario(), static_export=True),
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
    assert '<td class="num">?</td>' in row
    assert "(? GSU)" in row


def test_handmade_page_view_renders_every_slot_and_escapes_default_title() -> None:
    view = PageView(
        page_title='Hand & <script>Title</script> "Quote"',
        brand_heading="Heading <img onerror=1>",
        brand_subtitle="Sub & Arrow \u2192 End",
        badges=(BadgeView("Primary & <Badge>", "blue", element_id='b"1'),),
        docs_link=LinkView('/docs?a=1&b="2"', "Docs & Spec"),
        tabs=(
            TabView(
                panel_id="tab-custom",
                button_label="Tab 1 & <More>",
                kpis=(
                    KpiCardView(
                        label="KPI & <Label>",
                        value="42 & <Units>",
                        badge=BadgeView("Warn & <Tag>", "warn"),
                        caption="Cap & <Note>",
                        value_id="kpi-v",
                    ),
                ),
                rows=(
                    CardRowView(
                        (
                            CardView(
                                title="Card & <Title>",
                                subtitle="Sub & <Text>",
                                controls=(
                                    ControlFieldView(
                                        field_id="c-num",
                                        label="Num & <Input>",
                                        value='7" onload="alert(1)',
                                        min_val="1",
                                        handler="recalcDisagg()",
                                    ),
                                    ControlFieldView(
                                        field_id="c-sel",
                                        label="Sel & <Pick>",
                                        kind="select",
                                        options=(
                                            ControlOptionView('v"1', "Opt & <1>", selected=True),
                                        ),
                                        disabled=True,
                                    ),
                                ),
                                banner=BannerView("Banner & <Alert>", "danger", element_id="bn-1"),
                                table=TableView(
                                    headers=(
                                        TableHeaderView("Col & <A>"),
                                        TableHeaderView("Num & <B>", numeric=True),
                                    ),
                                    rows=(
                                        (
                                            CellView(code="m<script>1</script>"),
                                            CellView(
                                                text="10 / ",
                                                bold_suffix="2<x>",
                                                note='scope"onload="1',
                                                numeric=True,
                                            ),
                                        ),
                                    ),
                                ),
                                code_block="apiVersion: v1 & <raw>",
                            ),
                        )
                    ),
                ),
                active=True,
            ),
        ),
        include_script=False,
        live_api=True,
        api_context=(("data-custom", 'x" onload="alert(1)'),),
    )
    rendered = render_page_view(view)
    _assert_balanced_html(rendered, expected_scripts=0)
    assert (
        "<title>Hand &amp; &lt;script&gt;Title&lt;/script&gt; &quot;Quote&quot;</title>" in rendered
    )
    assert "Sub &amp; Arrow &rarr; End" in rendered
    assert 'href="/docs?a=1&amp;b=&quot;2&quot;"' in rendered
    assert "<code>m&lt;script&gt;1&lt;/script&gt;</code>" in rendered
    assert "<pre><code>apiVersion: v1 &amp; &lt;raw&gt;</code></pre>" in rendered
    assert "<script>Title" not in rendered

    default_html = render_showcase_portal_html(evaluate_showcase_scenario(), static_export=True)
    title_match = re.search(r"<title>(.*?)</title>", default_html)
    assert title_match is not None
    assert "Multi-Gateway &amp; FOCUS 1.2 Showcase" in title_match.group(1)
    assert re.findall(r"&(?!(?:amp|lt|gt|quot|rarr|#x27);)", title_match.group(1)) == []


def test_templates_and_renderer_hold_zero_numbers_and_single_escape_point() -> None:
    html_shell = (ASSETS_DIR / "workbench.html").read_text(encoding="utf-8")
    without_html_syntax = html_shell.replace("utf-8", "").replace("<h1>", "").replace("</h1>", "")
    assert re.findall(r"\d+", without_html_syntax) == []

    renderer_tree = ast.parse((SHOWCASE_DIR / "renderer.py").read_text(encoding="utf-8"))
    numeric_literals = [
        node.value
        for node in ast.walk(renderer_tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert numeric_literals == []

    escape_call_sites: list[str] = []
    for py_path in sorted(SHOWCASE_DIR.glob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "html"
                        and child.func.attr == "escape"
                    ):
                        escape_call_sites.append(f"{py_path.name}:{node.name}")
    assert escape_call_sites == ["renderer.py:_esc"]


def test_browser_js_holds_zero_formulas_and_burst_route_matches_served_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    js_src = (ASSETS_DIR / "workbench.js").read_text(encoding="utf-8")
    forbidden_domain_numbers = (
        "2015",
        "120900",
        "241800",
        "30000",
        "39000",
        "21000",
        "10417",
        "3200",
        "18500",
        "200000",
        "4096",
        "6144",
        "2048",
        "1024",
        "512",
        "524288",
        "0.05",
        "0.90",
        "0.125",
        "0.0077",
        "4.5",
        "1.30",
        "7.5",
    )
    for token in forbidden_domain_numbers:
        assert re.search(rf"(?<![\w.]){re.escape(token)}(?![\w.])", js_src) is None, token
    for route in (
        "/api/v1/self-hosted/disaggregated",
        "/api/v1/burst/window",
        "/api/v1/prefix-cache/model-tier",
        "/api/v1/gateway/config",
    ):
        assert route in js_src

    report = evaluate_showcase_scenario()
    static_doc = render_showcase_portal_html(report, static_export=True)
    live_doc = render_showcase_portal_html(report, static_export=False)
    assert 'data-live-api="false"' in static_doc
    assert 'data-live-api="true"' in live_doc
    assert static_doc.count(" disabled") == 16
    assert "oninput=" not in static_doc and "onchange=" not in static_doc
    assert " disabled" not in live_doc

    scen = ShowcaseScenario(
        gsu_sizing=ScenarioGsuSizing(
            project_label="override-live",
            minute_tpm_series={"gemini-3.8-flash@us-central1": [240_000.0] * 12},
            gsu_capacity_overrides={"gemini-3.8-flash": 60_000},
        ),
        burst_and_cache=ScenarioBurstAndCache(
            default_model="gemini-3.8-flash",
            provisioned_gsus=4,
            sixty_sec_tpm=180_000.0,
            peak_1s_tps=3_500.0,
        ),
    )
    scen_file = tmp_path / "live_scenario.json"
    scen_file.write_text(json.dumps(scen.model_dump()), encoding="utf-8")
    monkeypatch.setenv("CAPACITY_SCENARIO_FILE", str(scen_file))

    served_page = client.get("/")
    assert served_page.status_code == 200
    assert 'data-live-api="true"' in served_page.text
    assert "75.0% &rarr; 83.3%" in served_page.text

    burst_resp = client.post(
        "/api/v1/burst/window",
        json={
            "model": "gemini-3.8-flash",
            "provisioned_gsus": 4,
            "sixty_sec_tpm": 180_000.0,
            "peak_1s_tps": 3_500.0,
        },
    )
    assert burst_resp.status_code == 200
    b_json = burst_resp.json()
    assert b_json["tps_per_gsu"] == 1_000.0
    assert b_json["apparent_60s_utilization_pct"] == 75.0
    assert b_json["peak_window_utilization_pct"] == pytest.approx(83.33, abs=0.05)


def test_asset_files_match_design_tokens_and_font_stacks() -> None:
    for name in ("workbench.html", "workbench.css", "workbench.js", "favicon.svg"):
        assert (ASSETS_DIR / name).is_file(), name

    design_md = (REPO_ROOT / ".stitch" / "DESIGN.md").read_text(encoding="utf-8")
    css_src = (ASSETS_DIR / "workbench.css").read_text(encoding="utf-8")
    design_tokens = dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6});", design_md))
    css_tokens = dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6});", css_src))
    assert len(design_tokens) == 16
    assert css_tokens == design_tokens
    assert (
        "font-family: 'Google Sans', 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;"
        in css_src
    )
    assert "font-family: 'Roboto Mono', 'JetBrains Mono', ui-monospace, monospace;" in css_src


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

    rendered = render_showcase_portal_html(report, static_export=True)
    assert "6 Engines" in rendered
    assert "Healthy Scale Envelope" in rendered
    assert 'value="120"' in rendered
    assert "Monolithic Cheaper (1 vs 2 Reps)" in rendered
    assert 'class="badge badge-warn" id="kpi-disagg-badge"' in rendered
    assert "Prefix Cache Healthy" in rendered
    assert '<td class="num">60,000</td>' in rendered


def test_hostile_text_and_engine_values_across_all_section_combinations() -> None:
    hostile = '<script>alert(1)</script>" onload="alert(2)" \'&__MAIN_CONTENT__'
    gw_modes: tuple[GatewayPresentationMode, ...] = ("oss_stack", "central_proxy")
    pool_modes: tuple[FinOpsPoolingMode, ...] = ("folder_rhpt", "1p_vs_3p_hub")

    for sh_enabled, has_inv, gw_mode, pool_mode in itertools.product(
        (True, False), (False, True), gw_modes, pool_modes
    ):
        series_key = f"m-{hostile}@r-{hostile}"
        annotations = (
            {
                series_key: ModelInventoryAnnotation(
                    contracted_gsus=7,
                    scope_label=f"sc-{hostile}",
                    spillover_requests=19,
                    http_429_count=3,
                    lifecycle_track="long_term_ga",
                    renewal_mode="fixed_term",
                    expiry_date="2027-06-30",
                    expiring_fixed_gsus=5,
                )
            }
            if has_inv
            else {}
        )
        scen = ShowcaseScenario(
            presentation=ScenarioPresentation(
                title=f"T-{hostile}",
                subtitle=f"S-{hostile}",
                primary_badge=f"B1-{hostile}",
                secondary_badge=f"B2-{hostile}",
                wave_badge_label=f"W-{hostile}",
            ),
            gsu_sizing=ScenarioGsuSizing(
                project_label=f"P-{hostile}",
                minute_tpm_series={
                    "gemini-3.8-flash@us-central1": [362_700.0] * 8,
                    series_key: [120_900.0] * 8,
                },
                inventory_annotations=annotations,
            ),
            self_hosted=ScenarioSelfHosted(enabled=sh_enabled),
            gateway=ScenarioGateway(presentation_mode=gw_mode, proxy_label=f"GW-{hostile}"),
            finops=ScenarioFinOps(
                pooling_mode=pool_mode,
                chargeback=ChargebackRequest(
                    provisioned_gsus=10,
                    hourly_consumed_tpm_by_tenant={f"bu-{hostile}": [241_800.0, 120_900.0]},
                ),
                residual_schedule=ResidualScheduleRequest(
                    provisioned_gsus=12,
                    deadline_jobs=[
                        DeadlineJobSpec(
                            job_id=f"job-{hostile}",
                            value_usd=4_321.0,
                            burndown_tokens=50_000_000.0,
                            deadline_hour=4,
                        )
                    ],
                ),
                folder_pooling=FolderPoolingRequest(
                    hourly_tpm_by_project={
                        f"proj-{hostile}": [362_700.0, 60_000.0],
                        "amer-spoke": [60_000.0, 362_700.0],
                    }
                ),
            ),
        )
        report = evaluate_showcase_scenario(scen)
        doc = render_showcase_portal_html(report, static_export=True)

        assert "<script>alert(1)</script>" not in doc
        assert 'onload="alert(2)"' not in doc
        assert re.findall(r"__[A-Z0-9_]+__", doc) == ["__MAIN_CONTENT__"] * doc.count(
            "__MAIN_CONTENT__"
        )
        _assert_balanced_html(doc, expected_scripts=1)

        assert f"${report.finops_kpis.cpvo_usd:.4f}" in doc
        assert f"${report.scavenger.double_payment_waste_avoided_usd:,.0f}/mo" in doc
        assert "$4,321" in doc
        assert f"${report.chargeback.tenants[0].active_allocated_cost_usd:,.2f}" in doc
        if sh_enabled and report.self_hosted_audit and report.disaggregated_sizing:
            assert f"{report.self_hosted_audit.locked_regional_accelerators} GPUs" in doc
            assert (
                f"{report.disaggregated_sizing.prefill_replicas}P + "
                f"{report.disaggregated_sizing.decode_replicas}D"
            ) in doc
        elif has_inv:
            assert "7 GSUs" in doc
            assert "3 HTTP 429s" in doc
        if gw_mode == "oss_stack":
            assert f"{report.gateway.litellm_audit.total_prisma_rust_engines} Engines" in doc
        else:
            assert html.escape(f"GW-{hostile}", quote=True) in doc
        if pool_mode == "folder_rhpt":
            assert (
                f"{report.folder_pooling.siloed_total_gsus} &rarr; "
                f"{report.folder_pooling.pooled_folder_gsus} GSUs"
            ) in doc
        else:
            assert "GA Priority + RHPT" in doc
