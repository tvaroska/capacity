# DECISIONS.md — Architectural Decision Records (`capacity`)

## ADR-001: Server-Side Cloud Monitoring Reduction, Active-Hours Sizing & Daily-Peak Holt Forecasting
- **Status:** Accepted (`2026-11-05`, amended `2026-11-11`)
- **Context:** Summing `consumed_token_throughput` points in Python without a `cross_series_reducer` overwrites timestamps across regions (`us-central1` vs. `europe-west4`), while zero-padding missing minutes across `days * 1440` causes 8x5 enterprise workloads (`~23.8%` duty cycle) to collapse to `0 GSUs` at `P50` and `P75`.
- **Decision:** Separate pure percentile/Holt math and `TelemetryPort` (`LocalTraceAdapter`) in [`capacity/forecasting.py`](capacity/forecasting.py) from the Cloud Monitoring driven adapter in [`capacity/core.py`](capacity/core.py). Enforce server-side `REDUCE_SUM` grouped by `["resource.label.model_id", "resource.label.location", "metric.label.request_type"]`, expose both `all_hours` (24x7) and `active_hours` (non-zero business minutes + `duty_cycle_pct`), project one peak per active day via `holts_linear_trend`, and fail closed with `UnknownModelError` instead of guessing capacities for unknown models.
- **Rejected Options:**
  - *Client-side Python timestamp summation without `REDUCE_SUM`:* Silently drops concurrent regional series and balloons gRPC payload size by `10x+`.
  - *Per-minute Holt smoothing or heavy Prophet/statsmodels dependencies:* Per-minute Holt without seasonal terms projects the most recent evening/weekend trough (`~2x` under-sizing); heavy C/Stan seasonal libraries add `>150 MB` to cold container startup for marginal short-horizon benefit.
- **Where This Breaks:**
  - `holts_linear_trend` has no weekly/holiday seasonal decomposition or prediction interval; sudden step-function product launches or <2 active days of history produce flat point estimates without confidence bounds.
  - Self-hosted Cloud Monitoring fallback (`prediction_count * tokens_per_prediction`) is a coarse proxy when direct Prometheus/OTel token counters (`--vllm-file`) are not exported.

## ADR-002: Conserved Sub-Second & 3P Dual-Bucket Burst, $1/N$ Dilution & Model-Tier Prefix-Cache Physics
- **Status:** Accepted (`2026-11-05`, amended `2026-11-12`)
- **Context:** Cloud Monitoring averages throughput over 60-second windows, masking sub-minute bursts that exhaust 1P Gemini `1s` (`>=50` GSUs) or `10s` (`<50` GSUs) token buckets (`5%` burst credit) as well as 3P Anthropic Claude `30s`/`60s` dual buckets (`0%` burst credit, flat `1.0x` input / `0.1x` cache read / `1.25x` 5m cache write / `2.0x` 1h cache write / `5.0x` output burndown, `token_count` metric). Prompts exceeding `200,000` tokens on 1P Gemini jump from `1.0x/0.1x/4.0x` to `2.0x/0.2x/7.5x` burndown, stateless round-robin routing dilutes replica KV-cache hits to $h/N$, and `gemini-3.8-flash` (`4,096`-token blocks, `6,144` floor) suffers `0%` hit collapse on Block-0 dynamic UUIDs while 3P Claude uses token-boundary explicit `cache_control` breakpoints (`512` / `1,024` / `4,096` floors).
- **Decision:** Centralize model-family physics in [`capacity/model_families.py`](capacity/model_families.py), implement conserved burst evaluation (`active_peak_tps * window_sec <= sixty_sec_tpm`) across both 1P `1s`/`10s` and 3P `30s`/`60s` enforcement modes, client-side weight Cloud Monitoring `token_count` for 3P Claude in [`capacity/core.py`](capacity/core.py), and enforce RPS-scaled prefix-cache dilution + model-tier auditing in [`capacity/burst.py`](capacity/burst.py).
- **Rejected Options:**
  - *Modeling quotas at 1-minute resolution:* Misses all sub-minute `429` storms below `70%` 60-second utilization.
  - *Unbounded `10s` burst extrapolation from a `1s` spike:* Violates 60-second token conservation when `10 * peak_1s_tps > sixty_sec_tpm`.
- **Where This Breaks:**
  - When callers only have 60-second Cloud Monitoring exports without sub-second gateway logs, `peak_1s_tps` defaults to a `1.9x` heuristic peak-to-mean ratio unless measured `peak_1s_tps` / `peak_10s_tps` is supplied.
  - Prefix-cache block sizes (`4,096` vs. token-boundary) reflect current model-family architectures and must be updated if upstream serving block sizes change.

