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
  - [x] Extracted pure math & `TelemetryPort` (`LocalTraceAdapter`) into `capacity/forecasting.py`, decomposed `capacity/portal_template.py` from `capacity/portal.py`, and enforced 5 declarative AST contracts in [`specs/ast.rules.yaml`](../../specs/ast.rules.yaml) + [`tests/test_isolation_guard.py`](../../tests/test_isolation_guard.py) + `@pytest.mark.port` parity tests in [`tests/test_port_parity.py`](../../tests/test_port_parity.py).
  - [x] Unified apples-to-apples Disaggregated vs. Monolithic prefill scaling, unified 8-hour residual schedule defaults, upgraded Tier-1 admission to exact branch-and-bound value maximization, and enforced verbatim `README.md` stdout parity in [`tests/test_documentation_contract.py`](../../tests/test_documentation_contract.py).
  - [x] Hardened `BurndownAndTrafficTypeLogger` with server-side Redis rolling minute counters (`1,024`-token pre-SSE floor + `cached_tokens` and `reasoning_tokens`), RFC-1035 K8s DNS labels, `yaml.safe_dump` serialization, multi-scrape Prometheus counter-reset rate parsing, nested OTel `gen_ai.usage.*` ingestion, and bounded finite API request validation.
  - [x] Added [`benchmarks/run_l3_benchmark.py`](../../benchmarks/run_l3_benchmark.py), [`benchmarks/corpora/azure_llm_trace_10k.jsonl.gz`](../../benchmarks/corpora/azure_llm_trace_10k.jsonl.gz), [`benchmarks/results/l3_receipt.json`](../../benchmarks/results/l3_receipt.json), and [`tests/test_l3_receipt.py`](../../tests/test_l3_receipt.py).
- [x] **Pass E.1 — 3P Anthropic Claude Provisioned Throughput Mode (`v0.3.0`):** Added `capacity/model_families.py` (`ModelFamilySpec` separating 1P Gemini `1s`/`10s` `+5%` credit from 3P Claude `30s`/`60s` dual buckets with `0%` credit and flat `1.0x/0.1x/1.25x/2.0x/5.0x` burndown), `CLAUDE_GSU_CONFIG`, client-side weighted `token_count` Cloud Monitoring ingestion in `capacity/core.py`, Anthropic cache-write log parsing in `capacity/telemetry_parsers.py`, `standard` PayGo spillover + Claude CEL/callback generation in `capacity/gateway.py`, and 1P-only Folder RHPT hub-project warnings in `capacity/finops.py`.
- [x] **Pass F — Request Contract, Ingestion Accuracy & Invariant Tests (`v0.3.0`):** strict request models shared by the CLI and the API, whole-day live windows and zero-filled telemetry, exact commitment search at any size, default-deny package isolation, and exact-value tests for the `CRITICAL.md` and `ADR-006` rules that used to pass when inverted.
- [x] **Pass G — Parameterized Scenario Contract (`--scenario-file`), Dual Collector (`capacity collect`), `gemini-4-carbon` Lifecycle Rules & Bundled Agent Skill (`v0.3.0`):**
  - [x] Added `capacity/scenario.py` (`ShowcaseScenario`, `load_showcase_scenario`, `audit_scenario_public_safety` `--strict-public-check`, `load_orders_overlay`, and `compile_scenario_from_telemetry`), [`examples/scenarios/multi_tenant_enterprise.json`](../../examples/scenarios/multi_tenant_enterprise.json), [`examples/orders_overlay.yaml`](../../examples/orders_overlay.yaml), [`examples/bigquery_inference_request_export.sql`](../../examples/bigquery_inference_request_export.sql), and [`skills/capacity/SKILL.md`](../../skills/capacity/SKILL.md).
  - [x] Registered `gemini-4-carbon` (`500` TPS/GSU, `2,048` token-boundary floor, Short-Term GA guidance), enforced `50`-GSU (`10s -> 1s`) zero-spillover step continuity in `capacity/burst.py`, added dynamic `Forecast+{hours}h` table headers in `capacity/cli_formatters.py`, extended `capacity/telemetry_parsers.py` for `Observability 2.0` (`aiplatform.googleapis.com/inference_request`) JSONL logs + sub-second burst peaks, and added 3-state `--check-auth` + `dedicated_token_limit`/`model_invocation_count` inventory collection in `capacity/core.py`.

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

