# capacity: why 24x7 AI reservations throttle at noon and pay twice at midnight

![Capacity Architecture](docs/assets/architecture_card.svg)

On Google Cloud Provisioned Throughput (`PT`) and self-hosted `vLLM`, 60-second P95 charts hide `1s` and `10s` weighted-burndown `429` bursts (`1x` input up to `7.5x` long-context decode), stateless gateways dilute prefix caches to `1/N` and blackhole pools for 60 seconds, and "50%-off" Batch APIs bill a second invoice while paid commitments sit `75%` idle overnight. **`capacity`** runs offline with zero cloud credentials or live daemons, compiling Cloud Monitoring, Prometheus, and OpenTelemetry traces into active-hours pool targets, burst-isolating **LiteLLM, Envoy, or GKE** manifests, `$0`-marginal overnight backfill schedules, and two-pool **FOCUS 1.2** chargeback ledgers.

> **Scope Boundary (One Stateless Compiler, Not Seven Daemons):** You do not deploy `capacity` in the request hot path or run all targets at once. Teams feed usage traces in, compile manifests for their **single** chosen gateway (`--target litellm | envoy | epp`), and export JSON sizing/chargeback tables to existing schedulers and data warehouses.

---

## 1. Six Month-1 Reservation Traps & Who Uses This

### Why Standard Capacity & Chargeback Models Break in Month 1

| Default Industry Habit | Month-1 Production or Financial Failure | Underlying Serving & FinOps Physics | Compiled `capacity` Output |
| :--- | :--- | :--- | :--- |
| **1. Sizing from 24x7 averages or per-project `P99`** | `P50 = 0 GSUs` on 8x5 workloads, or `60%+` overnight waste when each project reserves its own peak; self-hosted autoscaling locks `max_replicas * GPUs` upfront. | Business traffic runs `~23.8%` of the 168h week; `accelerator/duty_cycle` stays `<60%` while `vLLM` KV-cache hits `>85%` preemption. | Active-hours **Newsvendor** sizing, **Shared Folder Provisioned Throughput (`RHPT`)** follow-the-sun spoke pooling (`-33%` GSUs), and **Disaggregated Prefill/Decode** replica pools. |
| **2. Watching 60-second Cloud Monitoring charts** | Sudden HTTP `429` storms fire while 1-minute throughput charts sit calmly below `70%` utilization. | Serving quotas enforce **`1s` (`>=50` GSUs) or `10s` (`<50` GSUs)** token buckets (`+5%` burst credit), and `>200K` context prompts jump from `1x/4x` to **`2.0x` input / `7.5x` output+reasoning** burndown. | Sub-second burst simulator (`illusion_detected`) and exact `<=200K` vs. `>200K` weighted burndown calculator (`capacity optimize`). |
| **3. Injecting dynamic UUIDs/timestamps at prompt top & round-robin GPU routing** | Prefix cache hit rate collapses to `0%` on block-aligned tiers (`89.9%` down to `0.77%` when proxies strip `thought_signature`), and `N` replicas dilute hits to `h / N`. | Block-aligned tiers (`4,096`-token blocks, `6,144` floor) invalidate everything after a mutated **Block 0**; stateless load balancers re-prefill identical system prompts across every pod. | Model-tier prefix-cache linter (Block-0 check, Turn-0 dummy-turn anchor, `thought_signature` audit) and `GKE epp` prefix/KV scorer weights. |
| **4. Enforcing LiteLLM raw `tpm_limit` & default router `cooldown_time`** | Reasoning-heavy tenants blow through unweighted quotas, a 1s PT micro-burst `429` blackholes the entire PT deployment for 60s, and Prisma locks Postgres above `500 RPS`. | LiteLLM's native `tpm_limit` counts unweighted 60s raw tokens, and each Uvicorn worker spawns a separate Rust Prisma engine (`pods * workers` connections). | Tag-routed `litellm_config.yaml` (`cooldown_time` matched to `1s`/`10s` buckets + `use_redis_transaction_buffer`), `litellm_burndown_callback.py`, Envoy post-SSE `CEL`, and PgBouncer scale audit. |
| **5. Sending overnight async jobs to the `0.5x` Batch API** | Enterprise pays a **second variable invoice (`+0.5x`)** while already-paid 24x7 `PT` reservations sit `75%` idle from 8 PM to 6 AM. | Batch API jobs bypass `PT` reservations completely, whereas paced online calls with `X-Vertex-AI-LLM-Request-Type: dedicated` consume idle `PT` at **`$0.00` marginal cost**. | **Batch Double-Payment Auditor** and **3-Tier Earliest-Deadline-First (`EDF`) Residual Scheduler** (`Tier-0 Revenue > Tier-1 Deadline EDF > Tier-2 Evergreen Water-Fill`). |
| **6. Dividing the monthly reservation invoice by raw token counts** | Reasoning agents (`4x-7.5x` decode weight) get subsidized up to `4x` by high-cache RAG teams (`0.1x`), and early adopters get punished `3x` for unallocated corporate headroom. | Raw token counts ignore compute burndown weights and conflate active consumption with unallocated capacity growth buffers. | **FinOps FOCUS 1.2 Two-Pool Chargeback** (*Active Consumed Pool* billed by weighted burndown vs. *Unallocated Idle Pool* held by platform) + **5 FinOps for AI KPIs**. |

