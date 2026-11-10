"""Repository hygiene & link integrity tests (`ADR-005` / `CRITICAL.md`).

Verifies that `capacity` remains 100% portable and free of:
1. Internal codenames, internal shortlinks, Buganizer IDs, and internal monorepo paths.
2. Customer names or cross-workspace local paths.
3. Hardcoded machine home directory paths or `file:///` links.
4. Sibling-repo relative Markdown links (`../<sibling>/`) or `.github/workflows/` CI files.
5. Broken relative Markdown links across all tracked documentation.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {"dist", "build", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv"}

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Hardcoded local home directory path", re.compile(r"/usr/local/" + r"google/home|\.c\.googlers\.com")),
    ("File URI scheme", re.compile(r"file:///")),
    ("Internal Buganizer ID", re.compile(r"\bb/\d{6,}\b")),
    ("Internal shortlink", re.compile(r"\bgo/(?!mod\b|sum\b|work\b|version\b|test\b|build\b|run\b)[a-zA-Z0-9_-]{3,}")),
    ("Internal monorepo path", re.compile(r"//depot/" + r"google3|\bgoogle3\b")),
    ("Cross-workspace local path", re.compile(r"~/" + r"customers|~/notes|~/ideas|~/robotics|~/projects")),
]


def _iter_tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(p.startswith(".") or p in EXCLUDED_DIRS for p in rel_parts):
            continue
        if rel_parts == ("tests", "test_repo_hygiene.py"):
            continue
        if path.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".toml"}:
            files.append(path)
    return files


def test_zero_github_workflows_and_cloudbuild_present() -> None:
    gh_workflows = REPO_ROOT / ".github" / "workflows"
    assert not gh_workflows.exists(), "CI/CD must use cloudbuild.yaml only (zero .github/workflows/)"
    assert (REPO_ROOT / "cloudbuild.yaml").is_file()


def test_claude_md_symlink_and_spine_caps() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    claude_md = REPO_ROOT / "CLAUDE.md"
    critical_md = REPO_ROOT / "CRITICAL.md"
    decisions_md = REPO_ROOT / "DECISIONS.md"
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert agents_md.is_file()
    assert agents_md.stat().st_size < 24_000, "AGENTS.md must stay strictly below 24 KB truncation cap"
    assert critical_md.is_file()
    assert critical_md.stat().st_size <= 5_000, "CRITICAL.md must stay <= 5 KB"
    assert decisions_md.is_file()
    assert decisions_md.stat().st_size <= 25_000, "DECISIONS.md must stay <= 25 KB"
    assert claude_md.is_symlink() and claude_md.readlink().as_posix() == "AGENTS.md"
    assert "AGENTS.md" not in gitignore
    assert "CLAUDE.md" not in gitignore
    assert ".env" in gitignore


def test_zero_internal_customer_or_machine_path_leaks() -> None:
    violations: list[str] = []
    for fpath in _iter_tracked_text_files():
        text = fpath.read_text(encoding="utf-8")
        rel = fpath.relative_to(REPO_ROOT)
        for label, pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                violations.append(f"{rel}: {label} ({match.group(0)!r})")
    assert not violations, "Repository hygiene violations found:\n" + "\n".join(violations)


def test_all_relative_markdown_links_resolve_within_repo() -> None:
    md_link_re = re.compile(r"\[[^\]\n]+\]\(([^)\s]+)\)")
    broken: list[str] = []
    repo_resolved = REPO_ROOT.resolve()
    for md_file in sorted(REPO_ROOT.rglob("*.md")):
        rel_parts = md_file.relative_to(REPO_ROOT).parts
        if any(p.startswith(".") or p in EXCLUDED_DIRS for p in rel_parts):
            continue
        text = md_file.read_text(encoding="utf-8")
        for match in md_link_re.finditer(text):
            raw_target = match.group(1).split("#", 1)[0]
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:")):
                continue
            target = (md_file.parent / raw_target.replace("%20", " ")).resolve()
            if not target.is_relative_to(repo_resolved):
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: sibling escape link ({raw_target})")
            elif not target.exists():
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: {raw_target}")
    assert not broken, "Broken or escaping markdown links found:\n" + "\n".join(broken)


def test_declarative_ast_rules_and_positive_controls(tmp_path: Path) -> None:
    rules_path = REPO_ROOT / "specs" / "ast.rules.yaml"
    assert rules_path.is_file(), "Missing specs/ast.rules.yaml declarative AST contract"
    raw = rules_path.read_text(encoding="utf-8")
    blocks = [
        c for c in re.split(r"(?m)^---\s*$", raw) if re.search(r"(?m)^id:\s*\S+", c)
    ]
    assert len(blocks) >= 4, "Expected >= 4 rules in specs/ast.rules.yaml"

    ast_grep = shutil.which("ast-grep") or str(
        Path.home() / ".local" / "bin" / "ast-grep"
    )
    if not Path(ast_grep).is_file():
        pytest.skip("ast-grep binary not installed on this runner")

    live = subprocess.run(  # noqa: S603
        [ast_grep, "scan", "--rule", str(rules_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert live.returncode == 0, f"AST scan failed:\n{live.stdout}\n{live.stderr}"

    for idx, chunk in enumerate(blocks):
        id_m = re.search(r"(?m)^id:\s*(\S+)", chunk)
        sev_m = re.search(r"(?m)^severity:\s*(\S+)", chunk)
        path_m = re.search(
            r'(?m)^\s+test_path:\s*["\']?([^"\'\r\n]+)["\']?\s*$', chunk
        )
        assert id_m is not None and sev_m is not None and path_m is not None
        rule_id = id_m.group(1)
        assert sev_m.group(1) == "error", f"{rule_id} must set severity: error"

        case_dir = tmp_path / f"case_{idx}_{rule_id}"
        target_file = case_dir / path_m.group(1)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        single_rule = case_dir / "_rule.yaml"
        single_rule.write_text(chunk.strip() + "\n", encoding="utf-8")

        for key, expected_rc in (("invalid_examples", 1), ("valid_examples", 0)):
            m = re.search(rf"(?m)^\s+{key}:\s*\n((?:\s+-\s+[^\r\n]+\n?)+)", chunk)
            assert m is not None, f"{rule_id} missing metadata.{key}"
            for line in m.group(1).splitlines():
                stripped = line.strip()
                if stripped.startswith("- "):
                    snippet = str(ast.literal_eval(stripped[2:].strip()))
                    target_file.write_text(f"{snippet}\n", encoding="utf-8")
                    res = subprocess.run(  # noqa: S603
                        [ast_grep, "scan", "--rule", str(single_rule), "."],
                        cwd=case_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    assert res.returncode == expected_rc, (
                        f"{rule_id} ({key}) failed on {snippet!r}:\n{res.stdout}"
                    )

