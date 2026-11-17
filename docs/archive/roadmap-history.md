# `capacity` — Roadmap Archive

Implemented milestones and completed passes moved from [`../roadmap.md`](../roadmap.md).

## v0.2.0-v0.3.0 (Passes A-G + 3P Claude)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-17.*

### 1. Two-Phase Architecture

| Phase | Target Workloads | Telemetry & Log Sources | Core Capabilities & Deliverables | Status |
| :--- | :--- | :--- | :--- | :---: |
| **Phase 1 (`v0.2.0`)** | **1A. Managed Gemini Platform (`--mode gsu`)**<br>**1B. Self-Hosted Models on Vertex AI Prediction (`--mode self-hosted`)** | • `aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput` (`dedicated` + `shared`)<br>• `aiplatform.googleapis.com/prediction/online/prediction_count`<br>• Offline JSON traces & `--demo` profiles | • Active-business-hours (`active_hours`) vs. 24x7 (`all_hours`) percentile sizing<br>• Multi-project & folder roll-up (`--project` repeatable + `--folder-map`)<br>• Daily-peak **Holt's Double Exponential Smoothing** (`--forecast-hours`)<br>• Conserved `1s` (`>=50` GSUs) / `10s` (`<50` GSUs) token-bucket micro-burst & `>200K` burndown cliff (`2.0x` input / `7.5x` output)<br>• Self-hosted **Upfront GPU Quota Lock**, **Duty-Cycle vs. KV-Cache Saturation Trap**, & `ScaleToZeroSpec` savings<br>• Round-robin $1/N$ prefix/KV-cache dilution vs. prefix-aware routing<br>• Duty-aware Newsvendor hybrid commitment (`PT + Priority PayGo` or `Reserved CUD + Elastic Burst`)<br>• FOCUS 1.2 Two-Pool Chargeback (Weighted Burndown vs. Raw Tokens + Active vs. Unallocated Idle + Batch Water-Fill) | **Implemented (`v0.2.0`)** |
| **Phase 2 (`v0.3.0` Showcase)** | **Multi-Gateway Control Planes (`LiteLLM`, `Envoy`, `K8s epp`), 1P Gemini + 3P Claude PT, Disaggregated GPU Pools & Empirical Verification** | • Multi-scrape Prometheus `/metrics` snapshots (`vllm:*`, `sglang:*`)<br>• OpenTelemetry `gen_ai.usage.*` & OpenAI SSE JSONL logs<br>• Cloud Monitoring `token_count` & `Observability 2.0` `inference_request` logs<br>• Microsoft Azure Public LLM Inference Trace (`10,000` requests, `CC-BY-4.0`) | • **Interactive GM3 Light 4-Tab Web Workbench (`GET /` & `capacity demo --html-out`)** governed by [`.stitch/DESIGN.md`](../../.stitch/DESIGN.md)<br>• **1P Gemini & 3P Anthropic Claude Physics (`model_families.py`, `burst.py`):** 1P `1s`/`10s` (`+5%` credit, `4,096` Block 0 / `6,144` Flash vs. `2,048` Argon/Carbon token-boundary floor, Turn-0 dummy-turn anchor, `thought_signature` stripping penalty, `-fast` decode) vs. 3P Claude `30s`/`60s` dual buckets (`0%` credit, flat `1.0x/0.1x/1.25x/2.0x/5.0x` weights, explicit `cache_control` floors)<br>• **Safe YAML Multi-Gateway Generator (`gateway.py`):** Tag-routed `litellm_config.yaml`, stateful Redis `litellm_burndown_callback.py`, Prisma/PgBouncer scale audit, Envoy AI Gateway `AIGatewayRoute` CEL + per-BU `BackendTrafficPolicy`, GKE `epp` manifests, and the 8 Vertex AI header routing policies<br>• **Disaggregated Prefill / Decode GPU Pool Sizer (`self_hosted.py`):** Apples-to-apples TTFT-bound prefill vs. TPOT/KV-bound decode pool sizing<br>• **Exact Residual Scheduler, `--scenario-file`, `capacity collect` & `Level 3` Benchmark (`finops.py`, `scenario.py`, `benchmarks/`):** Exact branch-and-bound deadline admission (`EDF`), Shared Folder RHPT pooling, 5 FinOps for AI KPIs (`CPVO` bridge to `bq-eval`), `10,000`-request Azure trace backtest vs. `L2` omniscient oracle, and `1,500`-stream live HTTP/1.1 SSE gateway verification | **Implemented (`v0.3.0` Showcase)** |

