"""Repository hygiene & link integrity tests (`ADR-005` / `CRITICAL.md`)."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    "dist",
    "build",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
}

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Hardcoded local home directory path",
        re.compile(r"/usr/local/" + r"google/home|\.c\.googlers\.com"),
    ),
    ("File URI scheme", re.compile(r"file:///")),
    ("Internal Buganizer ID", re.compile(r"\bb/\d{6,}\b")),
    (
        "Internal shortlink",
        re.compile(r"\bgo/(?!mod\b|sum\b|work\b|version\b|test\b|build\b|run\b)[a-zA-Z0-9_-]{3,}"),
    ),
    ("Internal monorepo path", re.compile(r"//depot/" + r"google3|\bgoogle3\b")),
    (
        "Cross-workspace local path",
        re.compile(r"~/" + r"customers|~/notes|~/ideas|~/robotics|~/projects"),
    ),
    ("Cookie-gated GCS URL", re.compile(r"https://storage\.cloud\.google\.com/")),
    # A kernel release string names the machine image a file was generated on.
    ("Machine kernel string", re.compile(r"\bLinux-\d+\.\d+\.\d+-")),
]


def _resolve_ast_grep_binary() -> str | None:
    # Not `.resolve()`: the venv's python is a symlink, and ast-grep sits beside the link.
    venv_candidate = Path(sys.executable).parent / "ast-grep"
    if venv_candidate.is_file():
        return str(venv_candidate)
    which_candidate = shutil.which("ast-grep")
    if which_candidate and Path(which_candidate).is_file():
        return which_candidate
    home_candidate = Path.home() / ".local" / "bin" / "ast-grep"
    if home_candidate.is_file():
        return str(home_candidate)
    return None


TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".svg",
    ".json",
    ".jsonl",
    ".txt",
    ".lock",
    ".sh",
    ".html",
    ".cfg",
    ".ini",
}
# Shipped files the suffix list would miss. The local `.env` is left out: it is never tracked.
TEXT_NAMES = {
    "Dockerfile",
    "LICENSE",
    ".dockerignore",
    ".gcloudignore",
    ".gitignore",
    ".env.example",
    ".python-version",
}


def _iter_tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        *dirs, name = path.relative_to(REPO_ROOT).parts
        if any((d.startswith(".") and d != ".stitch") or d in EXCLUDED_DIRS for d in dirs):
            continue
        if (*dirs, name) == ("tests", "test_repo_hygiene.py"):
            continue
        if name in TEXT_NAMES or (
            not name.startswith(".") and path.suffix.lower() in TEXT_SUFFIXES
        ):
            files.append(path)
    return files


def test_zero_github_workflows_and_cloudbuild_present() -> None:
    gh_workflows = REPO_ROOT / ".github" / "workflows"
    assert not gh_workflows.exists(), "CI/CD must use cloudbuild.yaml only"
    assert (REPO_ROOT / "cloudbuild.yaml").is_file()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in dockerignore and ".env.*" in dockerignore


def test_claude_md_symlink_and_spine_caps() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    claude_md = REPO_ROOT / "CLAUDE.md"
    critical_md = REPO_ROOT / "CRITICAL.md"
    decisions_md = REPO_ROOT / "DECISIONS.md"
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert agents_md.is_file() and agents_md.stat().st_size < 24_000
    assert critical_md.is_file() and critical_md.stat().st_size <= 5_000
    assert decisions_md.is_file() and decisions_md.stat().st_size <= 25_000
    assert claude_md.is_symlink() and claude_md.readlink().as_posix() == "AGENTS.md"
    assert "AGENTS.md" not in gitignore
    assert "CLAUDE.md" not in gitignore
    assert ".env" in gitignore


def test_zero_internal_customer_or_machine_path_leaks() -> None:
    violations: list[str] = []
    for fpath in _iter_tracked_text_files():
        text = fpath.read_text(encoding="utf-8", errors="ignore")
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
        if any((p.startswith(".") and p != ".stitch") or p in EXCLUDED_DIRS for p in rel_parts):
            continue
        text = md_file.read_text(encoding="utf-8")
        for match in md_link_re.finditer(text):
            raw_target = match.group(1).split("#", 1)[0]
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:")):
                continue
            target = (md_file.parent / raw_target.replace("%20", " ")).resolve()
            if not target.is_relative_to(repo_resolved):
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: sibling escape ({raw_target})")
            elif not target.exists():
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: {raw_target}")
    assert not broken, "Broken or escaping markdown links found:\n" + "\n".join(broken)


def test_declarative_ast_rules_and_positive_controls(tmp_path: Path) -> None:
    rules_path = REPO_ROOT / "specs" / "ast.rules.yaml"
    assert rules_path.is_file(), "Missing specs/ast.rules.yaml declarative AST contract"
    raw = rules_path.read_text(encoding="utf-8")
    blocks = [c for c in re.split(r"(?m)^---\s*$", raw) if re.search(r"(?m)^id:\s*\S+", c)]
    assert len(blocks) >= 5, "Expected >= 5 rules in specs/ast.rules.yaml"

    ast_grep = _resolve_ast_grep_binary()
    if ast_grep is None:
        pytest.fail("ast-grep is missing; run `uv sync --extra dev` (a skip would hide the gate)")

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
        path_m = re.search(r'(?m)^\s+test_path:\s*["\']?([^"\'\r\n]+)["\']?\s*$', chunk)
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
                    raw_val = stripped[2:].strip()
                    snippet = (
                        str(ast.literal_eval(raw_val))
                        if raw_val.startswith(('"', "'"))
                        else raw_val
                    )
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


def test_code_is_formatted() -> None:
    ruff = Path(sys.executable).parent / "ruff"
    result = subprocess.run(  # noqa: S603
        [str(ruff), "format", "--check", "--no-cache", "capacity", "tests", "benchmarks"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Run `ruff format capacity tests benchmarks`:\n{result.stdout}{result.stderr}"
    )
