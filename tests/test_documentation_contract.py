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

    for adr_id in ("ADR-001", "ADR-002", "ADR-003", "ADR-004", "ADR-005", "ADR-006"):
        assert f"## {adr_id}:" in decisions

    assert "Phase 1" in roadmap and "Phase 2" in roadmap
    assert "--mode self-hosted" in readme
    assert "FOCUS 1.2" in readme
    assert "```mermaid" in readme


def test_readme_quickstart_commands_execute_cleanly() -> None:
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

    res_cb = runner.invoke(
        cli_app,
        ["chargeback", "--model", "gemini-3.8-flash", "--gsus", "10", "--waterfill-tpm", "300000"],
    )
    assert res_cb.exit_code == 0
    assert "Unweighted Raw $" in res_cb.stdout
