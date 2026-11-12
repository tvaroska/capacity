"""Mechanical Hexagonal AST isolation guard (`Rule 0` / `STD-ARCH` / `STD-PORT`)."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPACITY_DIR = REPO_ROOT / "capacity"

PURE_DOMAIN_MODULES = (
    "burst.py",
    "finops.py",
    "self_hosted.py",
    "telemetry_parsers.py",
    "capacity.py",
    "gateway.py",
    "forecasting.py",
    "model_families.py",
    "models.py",
)

FORBIDDEN_DRIVING_OR_CLOUD_PREFIXES = (
    "fastapi",
    "typer",
    "uvicorn",
    "google.cloud",
    "capacity.api",
    "capacity.cli",
    "capacity.core",
    "examples",
    "benchmarks",
)


def _imported_targets(source: str) -> list[str]:
    tree = ast.parse(source)
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)
    return targets


def test_pure_domain_modules_never_import_driving_ports_or_gcp_adapter() -> None:
    violations: list[str] = []
    for mod_name in PURE_DOMAIN_MODULES:
        mod_path = CAPACITY_DIR / mod_name
        assert mod_path.is_file(), f"Expected pure domain module {mod_name}"
        for target in _imported_targets(mod_path.read_text(encoding="utf-8")):
            if any(
                target == pfx or target.startswith(f"{pfx}.")
                for pfx in FORBIDDEN_DRIVING_OR_CLOUD_PREFIXES
            ):
                violations.append(f"capacity/{mod_name} imports forbidden module '{target}'")
    assert not violations, "\n".join(violations)


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