## v0.4.0 (Milestone 2 - P1): Tidy in place, then one path from request to result

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-17.*

Part A (`P1.1` to `P1.7`) fixes 11 of the 14 defects without new structure. Part B (`P1.8` to `P1.11`) gives every operation one shape and fixes two more (`P1.8`, `P1.9`). The last defect, the browser script replacing the page's numbers, goes with `P3.2`. Command-line text and the README blocks stay identical except where a decision above changes them.

- [x] **P1.1 The page shows the data it was given**
  - `recommend --html-out` renders the sizing tab only, with no sample data. Its duty-cycle card comes from the data; today it always reads 23.8%.
  - On a scenario page the gateway cards and inputs, the sizer inputs and badge, and the cache badge, banner and inputs come from the computed result. Today they keep the values of the built-in demo, and the sizer badge stays green when the saving it reports is negative.
  - With a capacity override every tab uses the same capacity for a model.
  - An unknown capacity shows "?" in every cell. The forecast column names the real horizon, and the response carries the horizon and the requested percentiles.
  - The browser's cache badge follows the engine's rule for models that cache at token boundaries. Today it can read "healthy, 0.0% hit".
  - Files: `capacity/portal.py`, `capacity/portal_sections.py`, `capacity/portal_template.py`, `capacity/portal_scripts.py`, `capacity/models.py`.
  - Done when: one render test per case asserts engine values, not phrases.
- [x] **P1.2 Trace input has one clock and one window rule**
  - A plain list of minutes is turned into a dated series in four places with two start hours. The same list forecasts 467,000 through `--trace-file` and 400,000 through a scenario file. A trace file shaped as a mapping can report a duty cycle above 100%.
  - Round fractional TPM consistently in `--trace-file` (`round(float(v))` instead of `int(float(v))` truncation in `capacity/cli_formatters.py`) to match `--vllm-file` and scenario series.
  - Files: `capacity/forecasting.py`, `capacity/cli.py`, `capacity/cli_formatters.py`, `capacity/portal.py`.
  - Done when: one list gives one forecast through every entry point, fractional TPM rounds the same way everywhere, and a duty cycle never exceeds 100%.
- [x] **P1.3 Small fixes**
  - `optimize --mode self-hosted` works without `--model`. Today the default is a managed model id.
  - Scenario compilation fails on an unknown model and no longer assumes a default model and capacity (`capacity/scenario.py`).
  - The pooling mode `silo_stranding_audit` either changes the page or is removed. An annotation `note` is either shown or removed.
  - The test for leftover placeholders also matches names with digits (`tests/test_portal_export.py`).
- [x] **P1.4 The built-in demo is the default scenario**
  - The page builder has a second branch that repeats the default scenario as literals and has drifted in one block. Delete it and build the demo from a default scenario.
  - Keep the sample data (trace, tenants and their weights, jobs, projects) in one module. The self-hosted sample follows the request default: 6 replicas, 48 GPUs.
  - Files: `capacity/portal.py`, `capacity/scenario.py`, `capacity/cli.py`, `capacity/finops.py`, `capacity/forecasting.py`.
  - Done when: `capacity demo` and a scenario file that sets only defaults give the same result.
- [x] **P1.5 Named constants and one version**
  - Name the numbers that the self-hosted sizer, the cache audit and the gateway audit assume, each with a one-line source.
  - Read the version from the package metadata. It is typed in ten lines today.
  - Files: `capacity/self_hosted.py`, `capacity/burst.py`, `capacity/gateway.py`, `capacity/finops.py`, `capacity/api.py`, `capacity/cli.py`, `capacity/portal.py`, `capacity/scenario.py`.
- [x] **P1.6 Dead code and start-up time**
  - Load the Cloud Monitoring SDK only when a command needs it. Every command pays about half a second for it today.
  - Load `.env` in the command-line entry point and at server start rather than when `capacity.cli` or `capacity.api` is imported, so an untracked local `.env` cannot change test results.
  - Delete the fallback for a missing SDK (it cannot run, because the SDK is a required dependency), the function that only forwards to another, and the re-export lists that keep old import paths alive.
  - Move the body of the `collect` command out of the formatter module into a module of its own. `capacity/cli.py` is at the 800-line cap, so one more line fails the gate. Free room in it before other `P1` tasks add to it, for example by moving the text reports of `demo`, `gateway` and `schedule` beside the other formatters.
  - Files: `capacity/core.py`, `capacity/self_hosted.py`, `capacity/cli.py`, `capacity/cli_formatters.py`, `capacity/api.py`.
  - Done when: importing the command line or the API neither loads the SDK nor reads `.env`.
