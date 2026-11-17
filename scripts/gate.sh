#!/usr/bin/env bash
# Canonical verification gate for capacity (shared by AGENTS.md, proj-test, and cloudbuild.yaml).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONDONTWRITEBYTECODE=1

uv lock --check --offline --no-config
uv run --no-sync ast-grep scan --rule specs/ast.rules.yaml
uv run --no-sync ruff check --no-cache capacity tests benchmarks
uv run --no-sync ruff format --check --no-cache capacity tests benchmarks
uv run --no-sync mypy --no-incremental --cache-dir=/tmp/top3-mypy-capacity capacity tests benchmarks
exec uv run --no-sync pytest -p no:cacheprovider "$@"
