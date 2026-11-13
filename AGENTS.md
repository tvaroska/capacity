# AGENTS.md — Capacity (`<HOME>/projects/capacity`)

Canonical operating spine for **Capacity** (`capacity`, `v0.3.0`) — an enterprise AI capacity planner, sub-second micro-burst & model-tier prefix-cache simulator, multi-gateway configuration generator (`LiteLLM`, `Envoy AI Gateway`, `GKE Inference Gateway epp`), Newsvendor hybrid commitment optimizer, 3-Tier Residual Capacity Scheduler, and FinOps FOCUS 1.2 Two-Pool Chargeback Showcase across **Managed Gemini Platform GSUs** and **Self-Hosted GPU/TPU Replicas**.

Read [`CRITICAL.md`](CRITICAL.md) for hard invariants, [`DECISIONS.md`](DECISIONS.md) for Architectural Decision Records (`ADR-001`–`ADR-008`), [`.stitch/DESIGN.md`](.stitch/DESIGN.md) for the GM3 Light Tier-2 Showcase visual contract, and [`docs/roadmap.md`](docs/roadmap.md) for the product roadmap.

## 1. Repository Map

| Path | Responsibility |
| :--- | :--- |
| [`capacity/forecasting.py`](capacity/forecasting.py) | Pure-Python percentile interpolation, `holts_linear_trend`, exact convex integer search (`argmin_convex_int`), deterministic `--demo` sparse trace generator, `UnknownModelError`, and Hexagonal `TelemetryPort` + `LocalTraceAdapter`. |
| [`capacity/model_families.py`](capacity/model_families.py) | Model ID normalisation (`normalize_model_id`) and the pure-domain Provisioned Throughput physics registry (`ModelFamilySpec`) separating 1P Gemini (`1s`/`10s`, `+5%` credit, `>200K` cliff, Folder RHPT) from 3P Anthropic Claude (`30s`/`60s` dual bucket, `0%` credit, `1x/0.1x/1.25x/2x/5x` flat burndown, `/token_count` metric, hub-project BU pooling). |
| [`capacity/core.py`](capacity/core.py) | Cloud Monitoring driven adapter (`CloudMonitoringAdapter` / `fetch_throughput`) for 1P Managed GSUs (`consumed_token_throughput`), 3P Claude PT (client-side weighted `token_count`), and Self-Hosted Vertex AI Endpoints with strict regex filter validation, whole-UTC-day query windows and points keyed by the minute they cover (`ADR-001`, `ADR-005`). |
| [`capacity/capacity.py`](capacity/capacity.py) | Built-in 1P Gemini + 3P Anthropic Claude GSU & Self-Hosted throughput tables with exact-match lookup, `all_hours` vs. `active_hours` percentile + daily-peak Holt sizing, and the two entry points both ports share: `recommend_capacity` (through a `TelemetryPort`) and `evaluate_gsu_optimization` (`ADR-001`). |
| [`capacity/burst.py`](capacity/burst.py) | Conserved sub-second token-bucket micro-burst simulator (1P `1s`/`10s` with `5%` burst credit vs. 3P Claude `30s`/`60s` dual bucket with `0%` credit), 1P `>200K` long-context cliff (`2.0x` / `7.5x`) vs. 3P flat `1.0x/0.1x/1.25x/2.0x/5.0x` burndown, RPS-scaled $1/N$ prefix-cache dilution, and model-tier Block-0 / Turn-0 / `thought_signature` / explicit `cache_control` auditing (`ADR-002`). |
| [`capacity/self_hosted.py`](capacity/self_hosted.py) & [`telemetry_parsers.py`](capacity/telemetry_parsers.py) | Self-hosted GPU/TPU endpoint auditor, apples-to-apples Disaggregated Prefill/Decode sizer (`size_disaggregated_prefill_decode`), and multi-scrape Prometheus + nested OTel JSONL parsers (`ADR-006`). |
| [`capacity/gateway.py`](capacity/gateway.py) & [`litellm_burndown_callback.py`](capacity/litellm_burndown_callback.py) | Safe YAML multi-gateway config generator & scale auditor for **LiteLLM Proxy** (stateful Redis `litellm_burndown_callback.py`), **Envoy AI Gateway** (`AIGatewayRoute` CEL + per-BU `BackendTrafficPolicy`), **GKE Inference Gateway** (`epp`), and the 8 Vertex AI header policies (`ADR-007`). |
| [`capacity/finops.py`](capacity/finops.py) | Duty-aware Newsvendor commitment optimizer, Batch API (`0.5x`) double-payment audit, exact branch-and-bound 3-Tier Residual Capacity Scheduler (`EDF`), Shared Folder RHPT pooling, FOCUS 1.2 Two-Pool Chargeback, and the 5 FinOps for AI KPIs (`ADR-003`). |
| [`capacity/portal.py`](capacity/portal.py), [`portal_template.py`](capacity/portal_template.py) & [`portal_scripts.py`](capacity/portal_scripts.py) | Interactive GM3 Light 4-Tab Web Workbench (`GET /` on `capacity serve`) and offline static HTML scorecard (`--html-out`, `--scenario-file`, `static_export=True`) (`ADR-004`). |
| [`capacity/scenario.py`](capacity/scenario.py) | Strict (`extra="forbid"`) `ShowcaseScenario` schema (`--scenario-file`), `--strict-public-check` redaction gate, commercial order overlay loader, and customer telemetry-to-scenario compiler (`ADR-004`, `ADR-005`). |
| [`capacity/cli.py`](capacity/cli.py), [`cli_formatters.py`](capacity/cli_formatters.py) & [`capacity/api.py`](capacity/api.py) | Driving ports: Typer CLI (`demo|collect|recommend|optimize|gateway|schedule|chargeback|serve`), table formatters, and FastAPI REST service (`ADR-004`). |
| [`capacity/models.py`](capacity/models.py) | Strict (`extra="forbid"`), finite-bounded Pydantic v2 request and response schemas that the CLI and the API both validate with, plus canonical overnight schedule defaults. |
| [`examples/`](examples/) & [`skills/capacity/`](skills/capacity/SKILL.md) | Sample diurnal traces, multi-tenant scenario fixture, BigQuery `inference_request` SQL export, Prometheus/OTel logs, and portable agent `SKILL.md`. |
| [`benchmarks/`](benchmarks/) | Open-loop `Level 3` benchmark (`10,000` Azure Public LLM Inference trace requests + `1,500` live loopback HTTP/1.1 SSE gateway streams + SHA-256 `l3_receipt.json`) (`ADR-008`). |
| [`specs/ast.rules.yaml`](specs/ast.rules.yaml) & [`tests/`](tests/) | Declarative Tree-sitter AST rules plus unit, `L2` physics oracle, `@pytest.mark.port` parity, verbatim doc contract, request contract, telemetry ingestion, `CRITICAL.md` invariant, `L3` receipt, and `ADR-005` hygiene suites. |
| [`cloudbuild.yaml`](cloudbuild.yaml) | GCP Cloud Build CI/CD pipeline (`ADR-005`). |

