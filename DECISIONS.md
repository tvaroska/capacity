# DECISIONS.md — Architectural Decision Records (`capacity`)

## ADR-001: Server-Side Cloud Monitoring Reduction, Active-Hours Sizing & Holt Forecasting
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** Summing `consumed_token_throughput` points in Python without a `cross_series_reducer` overwrites timestamps across regions (`us-central1` vs. `europe-west4`), while zero-padding missing minutes across `days * 1440` causes 8x5 enterprise workloads (`~23.8%` duty cycle) to collapse to `0 GSUs` at `P50` and `P75`.
- **Decision:** Enforce server-side `REDUCE_SUM` grouped by `["resource.label.model_id", "resource.label.location", "metric.label.request_type"]`, expose both `all_hours` (24x7) and `active_hours` (non-zero business minutes + `duty_cycle_pct`) percentiles, support multi-project/folder roll-ups (`--folder-map`), and project short-horizon trajectory via pure-Python **Holt's Double Exponential Smoothing** (`holts_linear_trend`).
- **Consequences:** Accurate regional GSU/replica sizing for 8x5 enterprise workloads and proactive 24-hour capacity forecasting with zero extra ML library dependencies.

## ADR-002: Sub-Second Token-Bucket Micro-Burst & $1/N$ Prefix-Cache Dilution Physics
- **Status:** Accepted (`2026-11-05`)
- **Context:** Cloud Monitoring charts average throughput over 60-second windows, masking sub-minute agent bursts that exhaust the `1s` (`>=50` GSUs) or `10s` (`<50` GSUs) token bucket (`5%` burst credit). Simultaneously, prompts exceeding `200,000` tokens jump from `1.0x` input / `4.0x` output to `2.0x` input / `7.5x` output burndown, and stateless round-robin load balancing across $N$ endpoints/replicas dilutes replica-local KV-cache hit rates from $h$ to $h/N$.
- **Decision:** Model explicit burndown weights (`compute_weighted_burndown_tokens`), sub-minute token-bucket enforcement (`evaluate_micro_burst_window` with `illusion_detected` when 60s utilization `<70%`), and round-robin vs. prefix-aware sticky routing (`evaluate_prefix_cache_dilution`) in [`capacity/burst.py`](capacity/burst.py).
- **Consequences:** Quantifies exact GSU/replica waste from L7 round-robin routing and explains why `<70%` 60-second utilization still triggers HTTP 429s or PayGo spillover.

## ADR-003: Newsvendor Hybrid Commitment Sizing & FOCUS 1.2 Two-Pool Chargeback
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** Sizing 24x7 reserved capacity to cover `P99` peaks leaves `60%+` of the reservation idle overnight. Meanwhile, internal chargeback spreadsheets fail by (1) counting unweighted raw tokens instead of `4x–7.5x` decode/reasoning burndown weights and (2) dumping overnight idle reservation costs onto active product teams (`3x` early-adopter unit penalty).
- **Decision:** Solve optimal base commitment via the **Newsvendor critical fractile** ($C^* = \text{Quantile}_{1 - d_{\text{eff}}}(S(t))$) paired with Priority PayGo (`1.8x`), Standard (`1.0x`), or Flex (`0.5x`) spillover, and implement **FinOps FOCUS 1.2 Two-Pool Chargeback** (`Active Consumed Pool` vs. `Unallocated Idle Pool` + `$0`-marginal-cost asynchronous batch water-filling) in [`capacity/finops.py`](capacity/finops.py).
- **Consequences:** Minimizes blended monthly spend and reports `FOCUS Active $`, `Unweighted Raw $`, and `Naive Full-Pool $` side by side.

## ADR-004: Dual-Surface Typer CLI, FastAPI Service & Interactive GM3 Light Portal
- **Status:** Accepted (`2026-11-05`, expanded `2026-11-08`)
- **Context:** FinOps and platform engineering teams need both scriptable CLI/JSON automation in CI and an interactive browser/REST service on Cloud Run.
- **Decision:** Expose identical Pydantic v2 schemas across the Typer CLI (`capacity recommend|optimize|chargeback|serve` with offline `--demo` mode) and FastAPI (`GET /`, `/healthz`, `/api/v1/recommendations`, `/api/v1/optimize`, `/api/v1/self-hosted/audit`, `/api/v1/vllm/parse`, `/api/v1/chargeback`).
- **Consequences:** 100% schema parity between terminal tables, JSON pipelines, and Cloud Run deployments.

## ADR-005: Portable Air-Gapped Repository & Cloud Build CI (`STD-GOV` / `STD-TOOL`)
- **Status:** Accepted (`2026-11-05`)
- **Context:** As a public reference platform (`https://github.com/tvaroska/capacity`), the repository must remain 100% free of customer data, internal identifiers, and hardcoded host paths.
- **Decision:** Enforce automated repository hygiene and link-integrity gates in [`tests/test_repo_hygiene.py`](tests/test_repo_hygiene.py), strict `STD-TOOL` Ruff (`E,W,F,I,B,C4,UP,S,BLE,ANN,RUF`) + `mypy`, and GCP Cloud Build ([`cloudbuild.yaml`](cloudbuild.yaml)).
- **Consequences:** Zero customer or internal leaks; deterministic offline verification via `<HOME>/projects/bin/proj-test capacity`.

## ADR-006: Two-Phase Self-Hosted GPU/TPU Model Support (Vertex AI Endpoints -> Direct vLLM/SGLang Logs)
- **Status:** Accepted (`2026-11-08`)
- **Context:** Enterprises run a hybrid portfolio of Managed Gemini Platform GSUs and self-hosted open-weight models (`Llama`, `Gemma`, `Qwen`, `DeepSeek` on vLLM / SGLang). Self-hosted endpoints suffer from three distinct traps: (1) upfront regional GPU quota locking against `max_replica_count * accelerators_per_replica`, (2) the **Continuous-Batching Duty-Cycle Illusion** (where `accelerator/duty_cycle` stays `<60%` during memory-bandwidth-bound decode while `vertex_vllm_gpu_cache_usage_perc` exceeds `90%` and triggers KV preemption), and (3) 24x7 `replica_hours` billing without per-tenant token labels.
- **Decision:** Implement [`capacity/self_hosted.py`](capacity/self_hosted.py) supporting **Phase 1** (Vertex AI Online Prediction `DedicatedResources` + sidecar `prometheus.googleapis.com/vertex_vllm_*` metrics) and the **Phase 2 Parser Seam** (`parse_inference_server_telemetry` ingesting vLLM/SGLang Prometheus `/metrics` snapshots and JSONL request logs).
- **Consequences:** One unified CLI and API governs both Managed GSUs (`--mode gsu`) and Self-Hosted GPU/TPU clusters (`--mode self-hosted` / `--vllm-file`).
