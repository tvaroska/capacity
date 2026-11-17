"""Package isolation guard (`Rule 0` / `STD-ARCH` / `STD-PORT`).

Every module under `capacity/` (including sub-packages) is pure sizing or FinOps maths unless it is
classified as an adapter. Pure modules may not import a driving port (FastAPI, Typer, Uvicorn),
Google Cloud SDKs, the adapter modules, dynamic imports, or `examples/` and `benchmarks/`, however
the import is spelt.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPACITY_DIR = REPO_ROOT / "capacity"
AST_RULES_PATH = REPO_ROOT / "specs" / "ast.rules.yaml"

# Named once: entries ending with '/' classify whole sub-folders; bare paths classify single files.
ADAPTER_SPECS: tuple[str, ...] = (
    "api.py",
    "cli.py",
    "cli_formatters.py",
    "core.py",
)

FORBIDDEN_IN_PURE_MODULES: tuple[str, ...] = (
    "fastapi",
    "typer",
    "uvicorn",
    "google.cloud",
    "google.auth",
    "google.api_core",
    "capacity.api",
    "capacity.cli",
    "capacity.cli_formatters",
    "capacity.core",
    "capacity.telemetry.cloud_monitoring",
    "examples",
    "benchmarks",
)
DYNAMIC_IMPORT = "<dynamic import>"
ESCAPED_RELATIVE_IMPORT = "<escaped relative import>"


def _all_package_modules(root: Path = CAPACITY_DIR) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def _is_adapter(rel_posix: str) -> bool:
    return any(
        rel_posix.startswith(spec) if spec.endswith("/") else rel_posix == spec
        for spec in ADAPTER_SPECS
    )


def _classify_modules(root: Path = CAPACITY_DIR) -> tuple[list[Path], list[Path]]:
    pure: list[Path] = []
    adapters: list[Path] = []
    for path in _all_package_modules(root):
        rel = path.relative_to(root).as_posix()
        if _is_adapter(rel):
            adapters.append(path)
        else:
            pure.append(path)
    return pure, adapters


def _dotted_module_name(path: Path, repo_root: Path = REPO_ROOT) -> str:
    rel = path.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(
    source: str,
    *,
    module_rel_path: str = "capacity/probe.py",
) -> list[str]:
    """Every module the imports in `source` can load, resolving relative imports from `module_rel_path`."""
    pkg_parts = list(Path(module_rel_path).parent.parts)
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                if up >= len(pkg_parts):
                    targets.append(ESCAPED_RELATIVE_IMPORT)
                    continue
                anchor = ".".join(pkg_parts[: len(pkg_parts) - up])
            else:
                anchor = ""
            base = ".".join(part for part in (anchor, node.module or "") if part)
            targets.append(base)
            targets.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name in {"import_module", "__import__"}:
                targets.append(DYNAMIC_IMPORT)
    return targets


def _violations(source: str, *, module_rel_path: str = "capacity/probe.py") -> list[str]:
    return [
        target
        for target in _import_targets(source, module_rel_path=module_rel_path)
        if target in {DYNAMIC_IMPORT, ESCAPED_RELATIVE_IMPORT}
        or any(target == pfx or target.startswith(f"{pfx}.") for pfx in FORBIDDEN_IN_PURE_MODULES)
    ]


def test_every_module_is_classified_once_and_pure_modules_stay_isolated() -> None:
    all_files = _all_package_modules()
    pure, adapters = _classify_modules()

    assert set(pure).isdisjoint(set(adapters)), "a module cannot be both pure and an adapter"
    assert set(pure) | set(adapters) == set(all_files), (
        "every capacity/*.py file must be classified"
    )
    assert len(pure) >= 12, "the purity list is default-deny; it should cover most of the package"

    for spec in ADAPTER_SPECS:
        matched = [
            p
            for p in adapters
            if (
                p.relative_to(CAPACITY_DIR).as_posix().startswith(spec)
                if spec.endswith("/")
                else p.relative_to(CAPACITY_DIR).as_posix() == spec
            )
        ]
        assert matched, f"ADAPTER_SPECS entry matches no real file: {spec}"

    problems = [
        f"{path.relative_to(REPO_ROOT).as_posix()} imports {target}"
        for path in pure
        for target in _violations(
            path.read_text(encoding="utf-8"),
            module_rel_path=path.relative_to(REPO_ROOT).as_posix(),
        )
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        ("capacity/probe.py", "from capacity.core import fetch_throughput"),
        ("capacity/probe.py", "import capacity.core"),
        ("capacity/probe.py", "import math, capacity.core as adapter"),
        ("capacity/probe.py", "from capacity import core"),
        ("capacity/probe.py", "from capacity import finops, core"),
        ("capacity/probe.py", "from . import core"),
        ("capacity/probe.py", "from .core import fetch_throughput"),
        ("capacity/engine/probe.py", "from .. import core"),
        ("capacity/engine/probe.py", "from .. import (\n    finops,\n    core,\n)"),
        ("capacity/engine/probe.py", "from ..core import fetch_throughput"),
        ("capacity/engine/nested/probe.py", "from ... import core"),
        ("capacity/engine/nested/probe.py", "from ...core import fetch_throughput"),
        ("capacity/engine/probe.py", "from ... import anything"),
        ("capacity/engine/probe.py", "from capacity import api"),
        ("capacity/engine/probe.py", "from google import cloud"),
        ("capacity/engine/probe.py", "from google.cloud import monitoring_v3"),
        ("capacity/engine/probe.py", "from google.auth.exceptions import DefaultCredentialsError"),
        ("capacity/engine/probe.py", "from google.api_core.exceptions import PermissionDenied"),
        ("capacity/engine/probe.py", "import math, fastapi"),
        ("capacity/engine/probe.py", "from typer import Typer"),
        ("capacity/engine/probe.py", "import os, benchmarks.trace_backtest"),
        ("capacity/engine/probe.py", "import importlib\nimportlib.import_module('capacity.core')"),
        ("capacity/engine/probe.py", "__import__('capacity.core')"),
    ],
)
def test_guard_catches_every_spelling_of_a_forbidden_import(
    module_rel_path: str, statement: str
) -> None:
    assert _violations(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        ("capacity/probe.py", "from capacity.models import ModelRecommendation"),
        ("capacity/probe.py", "from capacity import finops"),
        ("capacity/probe.py", "from . import burst"),
        ("capacity/probe.py", "from .forecasting import percentile"),
        ("capacity/engine/probe.py", "from . import burst"),
        ("capacity/engine/probe.py", "from .. import finops"),
        ("capacity/engine/probe.py", "from ..forecasting import percentile"),
        ("capacity/engine/probe.py", "import corelib"),
        ("capacity/engine/probe.py", "from capacity.coreutils import helper"),
        ("capacity/engine/probe.py", "# from .. import core"),
        ("capacity/engine/probe.py", 'text = "import capacity.core"'),
        ("capacity/engine/probe.py", "import math"),
    ],
)
def test_guard_ignores_clean_imports_comments_and_near_misses(
    module_rel_path: str, statement: str
) -> None:
    assert not _violations(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


def test_subfolder_probe_importing_cloud_adapter_fails_both_guards(tmp_path: Path) -> None:
    ast_grep = Path(sys.executable).parent / "ast-grep"
    if not ast_grep.is_file():
        found = shutil.which("ast-grep")
        assert found is not None, "ast-grep binary required"
        ast_grep = Path(found)

    probe_dir = tmp_path / "capacity" / "engine"
    probe_dir.mkdir(parents=True)
    (tmp_path / "capacity" / "__init__.py").write_text("", encoding="utf-8")
    (probe_dir / "__init__.py").write_text("", encoding="utf-8")
    probe_file = probe_dir / "probe.py"

    for bad_import in (
        "from ..core import fetch_throughput\n",
        "from .. import core\n",
        "import math, capacity.core\n",
        "from google.cloud import monitoring_v3\n",
    ):
        probe_file.write_text(bad_import, encoding="utf-8")
        pure, adapters = _classify_modules(tmp_path / "capacity")
        assert probe_file in pure and probe_file not in adapters
        assert _violations(bad_import, module_rel_path="capacity/engine/probe.py"), bad_import

        res = subprocess.run(  # noqa: S603
            [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 1, f"ast-grep missed subfolder violation {bad_import!r}"


def test_importing_the_maths_loads_no_port_and_no_cloud_sdk() -> None:
    """A static scan cannot see what a package `__init__` drags in; an import can."""
    pure, _ = _classify_modules()
    modules = [
        _dotted_module_name(path) for path in pure if _dotted_module_name(path) != "capacity"
    ]
    probe = (
        "import sys\n"
        + "".join(f"import {mod}\n" for mod in modules)
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
    for py_path in _all_package_modules():
        loc = sum(
            1
            for line in py_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        if loc > 800:
            rel = py_path.relative_to(CAPACITY_DIR).as_posix()
            oversized.append(f"{rel}: {loc} non-blank/non-comment lines (> 800 ceiling)")
    assert not oversized, "Structural simplicity ceiling exceeded:\n" + "\n".join(oversized)
