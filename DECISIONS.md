# DECISIONS.md — Architectural Decision Records (`capacity`)

## ADR-001: Server-Side Cloud Monitoring Reduction, Active-Hours Sizing & Holt Forecasting
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** Summing `consumed_token_throughput` points in Python without a `cross_series_reducer` overwrites timestamps across regions (`us-central1` vs. `europe-west4`), while zero-padding missing minutes across `days * 1440` causes 8x5 enterprise workloads (`~23.8%` duty cycle) to collapse to `0 GSUs` at `P50` and `P75`.
- **Decision:** Enforce server-side `REDUCE_SUM` grouped by `["resource.label.model_id", "resource.label.location", "metric.label.request_type"]`, expose both `all_hours` (24x7) and `active_hours` (non-zero business minutes + `duty_cycle_pct`) percentiles, support multi-project/folder roll-ups (`--folder-map`), and project short-horizon trajectory via pure-Python **Holt's Double Exponential Smoothing** (`holts_linear_trend`).
- **Consequences:** Accurate regional GSU/replica sizing for 8x5 enterprise workloads and proactive 24-hour capacity forecasting with zero extra ML library dependencies.
- **Amendment (2026-11-11):** Holt has no seasonal term, so it runs on one peak per active day (`forecast_steps / 24` days ahead). Running it on per-minute points projected the last few minutes and under-sized the `--demo` trace by ~2x. Unknown models and self-hosted profiles now raise `UnknownModelError` (HTTP 422 / CLI exit 1) instead of defaulting to `gemini-3.8-flash` capacity or `llama-3.3-70b-fp8`; supply `GSU_CAPACITY_<MODEL>` to size a model outside the built-in table.

## ADR-002: Sub-Second Token-Bucket Micro-Burst, $1/N$ Dilution & Model-Tier Prefix-Cache Physics
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** Cloud Monitoring charts average throughput over 60-second windows, masking sub-minute agent bursts that exhaust the `1s` (`>=50` GSUs) or `10s` (`<50` GSUs) token bucket (`5%` burst credit). Simultaneously, prompts exceeding `200,000` tokens jump from `1.0x` input / `4.0x` output to `2.0x` input / `7.5x` output burndown, stateless round-robin load balancing across $N$ endpoints/replicas dilutes replica-local KV-cache hit rates from $h$ to $h/N$, and Flash-tier models (`gemini-3.8-flash`) enforce `6,144`-token floors with `4,096`-token blocks where dynamic tokens at offset `<4,096` cause Block 0 contamination (`0%` cache hit).
- **Decision:** Model explicit burndown weights (`compute_weighted_burndown_tokens`), sub-minute token-bucket enforcement (`evaluate_micro_burst_window`), round-robin vs. prefix-aware sticky routing (`evaluate_prefix_cache_dilution`), and model-tier block alignment, Turn-0 dummy-turn routing anchors, and `thought_signature` preservation (`evaluate_model_tier_prefix_cache`) in [`capacity/burst.py`](capacity/burst.py).
- **Consequences:** Quantifies exact GSU/replica waste from Block 0 contamination and L7 round-robin routing while explaining why `<70%` 60-second utilization triggers HTTP 429s.

## ADR-003: Newsvendor Hybrid Commitment Sizing, Folder RHPT Pooling & FOCUS 1.2 Chargeback
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** Sizing 24x7 reserved capacity to cover `P99` peaks leaves `60%+` of the reservation idle overnight. Meanwhile, internal chargeback spreadsheets fail by (1) counting unweighted raw tokens instead of `4x–7.5x` decode/reasoning burndown weights, (2) dumping overnight idle reservation costs onto active product teams (`3x` early-adopter unit penalty), and (3) paying an extra `0.5x` on the separate Batch Prediction API while 24x7 PT sits idle overnight.
- **Decision:** Solve optimal base commitment via the **Newsvendor critical fractile** ($C^* = \text{Quantile}_{1 - d_{\text{eff}}}(S(t))$) paired with Priority PayGo (`1.8x`), Standard (`1.0x`), or Flex (`0.5x`) spillover, quantify shared Folder RHPT pooling (`evaluate_folder_pooling`) and `$0` online PT scavenging vs. Batch API double-payment (`evaluate_batch_scavenger_economics`), implement **FinOps FOCUS 1.2 Two-Pool Chargeback** (`compute_focus_chargeback`), and emit the **5 Standardized FinOps for AI KPIs** (`CPWT_1M`, `CPVO` bridging to `bq-eval`, `TER`, `CSR`, `ECUR_168h`) in [`capacity/finops.py`](capacity/finops.py).
- **Consequences:** Minimizes blended monthly spend and links capacity economics directly to `bq-eval`'s **Cost per Verified Outcome (`CPVO`)**.

