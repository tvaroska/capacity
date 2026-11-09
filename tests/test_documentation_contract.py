"""Documentation contract tests verifying README, DECISIONS, roadmap, and example files stay in sync."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from capacity.cli import app as cli_app

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_governance_and_roadmap_documents_exist_and_cover_adrs() -> None:
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
    ):
        assert f"## {adr_id}:" in decisions

    assert "Phase 1" in roadmap and "Phase 2" in roadmap
    assert "--mode self-hosted" in readme
    assert "FOCUS 1.2" in readme
    assert "LiteLLM" in readme
    assert "```mermaid" in readme
    assert "GM3 Light" in design_md
    assert arch_svg.is_file()


def test_readme_quickstart_commands_execute_cleanly(tmp_path: Path) -> None:
    scorecard_path = tmp_path / "capacity_scorecard.html"
    res_demo = runner.invoke(cli_app, ["demo", "--html-out", str(scorecard_path)])
    assert res_demo.exit_code == 0
    assert "Capacity v0.3.0" in res_demo.stdout
    assert scorecard_path.is_file()

    res_rec = runner.invoke(cli_app, ["recommend", "--demo", "--active-only", "--by-region"])
    assert res_rec.exit_code == 0
    assert "gemini-3.8-flash" in res_rec.stdout

    res_opt = runner.invoke(cli_app, ["optimize", "--model", "gemini-3.8-flash"])
    assert res_opt.exit_code == 0
    assert "Illusion=True" in res_opt.stdout

    vllm_prom = REPO_ROOT / "examples" / "vllm_prometheus_metrics.txt"
    res_sh = runner.invoke(
        cli_app,
        [
            "optimize",
            "--mode",
            "self-hosted",
            "--model",
            "llama-3.3-70b-fp8",
            "--vllm-file",
            str(vllm_prom),
        ],
    )
    assert res_sh.exit_code == 0
    assert "Upfront GPU Quota Lock:" in res_sh.stdout

    res_gw = runner.invoke(
        cli_app,
        ["gateway", "--target", "all", "--model", "gemini-3.8-flash", "--gsus", "20"],
    )
    assert res_gw.exit_code == 0
    assert "litellm_config.yaml" in res_gw.stdout
    assert "AIGatewayRoute" in res_gw.stdout
    assert "EndpointPickerConfig" in res_gw.stdout

    res_sched = runner.invoke(
        cli_app,
        ["schedule", "--model", "gemini-3.8-flash", "--gsus", "20"],
    )
    assert res_sched.exit_code == 0
    assert "3-Tier Residual Capacity Schedule" in res_sched.stdout

    res_cb = runner.invoke(
        cli_app,
        ["chargeback", "--model", "gemini-3.8-flash", "--gsus", "10", "--waterfill-tpm", "300000"],
    )
    assert res_cb.exit_code == 0
    assert "Unweighted Raw $" in res_cb.stdout
