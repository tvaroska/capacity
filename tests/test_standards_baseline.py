"""Standards baseline ratchet: lint/typing config, file size, comment hygiene.

Gaps that existed when this gate was added are recorded in `standards_debt.json`.
Nothing recorded there may get worse, and an entry must be deleted once fixed.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    raise unittest.SkipTest("needs Python 3.11+ for tomllib") from None

REPO_ROOT = Path(__file__).resolve().parents[1]
DEBT_PATH = Path(__file__).with_name("standards_debt.json")

REQUIRED_RUFF_RULES = (
    "E",
    "W",
    "F",
    "I",
    "B",
    "C4",
    "UP",
    "S",
    "BLE",
    "ANN",
    "RUF",
    "C90",
    "PLR0911",
    "PLR0912",
    "PLR0913",
    "PLR0915",
)
BANNED_GLOBAL_IGNORES = ("BLE001", "E722", "B904")
MIN_PYTHON = (3, 12)
MAX_SOURCE_LINES = 800

SOURCE_SUFFIXES = {".py", ".go", ".ts", ".tsx", ".js", ".c", ".h", ".cpp"}
SKIP_DIRS = {
    "__pycache__",
    "__tests__",
    "alembic",
    "artifacts",
    "bucket",
    "build",
    "coverage",
    "dist",
    "e2e",
    "generated",
    "htmlcov",
    "managed_components",
    "migrations",
    "node_modules",
    "site-packages",
    "test",
    "tests",
    "vendor",
    "venv",
}
TEST_FILE_RE = re.compile(r"^(test_.*\.py|conftest\.py|.*_test\.go|.*\.(test|spec)\.\w+)$")
GENERATED_FILE_RE = re.compile(r"\.(min\.js|d\.ts)$")
NARRATION_RE = re.compile(r"^\s*(?:#|//)\s*(?:[=\-─#*]{8,}|\d+[.)]\s|Step \d)", re.MULTILINE)
REQUIRES_PYTHON_RE = re.compile(r">=\s*(\d+)\.(\d+)")


def _source_files() -> list[Path]:
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            path = Path(root) / name
            if path.suffix not in SOURCE_SUFFIXES or path.is_symlink():
                continue
            if TEST_FILE_RE.match(name) or GENERATED_FILE_RE.search(name):
                continue
            found.append(path)
    return found


def _selects(selected: list[str], rule: str) -> bool:
    return any(s == "ALL" or rule.startswith(s) for s in selected)


def _config_gaps() -> set[str] | None:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return None
    config: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool: dict[str, Any] = config.get("tool", {})
    ruff: dict[str, Any] = tool.get("ruff", {})
    lint: dict[str, Any] = ruff.get("lint", {})
    selected: list[str] = lint.get("select", ruff.get("select", []))
    ignored: list[str] = lint.get("ignore", ruff.get("ignore", []))

    gaps = {f"ruff:{r}" for r in REQUIRED_RUFF_RULES if not _selects(selected, r)}
    gaps |= {f"ruff-ignore:{r}" for r in BANNED_GLOBAL_IGNORES if r in ignored}

    mypy: dict[str, Any] | None = tool.get("mypy")
    if mypy is None:
        gaps.add("mypy:missing")
    elif not (
        mypy.get("strict") or (mypy.get("disallow_untyped_defs") and mypy.get("check_untyped_defs"))
    ):
        gaps.add("mypy:not-strict")

    if not (REPO_ROOT / "uv.lock").is_file():
        gaps.add("uv-lock:missing")

    requires: str = config.get("project", {}).get("requires-python", "")
    floor = REQUIRES_PYTHON_RE.search(requires)
    if floor is None or (int(floor.group(1)), int(floor.group(2))) < MIN_PYTHON:
        gaps.add("python:below-3.12")
    return gaps


def measure() -> dict[str, Any]:
    """Current state in the same shape as `standards_debt.json`."""
    oversized: dict[str, int] = {}
    narration = 0
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = sum(1 for line in text.splitlines() if line.strip())
        if lines > MAX_SOURCE_LINES:
            oversized[path.relative_to(REPO_ROOT).as_posix()] = lines
        narration += len(NARRATION_RE.findall(text))
    gaps = _config_gaps()
    return {
        "config": None if gaps is None else sorted(gaps),
        "oversized_files": oversized,
        "narration_comments": narration,
    }


class StandardsBaselineTest(unittest.TestCase):
    debt: dict[str, Any]
    state: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.debt = json.loads(DEBT_PATH.read_text(encoding="utf-8"))
        cls.state = measure()

    def test_config_has_no_new_gaps(self) -> None:
        if self.state["config"] is None:
            self.skipTest("no pyproject.toml")
        new = set(self.state["config"]) - set(self.debt["config"])
        self.assertFalse(new, f"pyproject.toml fell below the standards baseline: {sorted(new)}")

    def test_config_debt_is_current(self) -> None:
        if self.state["config"] is None:
            self.skipTest("no pyproject.toml")
        fixed = set(self.debt["config"]) - set(self.state["config"])
        self.assertFalse(
            fixed, f"Fixed; drop these 'config' entries in {DEBT_PATH.name}: {sorted(fixed)}"
        )

    def test_no_new_or_growing_oversized_files(self) -> None:
        caps: dict[str, int] = self.debt["oversized_files"]
        grown = {
            path: lines
            for path, lines in self.state["oversized_files"].items()
            if lines > caps.get(path, MAX_SOURCE_LINES)
        }
        self.assertFalse(
            grown,
            f"Source files over {MAX_SOURCE_LINES} non-blank lines (or past their "
            f"recorded cap); split the file before adding to it: {grown}",
        )

    def test_oversized_debt_is_current(self) -> None:
        still_oversized = self.state["oversized_files"]
        fixed = sorted(
            path
            for path in self.debt["oversized_files"]
            if path not in still_oversized and (REPO_ROOT / path).is_file()
        )
        self.assertFalse(
            fixed, f"Fixed; drop these 'oversized_files' entries in {DEBT_PATH.name}: {fixed}"
        )

    def test_narration_comments_do_not_grow(self) -> None:
        self.assertLessEqual(
            self.state["narration_comments"],
            self.debt["narration_comments"],
            "New numbered-step or banner comments; comments explain why, not what",
        )


if __name__ == "__main__":
    unittest.main()
