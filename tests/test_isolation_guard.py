"""Folder-level architectural isolation guard (`P2.3` / `ADR-005`).

Enforces one explicit import rule per folder across Ports, Adapters and Contracts:
- `contract/`: imports only stdlib, `pydantic`, and `capacity.contract`.
- `engine/`: imports only stdlib, `capacity.contract`, and `capacity.engine`.
- `ingest/`: imports only stdlib, `capacity.contract`, `capacity.engine`, and `capacity.ingest`.
- `telemetry/` (except `cloud_monitoring.py`): imports only stdlib, `capacity.contract`,
  `capacity.engine`, and `capacity.telemetry.{port,local}`.
- `telemetry/cloud_monitoring.py`: the sole module allowed to import `google.*` Cloud SDKs.
- `gateway/`: imports only stdlib, `yaml` (`litellm` optional in callback), `capacity.contract`,
  `capacity.engine`, and `capacity.gateway`.
- `showcase/{evaluate,sample_data}.py`: orchestrates scenario evaluation over `contract`, `engine`,
  `gateway`, `telemetry.local`, and `yaml`.
- `showcase/` renderer modules (`renderer`, `view`, `tabs`, `governance_tabs`): import zero
  calculations — only stdlib, `capacity.contract`, and sibling renderer modules.
- `cli/` and `api.py`: driving ports — may import any `capacity.*` module plus `fastapi`, `typer`,
  `uvicorn`, and `dotenv`, while still delegating `google.*` SDK calls to `cloud_monitoring.py`.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPACITY_DIR = REPO_ROOT / "capacity"
AST_RULES_PATH = REPO_ROOT / "specs" / "ast.rules.yaml"

DYNAMIC_IMPORT = "<dynamic import>"
ESCAPED_RELATIVE_IMPORT = "<escaped relative import>"
STDLIB_ROOTS: frozenset[str] = sys.stdlib_module_names

SHOWCASE_EVAL_FILES: frozenset[str] = frozenset(
    {
        "showcase/__init__.py",
        "showcase/evaluate.py",
        "showcase/sample_data.py",
    }
)


@dataclass(frozen=True)
class FolderRule:
    name: str
    allowed_capacity_prefixes: tuple[str, ...]
    allowed_third_party_roots: frozenset[str] = frozenset()
    forbidden_capacity_prefixes: tuple[str, ...] = ()


FOLDER_RULES: dict[str, FolderRule] = {
    "root": FolderRule(name="root", allowed_capacity_prefixes=()),
    "contract": FolderRule(
        name="contract",
        allowed_capacity_prefixes=("capacity.contract",),
        allowed_third_party_roots=frozenset({"pydantic"}),
    ),
    "engine": FolderRule(
        name="engine",
        allowed_capacity_prefixes=("capacity.contract", "capacity.engine"),
    ),
    "ingest": FolderRule(
        name="ingest",
        allowed_capacity_prefixes=("capacity.contract", "capacity.engine", "capacity.ingest"),
    ),
    "telemetry_pure": FolderRule(
        name="telemetry_pure",
        allowed_capacity_prefixes=(
            "capacity.contract",
            "capacity.engine",
            "capacity.telemetry.port",
            "capacity.telemetry.local",
        ),
        forbidden_capacity_prefixes=("capacity.telemetry.cloud_monitoring",),
    ),
    "cloud_monitoring": FolderRule(
        name="cloud_monitoring",
        allowed_capacity_prefixes=(
            "capacity.contract",
            "capacity.engine",
            "capacity.telemetry.port",
        ),
        allowed_third_party_roots=frozenset({"google"}),
    ),
    "gateway": FolderRule(
        name="gateway",
        allowed_capacity_prefixes=("capacity.contract", "capacity.engine", "capacity.gateway"),
        allowed_third_party_roots=frozenset({"yaml", "litellm"}),
    ),
    "showcase_eval": FolderRule(
        name="showcase_eval",
        allowed_capacity_prefixes=(
            "capacity.contract",
            "capacity.engine",
            "capacity.gateway",
            "capacity.telemetry.local",
            "capacity.showcase.evaluate",
            "capacity.showcase.sample_data",
        ),
        allowed_third_party_roots=frozenset({"yaml"}),
        forbidden_capacity_prefixes=("capacity.telemetry.cloud_monitoring",),
    ),
    "showcase_renderer": FolderRule(
        name="showcase_renderer",
        allowed_capacity_prefixes=("capacity.contract", "capacity.showcase"),
        forbidden_capacity_prefixes=(
            "capacity.showcase.evaluate",
            "capacity.showcase.sample_data",
        ),
    ),
    "driving_port": FolderRule(
        name="driving_port",
        allowed_capacity_prefixes=("capacity",),
        allowed_third_party_roots=frozenset(
            {"fastapi", "typer", "uvicorn", "dotenv", "pydantic", "yaml"}
        ),
    ),
}


def _all_package_modules(root: Path = CAPACITY_DIR) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not p.name.startswith(".")
    )


_EXACT_ROLE_MAP: dict[str, str] = {
    "__init__.py": "root",
    "api.py": "driving_port",
    "telemetry/cloud_monitoring.py": "cloud_monitoring",
    **dict.fromkeys(SHOWCASE_EVAL_FILES, "showcase_eval"),
}
_PREFIX_ROLE_MAP: tuple[tuple[str, str], ...] = (
    ("cli/", "driving_port"),
    ("contract/", "contract"),
    ("engine/", "engine"),
    ("ingest/", "ingest"),
    ("telemetry/", "telemetry_pure"),
    ("gateway/", "gateway"),
    ("showcase/", "showcase_renderer"),
)


def _classify_rel_path(rel_posix: str) -> str:
    if rel_posix in _EXACT_ROLE_MAP:
        return _EXACT_ROLE_MAP[rel_posix]
    for prefix, role in _PREFIX_ROLE_MAP:
        if rel_posix.startswith(prefix):
            return role
    raise AssertionError(f"Unclassified module path under capacity/: {rel_posix}")


def _dotted_module_name(path: Path, repo_root: Path = REPO_ROOT) -> str:
    rel = path.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(
    source: str,
    *,
    module_rel_path: str = "capacity/engine/probe.py",
) -> list[str]:
    """Resolve every absolute or relative import target in `source`."""
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


def _matches_prefix(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(f"{prefix}.")


def _violations_for_module(source: str, *, module_rel_path: str) -> list[str]:
    rel_inside_pkg = Path(module_rel_path).relative_to("capacity").as_posix()
    role = _classify_rel_path(rel_inside_pkg)
    rule = FOLDER_RULES[role]
    bad: list[str] = []

    for target in _import_targets(source, module_rel_path=module_rel_path):
        if target in {DYNAMIC_IMPORT, ESCAPED_RELATIVE_IMPORT}:
            bad.append(target)
            continue
        top = target.split(".", 1)[0]
        if top in {"examples", "benchmarks"}:
            bad.append(target)
            continue
        if top == "capacity":
            if target == "capacity":
                continue
            if any(_matches_prefix(target, pfx) for pfx in rule.forbidden_capacity_prefixes):
                bad.append(target)
            elif not any(_matches_prefix(target, pfx) for pfx in rule.allowed_capacity_prefixes):
                bad.append(target)
            continue
        if top in STDLIB_ROOTS:
            continue
        if top not in rule.allowed_third_party_roots:
            bad.append(target)
    return bad


def test_every_folder_has_one_rule_and_zero_violations() -> None:
    all_files = _all_package_modules()
    seen_roles: set[str] = set()
    problems: list[str] = []

    for path in all_files:
        rel_inside = path.relative_to(CAPACITY_DIR).as_posix()
        role = _classify_rel_path(rel_inside)
        seen_roles.add(role)
        rel_repo = path.relative_to(REPO_ROOT).as_posix()
        for bad_target in _violations_for_module(
            path.read_text(encoding="utf-8"),
            module_rel_path=rel_repo,
        ):
            problems.append(f"{rel_repo} ({role}) imports forbidden {bad_target}")

    assert seen_roles == set(FOLDER_RULES), f"Unused folder rules: {set(FOLDER_RULES) - seen_roles}"
    assert not problems, "\n".join(problems)


def test_only_cloud_monitoring_imports_google_sdk() -> None:
    importers = [
        path.relative_to(CAPACITY_DIR).as_posix()
        for path in _all_package_modules()
        if any(
            t == "google" or t.startswith("google.")
            for t in _import_targets(
                path.read_text(encoding="utf-8"),
                module_rel_path=path.relative_to(REPO_ROOT).as_posix(),
            )
        )
    ]
    assert importers == ["telemetry/cloud_monitoring.py"]


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        (
            "capacity/engine/probe.py",
            "from capacity.ingest.prometheus import parse_prometheus_metrics",
        ),
        ("capacity/engine/probe.py", "from capacity.telemetry.port import TelemetryPort"),
        ("capacity/engine/probe.py", "from ..telemetry import port"),
        ("capacity/engine/probe.py", "from capacity.gateway.bundle import generate_gateway_bundle"),
        (
            "capacity/engine/probe.py",
            "from capacity.showcase.sample_data import DEMO_OPTIMIZE_SERIES",
        ),
        ("capacity/engine/probe.py", "from pydantic import BaseModel"),
        ("capacity/engine/probe.py", "import yaml"),
        ("capacity/contract/probe.py", "from capacity.engine.catalog import build_model_catalog"),
        (
            "capacity/showcase/renderer.py",
            "from capacity.engine.catalog import build_model_catalog",
        ),
        (
            "capacity/showcase/renderer.py",
            "from capacity.showcase.evaluate import evaluate_showcase_scenario",
        ),
        ("capacity/showcase/renderer.py", "from . import evaluate"),
        ("capacity/showcase/view.py", "from capacity.engine.catalog import normalize_model_id"),
        ("capacity/telemetry/port.py", "from . import cloud_monitoring"),
        ("capacity/telemetry/local.py", "from google.cloud import monitoring_v3"),
        ("capacity/cli/recommend.py", "from google.cloud import monitoring_v3"),
        ("capacity/api.py", "from google.cloud import monitoring_v3"),
        ("capacity/ingest/request_logs.py", "import fastapi"),
        ("capacity/gateway/bundle.py", "from typer import Typer"),
        ("capacity/engine/probe.py", "import os, benchmarks.trace_backtest"),
        ("capacity/engine/probe.py", "import importlib\nimportlib.import_module('capacity.api')"),
        ("capacity/engine/probe.py", "__import__('capacity.api')"),
        ("capacity/engine/probe.py", "from ... import anything"),
    ],
)
def test_folder_guard_catches_every_forbidden_cross_folder_import(
    module_rel_path: str, statement: str
) -> None:
    assert _violations_for_module(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        ("capacity/engine/probe.py", "from capacity.contract.requests import OptimizeRequest"),
        ("capacity/engine/probe.py", "from . import burst"),
        ("capacity/engine/probe.py", "import math"),
        ("capacity/showcase/renderer.py", "from capacity.contract.scenario import ShowcaseReport"),
        ("capacity/showcase/renderer.py", "from .view import PageView"),
        ("capacity/telemetry/cloud_monitoring.py", "from google.cloud import monitoring_v3"),
        (
            "capacity/cli/recommend.py",
            "from capacity.telemetry.cloud_monitoring import CloudMonitoringAdapter",
        ),
        ("capacity/api.py", "from fastapi import FastAPI"),
    ],
)
def test_folder_guard_allows_declared_folder_dependencies(
    module_rel_path: str, statement: str
) -> None:
    assert not _violations_for_module(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


def test_subfolder_probes_fail_both_python_and_ast_grep_guards(tmp_path: Path) -> None:
    ast_grep = Path(sys.executable).parent / "ast-grep"
    if not ast_grep.is_file():
        found = shutil.which("ast-grep")
        assert found is not None, "ast-grep binary required"
        ast_grep = Path(found)

    cases = (
        ("capacity/engine/probe.py", "from ..telemetry import port\n"),
        ("capacity/engine/probe.py", "from pydantic import BaseModel\n"),
        ("capacity/showcase/probe.py", "from . import evaluate\n"),
        ("capacity/showcase/probe.py", "from ..engine.catalog import build_model_catalog\n"),
        ("capacity/telemetry/probe.py", "from google.cloud import monitoring_v3\n"),
        ("capacity/ingest/probe.py", "from ..telemetry import cloud_monitoring\n"),
    )
    for rel_path, bad_import in cases:
        probe_file = tmp_path / rel_path
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_text(bad_import, encoding="utf-8")
        assert _violations_for_module(bad_import, module_rel_path=rel_path), (rel_path, bad_import)

        res = subprocess.run(  # noqa: S603
            [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 1, f"ast-grep missed {rel_path}: {bad_import!r}"
        probe_file.unlink()


def test_importing_non_adapter_modules_loads_no_driving_port_or_cloud_sdk() -> None:
    """A static scan cannot see what a package `__init__` drags in; a clean subprocess import can."""
    pure_modules = [
        _dotted_module_name(path)
        for path in _all_package_modules()
        if _classify_rel_path(path.relative_to(CAPACITY_DIR).as_posix())
        not in {"driving_port", "cloud_monitoring"}
        and _dotted_module_name(path) != "capacity"
    ]
    probe = (
        "import sys\n"
        + "".join(f"import {mod}\n" for mod in pure_modules)
        + "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'fastapi', 'typer', 'uvicorn', 'grpc'} or m.startswith("
        "('google.cloud', 'capacity.api', 'capacity.cli', "
        "'capacity.telemetry.cloud_monitoring'))))"
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
