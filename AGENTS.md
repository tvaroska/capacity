# AGENTS.md — Capacity (`<HOME>/projects/capacity`)

Canonical operating spine for **Capacity** (`capacity`, `v0.2.0`) — an enterprise AI capacity planner, sub-second micro-burst & KV-cache simulator, Newsvendor hybrid commitment optimizer, and FinOps FOCUS 1.2 Two-Pool Chargeback engine across **Managed Gemini Platform GSUs** and **Self-Hosted GPU/TPU Replicas**.

Read [`CRITICAL.md`](CRITICAL.md) for hard invariants, [`DECISIONS.md`](DECISIONS.md) for Architectural Decision Records (`ADR-001`–`ADR-006`), and [`docs/roadmap.md`](docs/roadmap.md) for the Phase 1 / Phase 2 roadmap.

## 1. Repository Map

| Path | Responsibility |
| :--- | :--- |
| [`capacity/core.py`](capacity/core.py) | Cloud Monitoring async timeseries fetcher for both Managed GSUs (`consumed_token_throughput`) and Self-Hosted Vertex AI Endpoints (`prediction_count`, `accelerator/duty_cycle`, `vertex_vllm_*`), multi-project/folder roll-up, and pure-Python Holt's Double Exponential Smoothing (`holts_linear_trend`). |
| [`capacity/capacity.py`](capacity/capacity.py) | Model ID normalization, built-in GSU & Self-Hosted replica throughput tables, and dual-mode (`all_hours` 24x7 vs. `active_hours` business-hours) percentile + Holt forecast computation (`ADR-001`). |
| [`capacity/burst.py`](capacity/burst.py) | Sub-second token-bucket micro-burst simulator (`1s` vs. `10s` windows, `5%` burst credit), `>200K` long-context burndown cliff (`2.0x` input / `7.5x` output), and round-robin $1/N$ prefix-cache dilution (`ADR-002`). |
| [`capacity/self_hosted.py`](capacity/self_hosted.py) | Self-hosted GPU/TPU endpoint auditor (upfront `max_replica_count` quota locks, continuous-batching Duty-Cycle vs. KV-cache saturation illusion, Newsvendor replica sizing, `ScaleToZeroSpec` savings) + Phase 2 vLLM/SGLang Prometheus & JSONL log parser (`ADR-006`). |
| [`capacity/finops.py`](capacity/finops.py) | Newsvendor quantile hybrid commitment optimizer (`PT + Priority PayGo` or `Reserved CUD + Elastic Burst`) and FinOps FOCUS 1.2 Two-Pool Chargeback with raw-vs-weighted token attribution and asynchronous batch water-filling (`ADR-003`). |
| [`capacity/cli.py`](capacity/cli.py) | Typer CLI (`capacity recommend`, `capacity optimize`, `capacity chargeback`, `capacity serve`) supporting `--mode gsu|self-hosted`, `--demo`, `--trace-file`, and `--vllm-file`. |
| [`capacity/api.py`](capacity/api.py) | FastAPI service (`GET /`, `/healthz`, `/api/v1/recommendations`, `/api/v1/optimize`, `/api/v1/self-hosted/audit`, `/api/v1/vllm/parse`, `/api/v1/chargeback`) (`ADR-004`). |
| [`capacity/models.py`](capacity/models.py) | Pydantic v2 request/response schemas. |
| [`examples/`](examples/) | Sample diurnal traces, multi-tenant chargeback profiles, and Phase 2 vLLM Prometheus/JSONL snapshots. |
| [`tests/`](tests/) | Unit, CLI, FastAPI, documentation contract, and `ADR-005` repository hygiene test suites. |
| [`cloudbuild.yaml`](cloudbuild.yaml) | GCP Cloud Build CI/CD pipeline (`ADR-005`). |

## 2. Canonical Verification Gate

Always run the canonical prefix-matchable test runner from the parent workspace:

```bash
<HOME>/projects/bin/proj-test capacity
```

Or run `ruff`, `mypy`, and `pytest` directly inside the repository:

```bash
uv run --no-sync ruff check --no-cache capacity tests
uv run --no-sync pytest -v
```
