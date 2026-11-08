# `capacity` — Architecture & Product Roadmap (`v0.2.0` -> `v0.3.0`)

`capacity` unifies **empirical telemetry sizing**, **sub-second token-bucket & KV-cache physics**, **Newsvendor hybrid commitment optimization**, and **FinOps FOCUS 1.2 Two-Pool Chargeback** across both **Managed Reserved Throughput (`GSU`)** and **Self-Hosted Open-Weight Models (`GPU / TPU Replicas`)**.

---

## 1. Two-Phase Architecture

| Phase | Target Workloads | Telemetry & Log Sources | Core Capabilities & Deliverables | Status |
| :--- | :--- | :--- | :--- | :---: |
| **Phase 1 (Current — `v0.2.0`)** | **1A. Managed Gemini Platform (`--mode gsu`)**<br>**1B. Self-Hosted Models on Vertex AI Prediction (`--mode self-hosted`)** | • `aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput` (`dedicated` + `shared`)<br>• `aiplatform.googleapis.com/prediction/online/prediction_count`, `accelerator/duty_cycle`, `accelerator/memory_usage`<br>• `prometheus.googleapis.com/vertex_vllm_gpu_cache_usage_perc`, `vertex_vllm_num_requests_waiting`<br>• Offline JSON traces & `--demo` profiles | • Active-business-hours (`active_hours`) vs. 24x7 (`all_hours`) percentile sizing<br>• Multi-project & folder roll-up (`--project` repeatable + `--folder-map`)<br>• Pure-Python **Holt's Double Exponential Smoothing** (`--forecast-hours`)<br>• `1s` (`>=50` GSUs) / `10s` (`<50` GSUs) token-bucket micro-burst & `>200K` burndown cliff (`2.0x` input / `7.5x` output)<br>• Self-hosted **Upfront GPU Quota Lock** (`max_replica_count * accelerators`), **Duty-Cycle vs. KV-Cache Saturation Trap**, & `ScaleToZeroSpec` savings<br>• Round-robin $1/N$ prefix/KV-cache dilution vs. prefix-aware routing<br>• Newsvendor hybrid commitment (`PT + Priority PayGo` or `Reserved CUD min_replicas + Elastic Burst`)<br>• FOCUS 1.2 Two-Pool Chargeback (Weighted Burndown vs. Raw Tokens + Active vs. Unallocated Idle + Batch Water-Fill)<br>• Interactive GM3 Light Web Calculator (`GET /` on `capacity serve`) | **Implemented (`v0.2.0`)** |
| **Phase 2 (Next — `v0.3.0`)** | **Self-Hosted Inference Servers Across Any Cluster (`vLLM`, `SGLang`, `TGI`, `NIM`, `llm-d`)** | • Direct Prometheus `/metrics` scraper (`vllm:*`, `sglang:*`)<br>• OpenAI-compatible SSE (`"include_usage": true`) & JSONL request log collector<br>• OpenTelemetry `gen_ai.*` span exporter / collector receiver | • **File-based vLLM/SGLang Prometheus & JSONL parser (`--vllm-file`)** included in `v0.2.0` (`capacity/self_hosted.py`)<br>• **Live Polling Daemon & OTel Receiver (`v0.3.0`):** Lightweight async scraper polling `/metrics` across K8s pod endpoints (`llm-d` / `AIBrix` / GKE Inference Gateway) at `1s`–`5s` intervals to capture sub-minute queue depth (`num_requests_waiting`) and KV-cache preemption events (`gpu_cache_usage_perc`)<br>• **Disaggregated Prefill / Decode Sizing (`v0.3.0`):** Separate capacity sizing for prefill-heavy vs. decode-heavy worker pools | **Parser Seam Implemented (`v0.2.0`); Live Daemon Planned (`v0.3.0`)** |

---

## 2. Showcase Build Passes (`v0.2.0`)

- [x] **Pass A — Dual-Mode Engine (`gsu` + `self-hosted`), Multi-Project/Forecast Upgrades & `P1–P7` Fixes**
  - [x] Add `capacity/self_hosted.py` for Phase 1 Vertex AI Self-Hosted GPU/TPU Endpoints (`DedicatedResources` + sidecar `vertex_vllm_*` metrics) and Phase 2 vLLM/SGLang Prometheus & JSONL log parsing.
  - [x] Add multi-project + folder roll-up (`--project` repeatable, `--folder-map`), Holt linear trend forecasting (`holts_linear_trend`), and `--demo` / `--trace-file` offline execution to `capacity recommend`.
  - [x] Wire `compute_weighted_burndown_tokens()` into `optimize` and `chargeback`, displaying both **Unweighted Raw-Token Chargeback** and **Naive Full-Pool Idle Tax** vs. **FOCUS 1.2 Weighted Active Chargeback**.
  - [x] Fix default `capacity optimize` scenario so 60-second utilization (`<70%`) vs. `1s`/`10s` burst window triggers `illusion_detected=True`, and align CLI/API prefix-cache defaults (`20,000` input tokens, `8` replicas).
  - [x] Enforce `STD-TOOL` gates (`hatchling`, Ruff `E,W,F,I,B,C4,UP,S,BLE,ANN,RUF`, `mypy`, untracked `.env` -> `.env.example`).
- [x] **Pass B — Story, Whitepaper Diagrams, ADRs, Interactive UI & Examples**
  - [x] Serve interactive GM3 Light Web Calculator at `GET /` on `capacity serve`.
  - [x] Upgrade `README.md` with Mermaid architecture flowcharts, dual-mode CLI examples (`--mode gsu` & `--mode self-hosted`), and the **5 Enterprise Capacity Traps**.
  - [x] Expand `DECISIONS.md` (`ADR-001` through `ADR-006`) into full ADR sections and add `examples/` + `tests/test_documentation_contract.py`.
- [x] **Pass C — Verification Gate & Release Readiness**
  - [x] Pass `<HOME>/projects/bin/proj-test capacity` with `0` Ruff errors, `0` Mypy errors, and 100% passing unit, CLI, API, self-hosted, doc-contract, and `ADR-005` hygiene tests.