### 2. Completed Build & Hardening Passes (`v0.2.0` -> `v0.3.0`)

- [x] **Pass A — Dual-Mode Engine (`gsu` + `self-hosted`), Multi-Project/Forecast Upgrades (`v0.2.0`)**
- [x] **Pass B — Story, Architecture Card, ADRs, Interactive UI & Examples (`v0.2.0`)**
- [x] **Pass C — Verification Gate & Portable Lockfile Readiness (`v0.2.0`)**
- [x] **Pass D — Multi-Gateway, Disaggregated P/D, 5-KPI FinOps & GM3 Light Workbench (`v0.3.0`)**
- [x] **Pass E — Physics Conservation, Stateful Gateway, BYO Telemetry, Hexagonal Ports & Open-Loop Benchmark (`v0.3.0`):**
  - [x] Extracted pure math & `TelemetryPort` (`LocalTraceAdapter`) into [`capacity/forecasting.py`](../../capacity/forecasting.py), decomposed [`capacity/portal_template.py`](../../capacity/portal_template.py) from [`capacity/portal.py`](../../capacity/portal.py), and enforced 5 declarative AST contracts in [`specs/ast.rules.yaml`](../../specs/ast.rules.yaml) + [`tests/test_isolation_guard.py`](../../tests/test_isolation_guard.py) + `@pytest.mark.port` parity tests in [`tests/test_port_parity.py`](../../tests/test_port_parity.py).
  - [x] Unified apples-to-apples Disaggregated vs. Monolithic prefill scaling, unified 8-hour residual schedule defaults, upgraded Tier-1 admission to exact branch-and-bound value maximization, and enforced verbatim `README.md` stdout parity in [`tests/test_documentation_contract.py`](../../tests/test_documentation_contract.py).
  - [x] Hardened `BurndownAndTrafficTypeLogger` with server-side Redis rolling minute counters (`1,024`-token pre-SSE floor + `cached_tokens` and `reasoning_tokens`), RFC-1035 K8s DNS labels, `yaml.safe_dump` serialization, multi-scrape Prometheus counter-reset rate parsing, nested OTel `gen_ai.usage.*` ingestion, and bounded finite API request validation.
  - [x] Added [`benchmarks/run_l3_benchmark.py`](../../benchmarks/run_l3_benchmark.py), [`benchmarks/corpora/azure_llm_trace_10k.jsonl.gz`](../../benchmarks/corpora/azure_llm_trace_10k.jsonl.gz), [`benchmarks/results/l3_receipt.json`](../../benchmarks/results/l3_receipt.json), and [`tests/test_l3_receipt.py`](../../tests/test_l3_receipt.py).