## ADR-003: Duty-Aware Newsvendor Commitment, Exact Deadline Admission & FOCUS 1.2 Chargeback
- **Status:** Accepted (`2026-11-05`, amended `2026-11-11`)
- **Context:** Reserving 24x7 capacity at `P99` strands `60%+` overnight, unweighted raw-token chargeback subsidizes `7.5x` reasoning agents at the expense of `0.1x` cached RAG teams, and sending overnight batch jobs to the `0.5x` Batch API pays a second variable invoice while paid PT sits idle.
- **Decision:** Solve integer commitment sizing via duty-aware Newsvendor optimization (`active_duty_cycle_pct`), pool regional spokes via Folder RHPT (`evaluate_folder_pooling`), solve Tier-1 hard-deadline admission via exact branch-and-bound (`<=24` jobs) executed in EDF order (`schedule_residual_capacity`, omitting `X-Vertex-AI-LLM-Request-Type` on Flex spill), and compute FOCUS 1.2 Two-Pool Chargeback + the 5 FinOps KPIs (`CPWT_1M`, `CPVO`, `TER`, `CSR`, `ECUR_168h`) in [`capacity/finops.py`](capacity/finops.py).
- **Rejected Options:**
  - *Greedy value-density sorting alone for Tier-1 deadlines:* Suboptimal on classic deadline-knapsack instances where one medium-density job blocks two slightly lower-density jobs with higher combined SLA value.
  - *Full-pool 100% cost allocation to active tenants:* Penalizes early adopters `3x` during initial platform ramp-up.
- **Where This Breaks:**
  - Newsvendor assumes stationary spillover prices (`1.8x` Priority) with infinite upstream Priority elasticity; during regional Priority capacity brownouts, spillover suffers queuing delay rather than pure price multiplication.
  - Above `24` simultaneous deadline jobs, `schedule_residual_capacity` falls back from exact branch-and-bound to greedy density ranking to keep runtime `<5ms`.

## ADR-004: Dual-Surface Typer CLI, FastAPI Service & Decomposed GM3 Light 4-Tab Workbench
- **Status:** Accepted (`2026-11-05`, amended `2026-11-11`)
- **Context:** Platform and FinOps engineers need scriptable CLI/JSON automation in CI, an offline shareable HTML scorecard (`--html-out`), and an interactive REST/browser workbench on Cloud Run without `>1,000` LOC monolith files.
- **Decision:** Share Pydantic v2 wire schemas (`BoundedTpmFloat`, `allow_inf_nan=False`, `max_length=50_000`) across [`capacity/cli.py`](capacity/cli.py) and [`capacity/api.py`](capacity/api.py), and split HTML/JS presentation ([`capacity/portal_template.py`](capacity/portal_template.py)) from payload assembly ([`capacity/portal.py`](capacity/portal.py)), suppressing live `/docs` links when `static_export=True`.
- **Rejected Options:**
  - *Separate React/Node SPA build pipeline:* Adds Node/npm build complexity and breaks single-file offline `--html-out` email/Slack sharing.
  - *Unbounded float arrays on FastAPI endpoints:* Allows `[1e15]` or `Inf` payloads to block the Uvicorn event loop or raise `HTTP 500`.
- **Where This Breaks:**
  - Client-side interactive sliders in `portal_template.py` mirror core sizing formulas in browser JavaScript so static `--html-out` files work offline without a running server; Tab 3/Tab 4 tables remain static snapshots unless re-generated via the CLI or REST API.

## ADR-005: Portable Air-Gapped Repository, Declarative AST Rules & Locked Cloud Build CI
- **Status:** Accepted (`2026-11-05`, amended `2026-11-11`)
- **Context:** As a public reference platform (`https://github.com/tvaroska/capacity`), the repository must stay 100% free of customer identifiers, internal URLs, and unlocked CI tool drift.
- **Decision:** Enforce 5 declarative Tree-sitter rules in [`specs/ast.rules.yaml`](specs/ast.rules.yaml) (verified with positive/negative controls in [`tests/test_repo_hygiene.py`](tests/test_repo_hygiene.py) and [`tests/test_isolation_guard.py`](tests/test_isolation_guard.py)), lock `ast-grep-cli` and `pyyaml` in public-PyPI [`uv.lock`](uv.lock), and validate filter identifiers against strict regex patterns in [`capacity/core.py`](capacity/core.py).
- **Rejected Options:**
  - *Installing `ast-grep-cli` via unlocked `uv pip install` in `cloudbuild.yaml`:* Risks supply-chain/version drift and silently skips hygiene tests when `.venv/bin` is off `PATH`.
- **Where This Breaks:**
  - Static regex and AST checks cannot inspect external `--trace-file` or `--vllm-file` artifacts before a user commits them; operators must keep proprietary telemetry outside git.