### Who Uses `capacity`?

| Persona | Core Problem Solved | Primary Entrypoints |
| :--- | :--- | :--- |
| **1. AI Platform & Gateway Engineers** *(LiteLLM, Envoy AI Gateway, GKE `epp`)* | Stop unweighted `tpm_limit` leaks, `429` router cooldown blackholing, `>500 RPS` Prisma/Postgres locks, and `1/N` replica KV-cache dilution. | `capacity gateway --target litellm\|envoy\|epp`<br>`POST /api/v1/gateway/config` |
| **2. Cloud FinOps & Capacity Planners** *(GSU, Flexible Spend Plan `FSP`, GPU commitments & BU chargeback)* | Right-size commitments with Newsvendor quantiles, pool spoke projects under Shared Folder `RHPT`, stop `+0.5x` Batch double-pay, and run auditable FOCUS 1.2 chargeback. | `capacity recommend` · `capacity schedule`<br>`capacity chargeback` · `capacity demo` |
| **3. Applied AI & Self-Hosted Model Architects** *(Prompt caching & `vLLM` / `SGLang` clusters)* | Catch Block-0 prompt cache contamination and `thought_signature` stripping; separate compute-bound Prefill pools from bandwidth-bound Decode pools. | `capacity optimize --mode self-hosted`<br>`POST /api/v1/prefix-cache/model-tier` |

---

## 2. Quick Start: Run Locally in 10 Seconds (`0` Cloud Credentials)

Clone the repository and run either the built-in reference showcase or your own exported traces (`--trace-file` / `--vllm-file`):

```bash
# 1. Run the End-to-End Showcase Scorecard & generate the self-contained 4-tab GM3 Light HTML workbench
uv run capacity demo --html-out /tmp/capacity_scorecard.html

# 2. Bring Your Own Traces (JSON minute TPM, vLLM Prometheus /metrics, or OTel gen_ai.* JSONL)
uv run capacity recommend --trace-file examples/diurnal_8x5_trace.json --active-only
uv run capacity recommend --mode self-hosted --vllm-file examples/vllm_requests.jsonl --active-only

# 3. Built-in Multi-Region Reference Sizing + Daily-Peak Holt (+24h) Trend
uv run capacity recommend --demo --active-only --by-region
uv run capacity recommend --demo --mode self-hosted --active-only --by-region

# 4. Newsvendor Hybrid Commitment, 1s/10s Micro-Burst & Self-Hosted vLLM KV-Cache Audit
uv run capacity optimize --model gemini-3.8-flash
uv run capacity optimize --mode self-hosted --model llama-3.3-70b-fp8 --vllm-file examples/vllm_prometheus_metrics.txt

# 5. Compile Production LiteLLM Proxy, Envoy AI Gateway (CEL) & GKE Inference Gateway (epp) Configs
uv run capacity gateway --target all --model gemini-3.8-flash --gsus 20 --rps 650

# 6. Compute 3-Tier EDF Overnight Residual Schedule & FOCUS 1.2 Two-Pool Chargeback
uv run capacity schedule --model gemini-3.8-flash --gsus 20
uv run capacity chargeback --model gemini-3.8-flash --gsus 10 --waterfill-tpm 300000

# 7. Launch the Interactive 4-Tab Web Workbench & OpenAPI Server (http://localhost:8080)
uv run capacity serve --port 8080
```

---

## 3. Gateway Enforcement: Two-Phase Weighted Burndown & 8 Header Policies