- [x] **P1.7 Complexity rules on, as a ratchet**
  - Switch on the complexity lint rules that are listed as debt, with ceilings set to today's worst values. Lower the ceilings as functions are split. Remove the five entries from `tests/standards_debt.json`.
  - Files: `pyproject.toml`, `tests/standards_debt.json`.
- [x] **P1.8 One model catalog**
  - One record per model holds capacity, family rules, lifecycle and display label. Today these sit in six places and are matched by substring: an id that contains "pro" is read as the Pro tier, and an unknown id gets default weights without an error.
  - An unknown id is the same error everywhere. Overrides from the environment or a scenario produce a catalog.
  - The rule for the 1-second and the 10-second bucket lives in one function. The Envoy expression is built from the family record.
  - Files: `capacity/model_families.py`, `capacity/capacity.py`, `capacity/burst.py`, `capacity/gateway.py`, `capacity/scenario.py`, `capacity/core.py`, `capacity/portal_sections.py`, `benchmarks/trace_backtest.py`.
  - Done when: adding a model is one record, and no module matches model names by substring.
- [x] **P1.9 Every operation takes a checked request and returns a typed result**
  - Each operation is one function that takes its request model and looks the model up itself, as optimisation and the gateway bundle already do.
  - The capacity mode moves into the request, so the API accepts self-hosted chargeback as the command line does.
  - Data fields (series, tenants, jobs, projects) become required. An empty request is rejected. Defaults remain only for prices and percentiles. Also sanitize non-finite float inputs in FastAPI `RequestValidationError` responses so raw `NaN`/`Infinity` HTTP bodies answer `422` instead of crashing `JSONResponse`.
  - The command line takes its defaults from the same request models, so `optimize`, `schedule` and `chargeback` run on sample data today when no file is given, and the README quick start uses that. Decide when this task starts whether they then need an explicit sample switch, as `recommend --demo` has, or an input file from `examples/`.
  - Remove the helper that unpacks requests, the defaults repeated in function signatures, the reflection tests that keep the two in step, and the silent re-clamping inside the calculations.
  - Files: `capacity/finops.py`, `capacity/burst.py`, `capacity/self_hosted.py`, `capacity/gateway.py`, `capacity/capacity.py`, `capacity/models.py`, `capacity/api.py`, `capacity/cli.py`, `tests/test_request_contract.py`.
  - Done when: the command line and the API each make one call per operation and give the same result for the same request in both modes.
- [x] **P1.10 Scenario evaluation returns a typed report, outside the page**
  - Move whole-scenario evaluation out of the page module into one function that returns a typed report. `capacity demo --format json`, the page and an API route read that report.
  - The report holds only what the scenario defines. Today a scenario's result also carries sample self-hosted series under the scenario's own project label.
  - Files: `capacity/portal.py`, `capacity/scenario.py`, `capacity/cli.py`, `capacity/api.py`.
  - Done when: the page renderer takes a report and calls no calculation.
- [x] **P1.11 Telemetry series have a typed key**
  - Replace the text key (`folder/model@region`) with a typed key. Filter by model and region, and apply the folder map, in one place.
  - `collect` reads through the same port as `recommend`, and the access check returns a typed result.
  - Files: `capacity/forecasting.py`, `capacity/core.py`, `capacity/capacity.py`, `capacity/scenario.py`, `capacity/cli_formatters.py`.

## Milestone 3 - `v0.5.0`: Folders by job (`P2`)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18.*

Target layout across Ports, Adapters and Contracts:

```text
capacity/
  contract/    requests, results, scenario schema
  engine/      sizing, burst, commitment, chargeback, scheduler, pooling, kpis,
               self_hosted, stats, catalog
  ingest/      prometheus, request_logs, trace_files
  telemetry/   port, local, cloud_monitoring
  gateway/     litellm, envoy, gke, header_policies, callback template
  showcase/    evaluate (scenario to report), sample data, page renderer, assets/
  cli/         one module per command, output formatting
  api.py
```

