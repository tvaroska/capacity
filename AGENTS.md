# AGENTS.md — Capacity (`<HOME>/projects/capacity`)

Canonical operating spine for **Capacity** (`capacity`, `v0.5.0`) — an enterprise AI capacity planner, sub-second micro-burst & model-tier prefix-cache simulator, multi-gateway configuration generator (`LiteLLM`, `Envoy AI Gateway`, `GKE Inference Gateway epp`), Newsvendor hybrid commitment optimizer, 3-Tier Residual Capacity Scheduler, and FinOps FOCUS 1.2 Two-Pool Chargeback Showcase across **Managed Gemini Platform GSUs** and **Self-Hosted GPU/TPU Replicas**.

Read [`CRITICAL.md`](CRITICAL.md) for hard invariants, [`DECISIONS.md`](DECISIONS.md) for Architectural Decision Records (`ADR-001`–`ADR-008`), [`.stitch/DESIGN.md`](.stitch/DESIGN.md) for the GM3 Light Tier-2 Showcase visual contract, and [`docs/roadmap.md`](docs/roadmap.md) for the product roadmap.

## 1. Repository Map

| Path | Responsibility |
| :--- | :--- |
| [`capacity/contract/`](capacity/contract/) | Strict (`extra="forbid"`), finite-bounded Pydantic v2 wire contracts across [`common.py`](capacity/contract/common.py) (`SeriesKey`, `UnknownModelError`, `PACKAGE_VERSION`), [`requests.py`](capacity/contract/requests.py), [`results.py`](capacity/contract/results.py), and [`scenario.py`](capacity/contract/scenario.py) (`ShowcaseScenario` & `ShowcaseReport`). |
| [`capacity/engine/`](capacity/engine/) | Pure-Python domain calculations importing only `capacity.contract`: [`stats.py`](capacity/engine/stats.py) (percentiles, `argmin_convex_int`, `holts_linear_trend`), [`catalog.py`](capacity/engine/catalog.py) (unified 1P Gemini + 3P Claude `ModelCatalog`), [`sizing.py`](capacity/engine/sizing.py), [`burst.py`](capacity/engine/burst.py), [`self_hosted.py`](capacity/engine/self_hosted.py), [`commitment.py`](capacity/engine/commitment.py), [`chargeback.py`](capacity/engine/chargeback.py), [`scheduler.py`](capacity/engine/scheduler.py), [`pooling.py`](capacity/engine/pooling.py), and [`kpis.py`](capacity/engine/kpis.py) (`ADR-001`–`ADR-003`, `ADR-006`). |
| [`capacity/ingest/`](capacity/ingest/) | Multi-scrape Prometheus counter-rate/reset parser ([`prometheus.py`](capacity/ingest/prometheus.py)), nested OTel `gen_ai.usage.*` & `Observability 2.0` `inference_request` JSONL parser ([`request_logs.py`](capacity/ingest/request_logs.py)), and JSON trace-file helpers ([`trace_files.py`](capacity/ingest/trace_files.py)) (`ADR-006`). |
| [`capacity/telemetry/`](capacity/telemetry/) | Telemetry **Ports, Adapters and Contracts**: [`port.py`](capacity/telemetry/port.py) (`TelemetryPort` + `recommend_capacity`), [`local.py`](capacity/telemetry/local.py) (`LocalTraceAdapter` + deterministic `--demo` generator), and [`cloud_monitoring.py`](capacity/telemetry/cloud_monitoring.py) (the sole module importing `google.*` Cloud SDKs; `ADR-001`, `ADR-005`). |
| [`capacity/gateway/`](capacity/gateway/) | Safe YAML multi-gateway generators and scale auditors across [`litellm.py`](capacity/gateway/litellm.py), [`litellm_burndown_callback.py`](capacity/gateway/litellm_burndown_callback.py), [`envoy.py`](capacity/gateway/envoy.py), [`gke.py`](capacity/gateway/gke.py), [`header_policies.py`](capacity/gateway/header_policies.py), and [`bundle.py`](capacity/gateway/bundle.py) (`ADR-007`). |
| [`capacity/showcase/`](capacity/showcase/) | Scenario evaluator & `--strict-public-check` gate ([`evaluate.py`](capacity/showcase/evaluate.py)), canonical sample datasets ([`sample_data.py`](capacity/showcase/sample_data.py)), and pure zero-calculation GM3 Light HTML workbench renderer ([`renderer.py`](capacity/showcase/renderer.py), [`sections.py`](capacity/showcase/sections.py), [`template.py`](capacity/showcase/template.py), [`scripts.py`](capacity/showcase/scripts.py)) (`ADR-004`). |
| [`capacity/cli/`](capacity/cli/) & [`capacity/api.py`](capacity/api.py) | Driving ports: one CLI module per command (`demo`, `recommend`, `optimize`, `gateway`, `schedule`, `chargeback`, `collect`, `serve`), [`formatters.py`](capacity/cli/formatters.py), and FastAPI REST service (`ADR-004`). |
| [`examples/`](examples/) & [`skills/capacity/`](skills/capacity/SKILL.md) | Sample diurnal traces, multi-tenant scenario fixture, BigQuery `inference_request` SQL export, Prometheus/OTel logs, and portable agent `SKILL.md`. |
| [`benchmarks/`](benchmarks/) | Open-loop `Level 3` benchmark (`10,000` Azure Public LLM Inference trace requests + `1,500` live loopback HTTP/1.1 SSE gateway streams + SHA-256 `l3_receipt.json`) (`ADR-008`). |
| [`specs/ast.rules.yaml`](specs/ast.rules.yaml) & [`tests/`](tests/) | Per-folder declarative Tree-sitter AST rules plus unit, `L2` physics oracle, `@pytest.mark.port` parity, verbatim doc contract, request contract, telemetry ingestion, `CRITICAL.md` invariant, `L3` receipt, and `ADR-005` hygiene suites. |
| [`cloudbuild.yaml`](cloudbuild.yaml) | GCP Cloud Build CI/CD pipeline (`ADR-005`). |

## 2. Canonical Verification Gate

Always run the canonical prefix-matchable test runner from the parent workspace (which delegates to [`scripts/gate.sh`](scripts/gate.sh)):

```bash
<HOME>/projects/bin/proj-test capacity
```

Or run the exact same gate script directly inside the repository (mirrored by [`cloudbuild.yaml`](cloudbuild.yaml)):

```bash
./scripts/gate.sh
```

> **Gotchas:** `scripts/gate.sh` runs `uv lock --check --offline --no-config`, `ast-grep`, `ruff check`, `ruff format --check`, `mypy`, and `pytest` (including the deterministic Track-A receipt replay) once each. Comment or formatting edits no longer invalidate [`benchmarks/results/l3_receipt.json`](benchmarks/results/l3_receipt.json); when cutting a release after intentional physics/benchmark changes, run `uv run --no-sync python -m benchmarks.run_l3_benchmark --live`, verify with `--offline --check-source-hash`, and refresh the `receipt_sha256` prefix and any changed figures in [`README.md`](README.md). Note that [`tests/test_repo_hygiene.py`](tests/test_repo_hygiene.py) walks the repository directory tree directly (`rglob`), so untracked non-hidden files are scanned automatically before `git add`.