## 2. Canonical Verification Gate

Always run the canonical prefix-matchable test runner from the parent workspace:

```bash
<HOME>/projects/bin/proj-test capacity
```

Or run `ast-grep`, `ruff`, `mypy`, `pytest`, and the offline `Level 3` receipt verifier directly inside the repository:

```bash
uv lock --check
uv run --no-sync ast-grep scan --rule specs/ast.rules.yaml
uv run --no-sync ruff check --no-cache capacity tests benchmarks
uv run --no-sync ruff format --check --no-cache capacity tests benchmarks
uv run --no-sync mypy capacity tests benchmarks
uv run --no-sync pytest -v
uv run --no-sync python -m benchmarks.run_l3_benchmark --offline
```

> **Gotchas:** Use `uv lock --check` (never `--offline`, which fails split Python-marker resolution). Always run `ruff format` **before** `uv run --no-sync python -m benchmarks.run_l3_benchmark --live` so formatting does not change the SHA-256 of the 6 hashed engine modules (`burst.py`, `capacity.py`, `finops.py`, `forecasting.py`, `gateway.py`, `self_hosted.py`), then copy the new `receipt_sha256` prefix and any changed Track B figure into `README.md`. Run `git add -N` on new files before `proj-test capacity` so `tests/test_repo_hygiene.py` (`git ls-files`) scans them prior to commit.