- [x] **P2.1 Move modules into folders by job**
  - Move with `git mv`. Old import paths are not kept: no re-export modules.
  - Update the imports in `tests/` and `benchmarks/`, the entry points in `pyproject.toml`, `Dockerfile` and `capacity/cli.py`, the lint exemption for the gateway callback, and the path the gateway generator reads the callback from.
  - Done when: the gate is green and the README blocks are unchanged.
- [x] **P2.2 Split the modules that hold several jobs**
  - `finops` into commitment, chargeback, scheduler, pooling and KPIs. `forecasting` into maths, port and local adapter. `models` into requests and results. The command line into one module per command.
  - Done when: no module exists only to keep another one under the line cap.
- [x] **P2.3 The guard states one rule per folder**
  - The engine imports only the contract. One file imports the cloud SDK. The page renderer imports no calculation. The command line and the API may import anything.
  - Files: `specs/ast.rules.yaml`, `tests/test_isolation_guard.py`, the repository map in `AGENTS.md`, `ADR-005` in `DECISIONS.md`, and the links in `docs/`.

## Milestone 4 - `v0.6.0`: The page shows only what the engine computed (`P3`)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18.*

- [x] **P3.1 One typed, display-ready object for the page**
  - It carries every card, table, label and control default. The renderer turns it into HTML and escapes everything in one place, including the default page `<title>` (`Multi-Gateway & FOCUS 1.2` in `capacity/showcase/view.py`).
  - Files: `capacity/showcase/view.py`, `capacity/showcase/tabs.py`, `capacity/showcase/renderer.py`, `capacity/showcase/assets/workbench.html`.
  - Done when: no template holds a number, every text slot is escaped in one place, and a test renders a hand-made object.
- [x] **P3.2 No formulas in the browser**
  - The offline file is a snapshot with disabled controls. The served page sends control changes to the API (`POST /api/v1/burst/window`, `/api/v1/prefix-cache/model-tier`, `/api/v1/self-hosted/disaggregated`, `/api/v1/gateway/config`) and shows the answer, honouring active scenario overrides.
  - Deleted the tables and rules typed in the script and amended `ADR-004`.
  - Files: `capacity/showcase/assets/workbench.js`, `capacity/api.py`, `capacity/contract/requests.py`, `DECISIONS.md`.
  - Done when: the script holds no capacity, threshold or weight.
- [x] **P3.3 HTML, CSS and JavaScript as files**
  - Kept as real files inside `capacity/showcase/assets/` and joined at render time, so the export stays one file. Aligned the stylesheet font-family fallback stacks with `.stitch/DESIGN.md`, and extended the identifier scan to `.js` and `.css`.
  - Files: `capacity/showcase/assets/workbench.html`, `capacity/showcase/assets/workbench.css`, `capacity/showcase/assets/workbench.js`, `capacity/showcase/assets/favicon.svg`, `tests/test_repo_hygiene.py`.
- [x] **P3.4 Page tests on values**
  - Sent hostile text through every scenario text field with balanced-tag parsing, and rendered once per combination of page sections asserting engine values.
  - Files: `tests/test_portal_export.py`.

## v0.6.1 (Review 2026-11-18 - P0-P2)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18.*

Verified production, CI, and documentation defects at `v0.6.0` (`e95f059`). All items predate `v0.5.0` except `R13` and `R14`, which came with `v0.6.0`. Items are plain bullets like the rest of the backlog; promote them to `- [ ]` when scheduling them and write the failing test first for each one.

### `P0`: wrong numbers or broken gateway enforcement *(v0.6.1)*