- [x] **Pass E.1 — 3P Anthropic Claude Provisioned Throughput Mode (`v0.3.0`):** Added [`capacity/model_families.py`](../../capacity/model_families.py) (`ModelFamilySpec` separating 1P Gemini `1s`/`10s` `+5%` credit from 3P Claude `30s`/`60s` dual buckets with `0%` credit and flat `1.0x/0.1x/1.25x/2.0x/5.0x` burndown), `CLAUDE_GSU_CONFIG`, client-side weighted `token_count` Cloud Monitoring ingestion in [`capacity/core.py`](../../capacity/core.py), Anthropic cache-write log parsing in [`capacity/telemetry_parsers.py`](../../capacity/telemetry_parsers.py), `standard` PayGo spillover + Claude CEL/callback generation in [`capacity/gateway.py`](../../capacity/gateway.py), and 1P-only Folder RHPT hub-project warnings in [`capacity/finops.py`](../../capacity/finops.py).
- [x] **Pass F — Request Contract, Ingestion Accuracy & Invariant Tests (`v0.3.0`):** strict request models shared by the CLI and the API, whole-day live windows and zero-filled telemetry, exact commitment search at any size, default-deny package isolation, and exact-value tests for the `CRITICAL.md` and `ADR-006` rules that used to pass when inverted.
- [x] **Pass G — Parameterized Scenario Contract (`--scenario-file`), Dual Collector (`capacity collect`), `gemini-4-carbon` Lifecycle Rules & Bundled Agent Skill (`v0.3.0`):**
  - [x] Added [`capacity/scenario.py`](../../capacity/scenario.py) (`ShowcaseScenario`, `load_showcase_scenario`, `audit_scenario_public_safety` `--strict-public-check`, `load_orders_overlay`, and `compile_scenario_from_telemetry`), [`examples/scenarios/multi_tenant_enterprise.json`](../../examples/scenarios/multi_tenant_enterprise.json), [`examples/orders_overlay.yaml`](../../examples/orders_overlay.yaml), [`examples/bigquery_inference_request_export.sql`](../../examples/bigquery_inference_request_export.sql), and [`skills/capacity/SKILL.md`](../../skills/capacity/SKILL.md).
  - [x] Registered `gemini-4-carbon` (`500` TPS/GSU, `2,048` token-boundary floor, Short-Term GA guidance), enforced `50`-GSU (`10s -> 1s`) zero-spillover step continuity in [`capacity/burst.py`](../../capacity/burst.py), added dynamic `Forecast+{hours}h` table headers in [`capacity/cli_formatters.py`](../../capacity/cli_formatters.py), extended [`capacity/telemetry_parsers.py`](../../capacity/telemetry_parsers.py) for `Observability 2.0` (`aiplatform.googleapis.com/inference_request`) JSONL logs + sub-second burst peaks, and added 3-state `--check-auth` + `dedicated_token_limit`/`model_invocation_count` inventory collection in [`capacity/core.py`](../../capacity/core.py).

## v0.3.1 (Milestone 1 - P0)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-17.*

- [x] **P0.1 The benchmark is checked on behaviour, not on file bytes**
  - Today a test compares a hash of six engine files, so a comment edit forces a live benchmark run and hand edits to the README.
  - Keep the replay of the committed trace as the check on every run. Move the hash comparison to a release-time check that is off by default. Record the commit id in the receipt. Add tests that a damaged receipt and a drifted replay are rejected. Amend `ADR-008`.
  - Files: `benchmarks/run_l3_benchmark.py`, `tests/test_l3_receipt.py`, `DECISIONS.md`, `AGENTS.md`.
  - Done when: a comment edit in `capacity/burst.py` leaves the suite green, and a changed burndown weight still fails it.
- [x] **P0.2 The architecture guards follow a move into sub-folders**
  - Both guards assume every module sits directly in `capacity/`. After a move, moved modules are no longer scanned.
  - Scan sub-folders. Name the adapter files once, by folder where possible. Resolve relative imports from the file's real package. Assert that every file is classified exactly once. Add the new import spellings to the guard's own examples.
  - Files: `tests/test_isolation_guard.py`, `specs/ast.rules.yaml`.
  - Done when: a probe module in a sub-folder that imports the Cloud Monitoring adapter fails both guards.
- [x] **P0.3 Shared test fixtures**
  - Add `tests/conftest.py` with one fake Cloud Monitoring client that owns the patch target (repeated in 9 places today), one time-series builder, one cache fake and one loader for the generated gateway callback (it is loaded three different ways today).
  - Remove the second leak scanner in `tests/test_capacity.py`. Test through public functions where a private name is imported today, and take the private name out of `__all__` in `capacity/core.py`.
  - Done when: the patch target string appears once in `tests/`.
- [x] **P0.4 Tests where the refactor will cut**
  - Cover what has no test today: Cloud Monitoring errors (missing credentials, permission denied, API error) through to exit code 1 on the command line and HTTP 502 on the API; `--folder-map` parsing; trace files shaped as a mapping; the `--format json` branches; scenario compilation from live series; rejection of NaN and infinity; `recommend --html-out`; the `collect` paths with `--check-auth` and with a live project; the lifecycle class of a model id.
  - Files: `tests/`, exercising `capacity/core.py`, `capacity/cli.py`, `capacity/cli_formatters.py` and `capacity/scenario.py`.
- [x] **P0.5 One definition of the gate**
  - Put the gate in one script in the repository and call it from `AGENTS.md`, the workspace runner and `cloudbuild.yaml`. Run the `ast-grep` scan, the format check and the benchmark replay once, not twice.
  - Correct the two outdated notes in `AGENTS.md`: the lock check does run offline, and the hygiene test walks the directory.
  - Done when: the three places run the same script.
