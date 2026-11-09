# AGENTS.md — Capacity (`<HOME>/projects/capacity`)

Canonical operating spine for **Capacity** (`capacity`, `v0.3.0`) — an enterprise AI capacity planner, sub-second micro-burst & model-tier prefix-cache simulator, multi-gateway configuration generator (`LiteLLM`, `Envoy AI Gateway`, `GKE Inference Gateway epp`), Newsvendor hybrid commitment optimizer, 3-Tier Residual Capacity Scheduler, and FinOps FOCUS 1.2 Two-Pool Chargeback Showcase across **Managed Gemini Platform GSUs** and **Self-Hosted GPU/TPU Replicas**.

Read [`CRITICAL.md`](CRITICAL.md) for hard invariants, [`DECISIONS.md`](DECISIONS.md) for Architectural Decision Records (`ADR-001`–`ADR-007`), [`.stitch/DESIGN.md`](.stitch/DESIGN.md) for the GM3 Light Tier-2 Showcase visual contract, and [`docs/roadmap.md`](docs/roadmap.md) for the product roadmap.

## 1. Repository Map

| Path | Responsibility |
| :--- | :--- |
| [`capacity/core.py`](capacity/core.py) | Cloud Monitoring async timeseries fetcher for both Managed GSUs (`consumed_token_throughput`) and Self-Hosted Vertex AI Endpoints (`prediction_count`, `accelerator/duty_cycle`, `vertex_vllm_*`), multi-project/folder roll-up, and pure-Python Holt's Double Exponential Smoothing (`holts_linear_trend`). |
| [`capacity/capacity.py`](capacity/capacity.py) | Model ID normalization, built-in GSU (`gemini-3.8-flash`, `gemini-3.8-flash-fast`, `gemini-4-argon`, `gemini-3.5-flash-lite`, `gemini-embedding-2`) & Self-Hosted replica throughput tables, and dual-mode (`all_hours` 24x7 vs. `active_hours` business-hours) percentile + Holt forecast computation (`ADR-001`). |
| [`capacity/burst.py`](capacity/burst.py) | Sub-second token-bucket micro-burst simulator (`1s` vs. `10s` windows, `5%` burst credit), `>200K` long-context burndown cliff (`2.0x` input / `7.5x` output), round-robin $1/N$ prefix-cache dilution, and model-tier prefix-cache block alignment (`4,096`-token Block 0 contamination vs. token-boundary checkpointing, Turn-0 dummy-turn anchor, and `thought_signature` stripping) (`ADR-002`). |
| [`capacity/self_hosted.py`](capacity/self_hosted.py) | Self-hosted GPU/TPU endpoint auditor (upfront `max_replica_count` quota locks, continuous-batching Duty-Cycle vs. KV-cache saturation illusion, Newsvendor replica sizing, `ScaleToZeroSpec` savings), Disaggregated Prefill/Decode GPU pool sizer (`size_disaggregated_prefill_decode`), and Phase 2 vLLM/SGLang Prometheus & OTel JSONL log parser (`ADR-006`). |
| [`capacity/gateway.py`](capacity/gateway.py) | Multi-gateway declarative config generator & scale auditor for **LiteLLM Proxy** (`litellm_config.yaml`, `litellm_burndown_callback.py`, Prisma/Postgres/SSE scale audit), **Envoy AI Gateway** (`AIGatewayRoute` CEL + `BackendTrafficPolicy`), and **GKE Inference Gateway** (`InferencePool` + `EndpointPickerConfig` `epp`), plus the 8 Vertex AI header routing policies (`ADR-007`). |
| [`capacity/finops.py`](capacity/finops.py) | Newsvendor quantile hybrid commitment optimizer, Batch API (`0.5x`) double-payment trap vs. `$0` online PT scavenging, 3-Tier Residual Capacity Scheduler (`Revenue > Hard-Deadline EDF > Evergreen Water-Fill`), Shared Folder RHPT pooling vs. siloed stranding, FOCUS 1.2 Two-Pool Chargeback, and the 5 FinOps for AI KPIs (`CPWT_1M`, `CPVO`, `TER`, `CSR`, `ECUR_168h`) (`ADR-003`, `ADR-007`). |
| [`capacity/portal.py`](capacity/portal.py) | Interactive GM3 Light 4-Tab Web Calculator (`GET /` on `capacity serve`) and self-contained static HTML executive scorecard (`capacity demo --html-out`) (`ADR-004`). |
| [`capacity/cli.py`](capacity/cli.py) | Typer CLI (`capacity demo`, `capacity recommend`, `capacity optimize`, `capacity gateway`, `capacity schedule`, `capacity chargeback`, `capacity serve`). |
| [`capacity/api.py`](capacity/api.py) | FastAPI REST service (`GET /`, `/healthz`, `/api/v1/recommendations`, `/api/v1/optimize`, `/api/v1/prefix-cache/model-tier`, `/api/v1/self-hosted/audit`, `/api/v1/self-hosted/disaggregated`, `/api/v1/vllm/parse`, `/api/v1/gateway/config`, `/api/v1/chargeback`, `/api/v1/finops/scavenger`, `/api/v1/finops/schedule`, `/api/v1/finops/folder-pooling`, `/api/v1/finops/kpis`) (`ADR-004`). |
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
