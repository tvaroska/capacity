"""Documentation contract tests verifying verbatim CLI/YAML parity and ADR completeness."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from capacity.cli import app as cli_app
from capacity.gateway import generate_gateway_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def _extract_fenced_block_after_heading(markdown: str, heading_substring: str) -> str:
    idx = markdown.find(heading_substring)
    assert idx != -1, f"Missing heading containing {heading_substring!r} in README.md"
    m = re.search(r"```[a-zA-Z0-9_-]*\n(.*?)\n```", markdown[idx:], flags=re.DOTALL)
    assert m is not None, f"Missing fenced block under heading {heading_substring!r}"
    return m.group(1).strip()


def test_governance_and_adr_tradeoff_honesty_contract() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    decisions = (REPO_ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    roadmap = (REPO_ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    design_md = (REPO_ROOT / ".stitch" / "DESIGN.md").read_text(encoding="utf-8")
    arch_svg = REPO_ROOT / "docs" / "assets" / "architecture_card.svg"

    for adr_id in (
        "ADR-001",
        "ADR-002",
        "ADR-003",
        "ADR-004",
        "ADR-005",
        "ADR-006",
        "ADR-007",
        "ADR-008",
    ):
        assert f"## {adr_id}:" in decisions
    assert decisions.count("**Rejected Options:**") >= 8
    assert decisions.count("**Where This Breaks:**") >= 8

    # Ensure internal review-panel shorthand never leaks into public roadmap docs
    assert "-15 Blocker" not in roadmap
    assert "-20 Blocker" not in roadmap
    assert "Phase 1" in roadmap and "Phase 2" in roadmap
    assert "--mode self-hosted" in readme
    assert "FOCUS 1.2" in readme
    assert "LiteLLM" in readme
    assert "```mermaid" in readme
    assert "GM3 Light" in design_md
    assert arch_svg.is_file()


def test_readme_fenced_outputs_match_live_cli_and_gateway_verbatim() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    expected_demo = _extract_fenced_block_after_heading(
        readme, "### 4A. Executive Showcase Scorecard"
    )
    res_demo = runner.invoke(cli_app, ["demo"])
    assert res_demo.exit_code == 0
    assert res_demo.stdout.strip() == expected_demo

    expected_sched = _extract_fenced_block_after_heading(
        readme, "### 4B. 3-Tier Residual Capacity Scheduler"
    )
    res_sched = runner.invoke(cli_app, ["schedule", "--model", "gemini-3.8-flash", "--gsus", "20"])
    assert res_sched.exit_code == 0
    assert res_sched.stdout.strip() == expected_sched

    expected_envoy = _extract_fenced_block_after_heading(
        readme, "### Generated Envoy AI Gateway Post-SSE"
    )
    bundle = generate_gateway_bundle(model="gemini-3.8-flash", provisioned_gsus=20)
    assert bundle.envoy_ai_gateway_yaml.strip() == expected_envoy


def test_readme_quickstart_commands_execute_cleanly(tmp_path: Path) -> None:
    scorecard_path = tmp_path / "capacity_scorecard.html"
    res_demo = runner.invoke(cli_app, ["demo", "--html-out", str(scorecard_path)])
    assert res_demo.exit_code == 0
    assert scorecard_path.is_file()

    for cmd in (
        [
            "recommend",
            "--trace-file",
            str(REPO_ROOT / "examples/diurnal_8x5_trace.json"),
            "--active-only",
        ],
        [
            "recommend",
            "--mode",
            "self-hosted",
            "--vllm-file",
            str(REPO_ROOT / "examples/otel_genai_spans.jsonl"),
            "--active-only",
        ],
        ["recommend", "--demo", "--active-only", "--by-region"],
        ["recommend", "--demo", "--mode", "self-hosted", "--active-only", "--by-region"],
        [
            *("optimize", "--model", "gemini-3.8-flash"),
            *("--sixty-sec-tpm", "150000", "--peak-1s-tps", "4750"),
        ],
        [
            "optimize",
            "--mode",
            "self-hosted",
            "--model",
            "llama-3.3-70b-fp8",
            "--vllm-file",
            str(REPO_ROOT / "examples/vllm_prometheus_multiscrape.txt"),
        ],
        [
            "gateway",
            "--target",
            "all",
            "--model",
            "gemini-3.8-flash",
            "--gsus",
            "20",
            "--rps",
            "650",
        ],
        ["schedule", "--model", "gemini-3.8-flash", "--gsus", "20"],
        [
            "chargeback",
            "--model",
            "gemini-3.8-flash",
            "--gsus",
            "10",
            "--tenant-file",
            str(REPO_ROOT / "examples/chargeback_tenants.json"),
            "--waterfill-tpm",
            "300000",
        ],
    ):
        res = runner.invoke(cli_app, cmd)
        assert res.exit_code == 0, f"Failed command {cmd}: {res.output}"


def test_readme_benchmark_figures_come_from_the_committed_receipt() -> None:
    """Regenerating the receipt must also refresh the README; nothing else ties the two."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    receipt = json.loads(
        (REPO_ROOT / "benchmarks" / "results" / "l3_receipt.json").read_text(encoding="utf-8")
    )
    track_a = receipt["track_a_empirical_trace_backtest"]
    arms, deltas = track_a["arms"], track_a["summary_deltas"]
    live = receipt["track_b_live_http_sse_gateway"]["arms"]
    naive, round_robin, ours = (
        live["unweighted_60s_default"],
        live["stateless_rr_stripped"],
        live["capacity_two_phase_gateway"],
    )

    def reserved_and_cost(arm: str, bold: bool = False) -> str:
        cost = f"`${arms[arm]['total_7d_cost_usd']:,.2f}`"
        return f"`{arms[arm]['mean_reserved_gsus']:.0f}` GSUs | {f'**{cost}**' if bold else cost}"

    quoted = [
        f"`receipt_sha256: {receipt['receipt_sha256'][:12]}...`",
        f"**`{deltas['cost_savings_vs_static_p99_pct']:.2f}%`** vs. static active-`P99`",
        f"within `{deltas['optimality_gap_vs_l2_oracle_pct']:.2f}%` of the hindsight oracle",
        f"{reserved_and_cost('naive_24x7_avg')} | "
        f"`{arms['naive_24x7_avg']['tier0_unhandled_429_pct']:.2f}%` Tier-0 `429`",
        reserved_and_cost("static_p99_pt_l0"),
        reserved_and_cost("pure_priority_paygo_l1"),
        reserved_and_cost("capacity_compiled", bold=True),
        f"**`{deltas['cost_savings_vs_static_p99_pct']:.2f}%` savings vs. Static `P99`**",
        f"`{naive['http_200_streams']} / {naive['requests_sent']}` `200 OK` | "
        f"`{naive['burndown_accounting_drift_pct']:.2f}%` burndown drift",
        f"`{naive['router_blackhole_drop_rate_pct']:.2f}%` `60s`-cooldown blackhole drops and "
        f"`{naive['peer_pt_starvation_events']}` peer `PT` starvation events",
        f"`{round_robin['burndown_accounting_drift_pct']:.2f}%` burndown drift | "
        f"`{round_robin['effective_prefix_cache_hit_pct']:.2f}%` effective prefix-cache hit rate "
        f"and `{round_robin['peer_pt_starvation_events']}` peer `PT` starvation events",
        f"**`{ours['peer_pt_starvation_events']}` peer `PT` starvation events**, "
        f"**`{ours['router_blackhole_drop_rate_pct']:.2f}%` blackhole drops**, "
        f"**`{ours['effective_prefix_cache_hit_pct']:.2f}%` cache hit**",
    ]
    stale = [figure for figure in quoted if figure not in readme]
    assert not stale, "README.md quotes figures the committed receipt does not hold:\n" + "\n".join(
        stale
    )