- **R1 Prometheus: scrapes without timestamps are added together**. `capacity/ingest/prometheus.py` flushes a scrape whenever a `(metric, labels)` series repeats without splitting multi-label shards within one scrape (`test_untimestamped_concatenated_scrapes_are_differenced` in `tests/test_critical_invariants.py`).
- **R2 Prometheus: the tenant TPM is an unweighted mean of interval rates**. `capacity/ingest/prometheus.py` integrates duration-weighted 60-minute hourly tenant bins and resamples unequal multi-scrape intervals onto 1-minute `tpm_series` steps (`test_unequal_scrape_intervals_weight_tenant_tpm_and_expand_minute_series` in `tests/test_critical_invariants.py`).
- **R3 Cloud Monitoring without `--model` never queries 3P Claude `token_count`**. `capacity/telemetry/cloud_monitoring.py` queries `PARTNER_TOKEN_METRIC` whenever `models_filter is None or has_partner_filter` while skipping partner Claude IDs on 1P `PT_METRIC` (`test_fetch_throughput_partner_token_count_weighting_and_mixed_models` in `tests/test_claude_3p.py`).
- **R4 JSONL logs: `tpm_series` drops idle minutes, so `recommend` puts new dates on them and `optimize` treats them as 24x7**. `capacity/ingest/request_logs.py` zero-fills `tpm_series` across `[first_minute .. last_minute]` and records `window_start_utc` on `VllmTelemetrySnapshot`, which `capacity/cli/recommend.py` passes to `minute_list_to_sparse` (`test_sparse_multi_day_jsonl_preserves_idle_minutes_and_daily_peaks` in `tests/test_ingestion.py`).
- **R5 Chargeback: a scalar tenant beside hourly lists counts as one hour, then zeros**. `capacity/contract/common.py` and `capacity/engine/commitment.py` reject empty or mismatched-length list series and broadcast scalar tenant rates across the common horizon (`test_chargeback_scalar_tenant_is_constant_and_mismatched_lengths_fail` in `tests/test_critical_invariants.py`).
- **R6 `GSU_CAPACITY_*` overrides: a malformed value is ignored without a message, and `0` crashes later**. `capacity/engine/catalog.py` raises `ValueError("<key>=<val>: expected a positive integer TPM per GSU")` on non-integer or `<= 0` `GSU_CAPACITY_*` overrides and `<= 0` programmatic overrides (`test_malformed_or_nonpositive_gsu_capacity_override_raises` in `tests/test_critical_invariants.py`).
- **R7 The generated LiteLLM callback never writes to the shared Redis cache**. `capacity/gateway/litellm_burndown_callback.py` calls `DualCache.async_increment_cache` and uses its authoritative Redis post-increment return value directly (avoiding stale `DualCache.async_get_cache` worker-local reads), updates `tests/conftest.py` (`FakeDualCache`), `benchmarks/live_sse_harness.py`, and `DECISIONS.md` (`ADR-007`), and logs a one-time warning when Redis is absent (`test_litellm_callback_dual_cache_cross_worker_ceilings_and_failure_refund` in `tests/test_critical_invariants.py`).
- **R8 The per-team funded ceiling reads `max_budget_tpm`, which LiteLLM's `UserAPIKeyAuth` does not have**. `capacity/gateway/litellm_burndown_callback.py` resolves `team_metadata["funded_burndown_tpm"]` / `metadata["funded_burndown_tpm"]` first, falling back to `team_tpm_limit`, `tpm_limit`, and `DEFAULT_TEAM_FUNDED_BURNDOWN_TPM` (`tests/test_critical_invariants.py`, `tests/test_claude_3p.py`, `tests/test_physics_oracles.py`).

### `P1`: wrong gateway pricing, regressions, and CI drift *(v0.6.1)*