## ADR-006: Two-Phase Self-Hosted GPU/TPU Sizing, Apples-to-Apples P/D Split & Multi-Scrape Ingestion
- **Status:** Accepted (`2026-11-08`, amended `2026-11-11`)
- **Context:** Self-hosted vLLM/SGLang endpoints lock `max_replica_count * GPUs` upfront, suffer continuous-batching duty-cycle illusions (`duty_cycle < 60%` while KV-cache `>85%`), and export messy cumulative Prometheus counters or nested OpenTelemetry `gen_ai.usage.*` spans.
- **Decision:** Implement bounded-grid replica Newsvendor sizing, unclamped apples-to-apples Disaggregated Prefill/Decode sizing (`in_tok / 4.5` prefill scaling on both pools), multi-scrape counter-rate + counter-reset handling, nested OTel attribute extraction, UTC timestamp bucketing, and `/ 60.0` hourly-to-TPM conversion in [`capacity/self_hosted.py`](capacity/self_hosted.py).
- **Rejected Options:**
  - *Clamping `monolithic_replicas = max(total_disagg, ...)` or charging `1.0x` weighted TPS on monolithic prefill:* Artificially inflates monolithic replica counts by `3.3x–7.8x` and hides the fact that disaggregation loses (`2` vs. `1` replica) at low QPS.
  - *Treating single-scrape cumulative Prometheus counters as 1-minute TPM without uptime/window scaling:* Over-sizes long-running pods by orders of magnitude.
- **Where This Breaks:**
  - A single Prometheus scrape that omits both `# scrape_timestamp_seconds` pairs and `process_start_time_seconds` / `uptime_seconds` cannot distinguish a 1-minute delta from a multi-hour cumulative counter unless `scrape_window_minutes` is specified.
  - Disaggregated P/D sizing models steady-state Little's Law KV occupancy and does not simulate RDMA/RoCE KV-transfer network congestion spikes (`llm-d` NIXL transfer stalls).

## ADR-007: Safe YAML Multi-Gateway Generation & Stateful Redis Pre-Flight Floor Enforcement
- **Status:** Accepted (`2026-11-08`, amended `2026-11-12`)
- **Context:** String-interpolated gateway YAML risks syntax/label breakage on dotted model IDs (`gemini-3.8-flash` violates K8s RFC-1035 Service names), while `cost.request=0` pre-flight admission with stateless metadata trusts untrusted callers and allows concurrent slow `>200K` SSE streams to overshoot BU quotas before post-SSE settlement. Additionally, 3P Anthropic Claude PT enforces `30s/60s` buckets, `5.0x` output / `1.25x` 5m / `2.0x` 1h cache-write burndown, Standard PayGo spillover (no Priority PayGo), and project-scoped quotas without Folder RHPT.
- **Decision:** Serialize all gateway manifests via `yaml.safe_dump` / `yaml.dump_all` with `_k8s_dns_label` (`<=63` chars, dot-free RFC-1035 labels, aligned `-epp-config` refs), enforce per-BU `BackendTrafficPolicy` limits with `cost.request.number: 256` and model-family `CEL` burndown weights (`1P` vs. `3P` Claude), emit Standard PayGo spillover + hub-project governance warnings for 3P Claude, and track rolling Redis minute windows (`async_get_cache` + `async_increment` with a `1,024`-token pre-SSE reservation and `cached_tokens` / Anthropic `cache_creation` + `reasoning_tokens` post-SSE delta) in [`capacity/gateway.py`](capacity/gateway.py).
- **Rejected Options:**
  - *Reading `team_current_burndown_tpm` from client request `metadata`:* Trivially bypassed by omitting the header/metadata field.
  - *Zero pre-SSE reservation (`cost=0`):* Vulnerable to TOCTOU burst overshoot when dozens of slow reasoning streams start simultaneously.
- **Where This Breaks:**
  - A fixed `256` (Envoy) or `1,024` (LiteLLM) pre-SSE token reservation bounds connection floods but can still temporarily under-reserve if `100+` concurrent `240K`-token streams arrive in the same sub-second before any stream finishes; hard concurrency limits (`concurrent_sse_streams`) remain necessary alongside token rate limits.

## ADR-008: Open-Loop Level 3 Empirical Trace & Live HTTP SSE Verification (`benchmarks/`)
- **Status:** Accepted (`2026-11-11`)
- **Context:** Closed-loop unit tests (`Level 1`) verify formula self-consistency on toy arrays but do not test real production burst arrival distributions, optimality gaps against an independent hindsight solver (`Level 2`), or live concurrent HTTP/1.1 SSE callback execution (`Level 3`).
- **Decision:** Commit `10,000` real Microsoft Azure Public LLM Inference trace requests (`CC-BY-4.0`, [`benchmarks/corpora/MANIFEST.json`](benchmarks/corpora/MANIFEST.json)), benchmark `capacity` against an ex-post omniscient `L2` oracle in [`benchmarks/trace_backtest.py`](benchmarks/trace_backtest.py), execute `1,500` live loopback `uvicorn` + `httpx` SSE streams in [`benchmarks/live_sse_harness.py`](benchmarks/live_sse_harness.py), and verify SHA-256 tamper-evident receipts ([`benchmarks/results/l3_receipt.json`](benchmarks/results/l3_receipt.json)) in [`tests/test_l3_receipt.py`](tests/test_l3_receipt.py).
- **Rejected Options:**
  - *Pseudo-random Poisson synthetic trace generation:* Smooths away heavy-tailed sub-second arrival clumping and bimodal prompt/completion skew observed in real production clusters.
- **Where This Breaks:**
  - Track B exercises real HTTP/1.1 SSE streaming and compiled callback concurrency against an in-process async Redis store and mock Vertex upstream rather than a multi-node cloud Redis cluster or paid cloud GPU reservation.