## ADR-004: Dual-Surface Typer CLI, FastAPI Service & Interactive GM3 Light 4-Tab Workbench
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** FinOps and platform engineering teams need scriptable CLI/JSON automation in CI, a self-contained HTML executive scorecard (`--html-out`), and an interactive browser/REST service on Cloud Run.
- **Decision:** Expose identical Pydantic v2 schemas across the Typer CLI (`capacity demo|recommend|optimize|gateway|schedule|chargeback|serve`) and FastAPI (`GET /` serving the 4-tab GM3 Light workbench from [`capacity/portal.py`](capacity/portal.py) per [`.stitch/DESIGN.md`](.stitch/DESIGN.md)).
- **Consequences:** 100% schema parity between terminal tables, standalone HTML executive scorecards, JSON pipelines, and Cloud Run deployments.

## ADR-005: Portable Air-Gapped Repository & Cloud Build CI (`STD-GOV` / `STD-TOOL`)
- **Status:** Accepted (`2026-11-05`)
- **Context:** As a public reference platform (`https://github.com/tvaroska/capacity`), the repository must remain 100% free of customer data, internal identifiers, and hardcoded host paths.
- **Decision:** Enforce automated repository hygiene and link-integrity gates in [`tests/test_repo_hygiene.py`](tests/test_repo_hygiene.py), strict `STD-TOOL` Ruff (`E,W,F,I,B,C4,UP,S,BLE,ANN,RUF`) + `mypy`, and GCP Cloud Build ([`cloudbuild.yaml`](cloudbuild.yaml)).
- **Consequences:** Zero customer or internal leaks; deterministic offline verification via `<HOME>/projects/bin/proj-test capacity`.

## ADR-006: Two-Phase Self-Hosted GPU/TPU Model Support & Disaggregated Prefill/Decode Sizing
- **Status:** Accepted (`2026-11-08`)
- **Context:** Enterprises run a hybrid portfolio of Managed Gemini Platform GSUs and self-hosted open-weight models (`Llama`, `Gemma`, `Qwen`, `DeepSeek` on vLLM / SGLang). Self-hosted endpoints suffer from upfront regional GPU quota locking (`max_replica_count * accelerators_per_replica`), the **Continuous-Batching Duty-Cycle Illusion** (`accelerator/duty_cycle < 60%` while `vertex_vllm_gpu_cache_usage_perc > 90%`), and prefill-decode interference on long prompts.
- **Decision:** Implement [`capacity/self_hosted.py`](capacity/self_hosted.py) supporting Phase 1 Vertex AI `DedicatedResources` audits, Disaggregated Prefill/Decode worker pool sizing (`size_disaggregated_prefill_decode`), and Phase 2 vLLM/SGLang Prometheus `/metrics` & OpenTelemetry `gen_ai.usage.*` JSONL log ingestion (`parse_inference_server_telemetry`).
- **Consequences:** One unified CLI and API governs both Managed GSUs (`--mode gsu`) and Self-Hosted GPU/TPU clusters (`--mode self-hosted` / `--vllm-file`).

## ADR-007: Multi-Gateway Control Plane (`LiteLLM`, `Envoy AI Gateway`, `GKE epp`) & 3-Tier Residual Capacity Scheduling
- **Status:** Accepted (`2026-11-08`)
- **Context:** Capacity plans only succeed in production if the central AI gateway enforces weighted burndown quotas, routes bursts using the 8 Vertex AI admission headers, avoids blackholing PT pools on 1-second micro-burst `429`s, and harvests overnight residual PT headroom in priority order.
- **Decision:** Implement [`capacity/gateway.py`](capacity/gateway.py) to generate production-hardened configurations and scale audits for **LiteLLM Proxy** (`litellm_config.yaml` + `litellm_burndown_callback.py` `CustomLogger` + Prisma/Postgres/SSE bottleneck audit), **Envoy AI Gateway** (`AIGatewayRoute` two-phase `type: CEL` `llmRequestCosts`), and **GKE Inference Gateway** (`InferencePool` + `EndpointPickerConfig` `epp`), alongside the **3-Tier Residual Capacity Scheduler** (`schedule_residual_capacity`: `Tier 0 Revenue > Tier 1 Hard-Deadline EDF > Tier 2 Evergreen Water-Fill`) in [`capacity/finops.py`](capacity/finops.py).
- **Consequences:** Translates sizing and tokenomics math directly into deployable gateway manifests and deterministic residual capacity schedules.