- **R9 The generated LiteLLM config hardcodes per-token prices for every model**. `capacity/gateway/litellm.py` omits hardcoded Flash `input_cost_per_token` / `output_cost_per_token` from `model_info` entries so LiteLLM resolves model-specific pricing (`test_litellm_yaml_omits_hardcoded_flash_token_prices` in `tests/test_claude_3p.py`).
- **R10 Multi-model JSONL: `snapshot.model` is the last record's model, while `tpm_series` sums all models**. `capacity/ingest/request_logs.py` and `capacity/ingest/prometheus.py` reject multi-model inputs on single-model commands (`optimize`, `chargeback`) with one error line naming every model unless `--model` filters the series (`test_single_model_commands_reject_mixed_model_jsonl_unless_model_selected` in `tests/test_ingestion.py`), while `recommend --vllm-file` preserves per-model series.
- **R12 Minute series: zero-filling stops above the cell cap silently, and `collect` with `by_region=False` drops non-first regions and `@region` log counts**. `capacity/ingest/request_logs.py` raises `ValueError` above `MAX_SERIES_POINTS` or `MAX_SERIES_CELLS`, and `capacity/showcase/evaluate.py` sums regional minute series element-wise and folds `@region` spillover/429 log counts when `by_region=False` (`test_minute_series_by_key_raises_above_cell_cap` and `test_collect_no_by_region_sums_regional_minute_series_and_log_counts` in `tests/test_scenario_and_collect.py`).
- **R13 A bad scenario file returns a plain 500 on every sizing route (regression in `v0.6.0`)**. `capacity/api.py` caches `CAPACITY_SCENARIO_FILE` by `(path, mtime_ns, size)` and maps `OSError` / `ValueError` / `ValidationError` (including unreadable JSON/YAML in `capacity/showcase/evaluate.py`) to structured `422` JSON responses (`test_corrupted_or_missing_scenario_file_returns_structured_json_error` in `tests/test_request_contract.py`).
- **R14 The workbench swallows API errors and leaves stale numbers on screen (`v0.6.0`)**. `capacity/showcase/assets/workbench.js` treats blank or non-finite numeric fields as invalid without calling `fetch`, catches network rejections and non-OK HTTP responses, surfaces `badge-danger` + `danger-banner` error states, and restores `info-banner` on recovery (`test_workbench_surfaces_error_state_on_empty_input_non_ok_and_network_failure` in `tests/test_portal_export.py`).
- **R17 Cloud Build and `Dockerfile` pin `uv:0.9.1` against a `revision = 5` lockfile**. Aligned `uv` to `0.12.24` across `cloudbuild.yaml`, `Dockerfile`, `pyproject.toml` (`[tool.uv] required-version = ">=0.12.24"`), and `AGENTS.md` (`test_uv_version_pinned_consistently_across_ci_dockerfile_pyproject_and_agents` in `tests/test_repo_hygiene.py`).

### `P2`: override family rules, callback leaks, auth errors, and broken doc/skill commands *(v0.6.1)*

- **R20 Capacity overrides for unlisted Claude models silently get Gemini 1P rules**. `capacity/engine/catalog.py` infers `claude_3p` (`CLAUDE_3P_V55_SPEC` / `CLAUDE_3P_OPUS_4_SPEC` / `CLAUDE_3P_SONNET_SPEC`), `GEMINI_1P_PRO_SPEC`, or `SELF_HOSTED_VLLM_SPEC` on unlisted capacity overrides and `is_partner_claude` (`test_unlisted_claude_capacity_override_infers_claude_3p_family` in `tests/test_claude_3p.py`).
- **R21 Workbench input race**. `capacity/showcase/assets/workbench.js` debounces each recalculation handler by `150` ms and cancels superseded in-flight `fetch` requests via per-handler `AbortController` instances (`test_workbench_debounces_and_aborts_stale_inflight_requests` in `tests/test_portal_export.py`).
- **R23 Callback state leaks on failure and across minutes** *(with `R7`).* `capacity/gateway/litellm_burndown_callback.py` prunes `_local_window` keys older than `current_minute - 2`, avoids writing `_local_window` when shared Redis succeeds, and refunds `-reserved` once in `async_log_failure_event` (`test_litellm_callback_dual_cache_cross_worker_ceilings_and_failure_refund` in `tests/test_critical_invariants.py`).
- **R28 `SKILL.md` commands fail as written**. Added `--demo` to `optimize` and `schedule` in `skills/capacity/SKILL.md` (with `.agents/skills` symlinked to `../skills`) and verified all offline `SKILL.md` CLI examples in `tests/test_documentation_contract.py` (`test_skill_md_symlink_and_runnable_commands_exit_zero`).
- **R29 A README quickstart command prints `?` replicas and `0.0%` duty cycle**. Split `README.md` Quick Start `# 1` into managed OTel JSONL (`examples/otel_genai_spans.jsonl --days 1`) and self-hosted Prometheus (`--mode self-hosted --vllm-file examples/vllm_prometheus_multiscrape.txt --days 1`), asserting zero `?` cells and non-zero duty cycles in `tests/test_documentation_contract.py`.
- **R38 `GET /api/v1/recommendations` with expired or offline ADC returns an unhandled 500**. `capacity/telemetry/cloud_monitoring.py` applies a bounded `30s` RPC timeout and catches `GoogleAuthError` (`TransportError`, `RefreshError`), wrapping them in `GCPQueryError` (`502` JSON on `api.py`; verified by `test_recommendations_route_maps_google_auth_transport_and_refresh_errors_to_502` in `tests/test_request_contract.py`).