Output and reasoning token counts (`4.0x` at `<=200K` prompt tokens; `7.5x` at `>200K`) are unknown until streaming finishes. `capacity gateway` solves this across stateful proxies (**LiteLLM**) and stateless C++ data planes (**Envoy AI Gateway**):

```mermaid
sequenceDiagram
    participant App as Caller (BU Tag)
    participant GW as AI Gateway (LiteLLM / Envoy)
    participant Bucket as Burndown Counter (Redis / Envoy Global RL)
    participant Upstream as Reserved PT or Spillover Pool

    App->>GW: Request (prompt + x-business-unit)
    GW->>Bucket: Pre-flight check (rolling team burndown vs funded PT share; Envoy cost=0)
    alt Within Funded PT Share
        GW->>Upstream: X-Vertex-AI-LLM-Request-Type: <unset> + Shared: priority
    else Team Bursting Above Funded Share
        GW->>Upstream: X-Vertex-AI-LLM-Request-Type: shared + Shared: priority (protects peer PT)
    end
    Upstream-->>GW: SSE Stream + final usage_metadata (input, cached, output, thoughts)
    GW->>Bucket: Post-SSE exact deduction (1x/0.1x/4x <=200K or 2x/0.2x/7.5x >200K)
```

### Generated Envoy AI Gateway Post-SSE `CEL` Snippet (`capacity gateway --target envoy`)

```yaml
apiVersion: aigateway.envoyproxy.io/v1alpha1
kind: AIGatewayRoute
metadata:
  name: gemini-3.8-flash-weighted-burndown-route
spec:
  rules:
    - llmRequestCosts:
        - metadataKey: vertex_burndown_tokens
          type: CEL
          cel: >
            input_tokens > 200000
              ? int(double(input_tokens - cached_input_tokens) * 2.0
                  + double(cached_input_tokens) * 0.2
                  + double(output_tokens) * 7.5)
              : int(double(input_tokens - cached_input_tokens) * 1.0
                  + double(cached_input_tokens) * 0.1
                  + double(output_tokens) * 4.0)
```

### The 8 Canonical Header-Pair Routing Policies (`capacity/gateway.py`)

Each policy pairs `X-Vertex-AI-LLM-Request-Type` with `X-Vertex-AI-LLM-Shared-Request-Type`:

| Tier | Workload Scenario | `Request-Type` | `Shared-Request-Type` | Capacity & Billing Behavior |
| :---: | :--- | :---: | :---: | :--- |
| **1** | Prod within funded `PT` share | `<unset>` | `priority` | Consumes reserved `PT` first; spills automatically to `1.8x` Priority PayGo on saturation. |
| **2** | Prod bursting past funded `PT` share | `shared` | `priority` | Bypasses `PT` so a bursting team never starves peer reservations; bills caller at `1.8x` Priority. |
| **3** | Standard interactive prod | `<unset>` or `shared` | `standard` | Uses spare `PT` (if unset) and spills over to `1.0x` Standard PayGo. |
| **4** | Hard-capped prod (`$0` overage) | `dedicated` | `<ignored>` | Consumes `PT` only; returns HTTP `429` immediately on saturation (`$0.00` variable overage). |
| **5** | Daytime dev, UAT & eval | `shared` | `flex` | Bypasses `PT` and Priority completely; runs on `0.5x` Flex PayGo (`50%` discount). |
| **6** | Off-peak `$0` `PT` scavenger | `dedicated` | `<ignored>` | Harvests idle overnight/weekend `PT` at `$0.00` marginal cost; backs off on HTTP `429`. |
| **7** | Off-peak hybrid `PT` + Flex spill | `<unset>` | `flex` | Drains `$0.00` idle `PT` first, then spills excess batch volume to `0.5x` Flex PayGo. |
| **8** | Off-peak batch without `PT` | `shared` | `offpeak` | Executes on `0.5x` off-peak PayGo in projects with no reserved `PT`. |

---

## 4. Reference Scorecard, Plain-English 5-KPI Glossary & EDF Schedule

> **Physics vs. Reference Fixture Provenance:** Serving mechanics (`1s`/`10s` token buckets, `<=200K` `1x/0.1x/4x` vs. `>200K` `2x/0.2x/7.5x` burndown weights, `4,096`-token block alignment, and `$0` `PT` vs. `+0.5x` Batch billing) encode deterministic platform rules; Holt's double-exponential smoothing projects the trend in daily active-window peaks (one point per active day, so it never forecasts an overnight or weekend trough) rather than multi-week seasonal quantiles. Model IDs (`gemini-3.8-flash` `4,096`-block profile, `gemini-4-argon` token-boundary profile, `llama-3.3-70b-fp8`) and `--demo` dollar outputs below come from deterministic built-in reference traces so every check runs offline in `<2s`.

