"""Package isolation guard (`Rule 0` / `STD-ARCH` / `STD-PORT`).

Every module in `capacity/` is sizing or FinOps maths unless it is listed as an adapter. The
maths may not import a driving port (FastAPI, Typer), the Cloud Monitoring adapter, or the
examples and benchmarks, however the import is spelt.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPACITY_DIR = REPO_ROOT / "capacity"

# The only modules that may touch a web framework, the CLI toolkit or a cloud SDK. A new
# module is pure until someone adds it here.
ADAPTER_MODULES = frozenset({"api.py", "cli.py", "cli_formatters.py", "core.py"})

FORBIDDEN_IN_PURE_MODULES = (
    "fastapi",
    "typer",
    "uvicorn",
    "google.cloud",
    "capacity.api",
    "capacity.cli",
    "capacity.cli_formatters",
    "capacity.core",
    "examples",
    "benchmarks",
)
DYNAMIC_IMPORT = "<dynamic import>"


def _import_targets(source: str) -> list[str]:
    """Every module the imports in `source` can load, with relative imports resolved."""
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            package = "capacity" if node.level else ""
            base = ".".join(part for part in (package, node.module or "") if part)
            targets.append(base)
            # `from pkg import name` loads `pkg.name` when that is a submodule.
            targets.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name in {"import_module", "__import__"}:
                targets.append(DYNAMIC_IMPORT)
    return targets


def _violations(source: str) -> list[str]:
    return [
        target
        for target in _import_targets(source)
        if target == DYNAMIC_IMPORT
        or any(target == pfx or target.startswith(f"{pfx}.") for pfx in FORBIDDEN_IN_PURE_MODULES)
    ]


def _pure_modules() -> list[Path]:
    return sorted(p for p in CAPACITY_DIR.glob("*.py") if p.name not in ADAPTER_MODULES)


def test_pure_modules_never_import_ports_or_the_cloud_adapter() -> None:
    pure = _pure_modules()
    assert len(pure) >= 12, "the purity list is default-deny; it should cover most of the package"
    problems = [
        f"capacity/{path.name} imports {target}"
        for path in pure
        for target in _violations(path.read_text(encoding="utf-8"))
    ]
    assert not problems, "\n".join(problems)


def test_adapter_list_names_real_modules() -> None:
    for name in ADAPTER_MODULES:
        assert (CAPACITY_DIR / name).is_file(), f"ADAPTER_MODULES lists a missing module: {name}"


@pytest.mark.parametrize(
    "statement",
    [
        "from capacity.core import fetch_throughput",
        "import capacity.core",
        "import capacity.core as adapter",
        "from capacity import core",
        "from capacity import finops, core",
        "from . import core",
        "from .core import fetch_throughput",
        "from capacity import api",
        "from google import cloud",
        "from google.cloud import monitoring_v3",
        "import fastapi",
        "from typer import Typer",
        "import benchmarks.trace_backtest",
        "import importlib\nimportlib.import_module('capacity.core')",
        "__import__('capacity.core')",
    ],
)
def test_guard_catches_every_spelling_of_a_forbidden_import(statement: str) -> None:
    assert _violations(statement), statement


@pytest.mark.parametrize(
    "statement",
    [
        "from capacity.models import ModelRecommendation",
        "from capacity import finops",
        "from . import burst",
        "from .forecasting import percentile",
        "import corelib",
        "from capacity.coreutils import helper",
        "# from capacity import core",
        'text = "import capacity.core"',
        "import math",
    ],
)
def test_guard_ignores_clean_imports_comments_and_near_misses(statement: str) -> None:
    assert not _violations(statement), statement


def test_importing_the_maths_loads_no_port_and_no_cloud_sdk() -> None:
    """A static scan cannot see what a package `__init__` drags in; an import can."""
    modules = [path.stem for path in _pure_modules() if path.stem != "__init__"]
    probe = (
        "import sys\n"
        + "".join(f"import capacity.{name}\n" for name in modules)
        + "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'fastapi', 'typer', 'uvicorn', 'grpc'} or m.startswith("
        "('google.cloud', 'capacity.core', 'capacity.api', 'capacity.cli'))))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_all_production_modules_respect_structural_loc_ceiling() -> None:
    oversized: list[str] = []
    for py_path in sorted(CAPACITY_DIR.glob("*.py")):
        loc = sum(
            1
            for line in py_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        if loc > 800:
            oversized.append(f"{py_path.name}: {loc} non-blank/non-comment lines (> 800 ceiling)")
    assert not oversized, "Structural simplicity ceiling exceeded:\n" + "\n".join(oversized)