### 4A. Executive Showcase Scorecard (`capacity demo`) & 5-KPI Definitions

```text
=== Capacity v0.3.0 — End-to-End Executive Showcase Scorecard ===
1. FinOps for AI KPIs:    CPVO=$0.1750/outcome | CPWT_1M=$5.6193 | TER=7.4% | CSR=68.4% | ECUR_168h=57.0%
2. Folder RHPT Pooling:   Siloed=15 GSUs -> Pooled=10 GSUs (Saved=5 GSUs / $10,000/mo)
3. Batch 0.5x Trap Saved: $11,250.00/mo (Lifts PT utilization 24% -> 67.1%)
4. Disaggregated P/D:     8 Prefill + 3 Decode Replicas (vs 12 Monolithic, saves 1 replica[s])
5. Multi-Gateway Audit:   LiteLLM (48 Prisma engines, cooldown=5s, status=tune_required) + Envoy CEL + GKE epp
```

- **`CPWT_1M` (Cost per 1M Weighted Burndown Tokens):** Total amortized spend divided by millions of compute-weighted burndown tokens (`1x` uncached input + `0.1x` cached input + `4x` output, reasoning, and retry tokens).
- **`CPVO` (Cost per Verified Outcome):** Total amortized spend divided by evaluation-passing business outcomes, bridging capacity spend directly to [`bq-eval`](https://github.com/tvaroska/bq-eval) (`v_step1_cost_per_outcome`).
- **`TER` (Token Efficiency Ratio):** Delivered user-facing output tokens divided by total raw tokens (uncached input + cached input + output + thinking + retries).
- **`CSR` (Cache Savings Rate):** Share of total prompt compute saved through prefix caching (`(cached_input / total_prompt) * (1 - 0.10)`).
- **`ECUR_168h` (168-Hour Effective Capacity Utilization Rate):** Percentage of weekly 24x7 reserved burndown capacity (`168h * hourly_ceiling`) actively consumed after overnight batch scavenging.

### 4B. 3-Tier Residual Capacity Scheduler (`capacity schedule`)

Ranks `Tier-1` deadline jobs by value density (`USD / 1M burndown tokens`) subject to cumulative hourly residual headroom `A(h) = 0.85 * C_hour - R(h)` at each job's deadline cutoff `T+d`:

```text
=== 3-Tier Residual Capacity Schedule (gemini-3.8-flash, 20 GSUs) ===
Residual PT Budget (8h): 703,872,000 burndown tokens
Tier-1 Admitted (EDF PT):    420,000,000 tokens ($0 marginal)
Tier-1 Spilled (0.5x Flex):  320,000,000 tokens ($80.00)
Tier-2 Evergreen Water-Fill: 250,000,000 tokens ($0)
  - regulatory-risk-digest: status=admitted_edf_pt | deadline=T+5h | density=$66.67/1M | cost=$0.00
  - premarket-portfolio-briefings: status=admitted_edf_pt | deadline=T+6h | density=$75.00/1M | cost=$0.00
  - low-density-archive-sweep: status=spilled_or_aborted | deadline=T+6h | density=$2.81/1M | cost=$80.00
```

*(Note: `low-density-archive-sweep` exceeds cumulative `PT` headroom inside the `T+6h` cutoff and spills to `0.5x` Flex, leaving post-`T+6h` residual headroom (`T+6h..T+8h`) for deadline-free `Tier-2` evergreen water-filling at `$0.00` marginal cost.)*

---

## 5. Documentation & Trade-Offs

- [`AGENTS.md`](AGENTS.md): Repository module map and local verification commands
- [`CRITICAL.md`](CRITICAL.md): Non-negotiable serving-physics and FinOps invariants
- [`DECISIONS.md`](DECISIONS.md): Architecture Decision Records (`ADR-001` through `ADR-007`), trade-offs, and model boundaries
- [`.stitch/DESIGN.md`](.stitch/DESIGN.md): Web Workbench UI design specification (`GM3` Light tokens)
- [`docs/roadmap.md`](docs/roadmap.md): Product roadmap and planned empirical trace benchmarks
