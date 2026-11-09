"""Interactive GM3 Light 4-Tab Web Calculator & Static HTML Executive Scorecard (`portal.py`)."""

from __future__ import annotations

import html
import json
from typing import Any

from capacity.burst import (
    evaluate_micro_burst_window,
    evaluate_model_tier_prefix_cache,
    evaluate_prefix_cache_dilution,
)
from capacity.capacity import build_response
from capacity.core import build_demo_throughput
from capacity.finops import (
    compute_finops_kpis,
    compute_focus_chargeback,
    evaluate_batch_scavenger_economics,
    evaluate_folder_pooling,
    optimize_hybrid_commitment,
    schedule_residual_capacity,
)
from capacity.gateway import generate_gateway_bundle
from capacity.models import DeadlineJobSpec
from capacity.self_hosted import (
    evaluate_self_hosted_endpoint,
    size_disaggregated_prefill_decode,
)

_FAVICON_DATA_URI = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%231a73e8'/%3E"
    "%3Cpath d='M14 46V30h8v16h-8zm14 0V18h8v28h-8zm14 0V24h8v22h-8z' fill='%23ffffff'/%3E"
    "%3Ccircle cx='46' cy='16' r='5' fill='%23fbbc04'/%3E%3C/svg%3E"
)

_PORTAL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Capacity v0.3.0 — AI Capacity, Micro-Burst, Multi-Gateway &amp; FOCUS 1.2 Showcase</title>
  <link rel="icon" type="image/svg+xml" href="__FAVICON_URI__" />
  <style>
    :root {
      --surface: #ffffff;
      --surface-container: #f8f9fa;
      --surface-container-high: #f1f3f4;
      --on-surface: #202124;
      --on-surface-variant: #5f6368;
      --primary: #1a73e8;
      --primary-hover: #1557b0;
      --primary-container: #e8f0fe;
      --on-primary-container: #174ea6;
      --outline-variant: #dadce0;
      --success: #137333;
      --success-bg: #e6f4ea;
      --warning: #b06000;
      --warning-bg: #fef7e0;
      --danger: #c5221f;
      --danger-bg: #fce8e6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Google Sans', 'Inter', -apple-system, BlinkMacSystemFont,
        'Segoe UI', Roboto, sans-serif;
      background: var(--surface-container);
      color: var(--on-surface);
      line-height: 1.5;
      font-variant-numeric: tabular-nums;
    }
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--outline-variant);
      padding: 16px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand h1 {
      font-size: 19px;
      font-weight: 700;
      margin: 0;
    }
    .brand p {
      margin: 2px 0 0;
      font-size: 12.5px;
      color: var(--on-surface-variant);
    }
    .badge {
      display: inline-block;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 11.5px;
      font-weight: 600;
    }
    .badge-blue { background: var(--primary-container); color: var(--on-primary-container); }
    .badge-green { background: var(--success-bg); color: var(--success); }
    .badge-warn { background: var(--warning-bg); color: var(--warning); }
    .badge-danger { background: var(--danger-bg); color: var(--danger); }
    main {
      max-width: 1280px;
      margin: 20px auto 48px;
      padding: 0 24px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--outline-variant);
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    .tab-btn {
      background: transparent;
      border: none;
      border-bottom: 3px solid transparent;
      padding: 10px 16px;
      font-size: 13.5px;
      font-weight: 600;
      color: var(--on-surface-variant);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .tab-btn:hover { color: var(--primary); }
    .tab-btn.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
      background: var(--primary-container);
      border-radius: 8px 8px 0 0;
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--outline-variant);
      border-radius: 12px;
      padding: 18px 20px;
      margin-bottom: 18px;
    }
    .card h2 {
      font-size: 15.5px;
      margin: 0 0 6px;
      color: var(--on-surface);
    }
    .card p.sub {
      margin: 0 0 14px;
      font-size: 12.5px;
      color: var(--on-surface-variant);
    }
    .kpi-card {
      background: var(--surface);
      border: 1px solid var(--outline-variant);
      border-radius: 12px;
      padding: 16px 18px;
    }
    .kpi-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--on-surface-variant);
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .kpi-value {
      font-size: 26px;
      font-weight: 700;
      margin: 6px 0;
      color: var(--on-surface);
    }
    .kpi-caption {
      font-size: 12px;
      color: var(--on-surface-variant);
    }
    .controls-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      background: var(--surface-container);
      border: 1px solid var(--outline-variant);
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 16px;
    }
    .field label {
      display: block;
      font-size: 11.5px;
      font-weight: 600;
      color: var(--on-surface-variant);
      margin-bottom: 4px;
    }
    .field input, .field select {
      width: 100%;
      padding: 7px 10px;
      border: 1px solid var(--outline-variant);
      border-radius: 6px;
      font-size: 13px;
      background: var(--surface);
      color: var(--on-surface);
      font-variant-numeric: tabular-nums;
    }
    .btn {
      background: var(--primary);
      color: #ffffff;
      border: none;
      border-radius: 6px;
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .btn:hover { background: var(--primary-hover); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
    }
    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--outline-variant);
      text-align: left;
    }
    th {
      background: var(--surface-container);
      font-weight: 600;
      color: var(--on-surface-variant);
    }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    code, pre {
      font-family: 'Roboto Mono', 'JetBrains Mono', ui-monospace, monospace;
      font-size: 12px;
    }
    pre {
      background: var(--surface-container-high);
      border: 1px solid var(--outline-variant);
      border-radius: 8px;
      padding: 12px;
      overflow-x: auto;
      margin: 8px 0 0;
      max-height: 340px;
    }
    .two-col {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
      gap: 16px;
    }
    .info-banner {
      font-size: 13px;
      font-weight: 600;
      color: var(--on-primary-container);
      background: var(--primary-container);
      padding: 10px 14px;
      border-radius: 8px;
    }
    .danger-banner {
      background: var(--danger-bg);
      color: var(--danger);
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
    }
    .warn-banner {
      font-size: 13px;
      background: var(--warning-bg);
      color: var(--warning);
      padding: 10px 14px;
      border-radius: 8px;
      font-weight: 600;
    }
    .docs-link {
      margin-left: 8px;
      font-size: 12.5px;
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div>
        <h1>Capacity Showcase Workbench (v0.3.0)</h1>
        <p>
          Managed Gemini Platform GSUs + Self-Hosted GPU/vLLM &bull;
          Sub-Second Burst &amp; Prefix Cache &bull;
          LiteLLM / Envoy / K8s epp &bull; FOCUS 1.2 FinOps
        </p>
      </div>
    </div>
    <div>
      <span class="badge badge-blue">GM3 Light Tier-2 Showcase</span>
      <span class="badge badge-green">FOCUS 1.2 &amp; bq-eval CPVO Ready</span>
      <a href="/docs" class="docs-link">OpenAPI /docs &rarr;</a>
    </div>
  </header>

  <main>
    <nav class="tabs" role="tablist">
      <button class="tab-btn active" onclick="switchTab('tab-sizing', this)">
        1. Dual-Mode Sizing &amp; Holt Forecast
      </button>
      <button class="tab-btn" onclick="switchTab('tab-burst', this)">
        2. Micro-Burst &amp; Prefix-Cache Physics
      </button>
      <button class="tab-btn" onclick="switchTab('tab-gateway', this)">
        3. Multi-Gateway (LiteLLM / Envoy / K8s epp)
      </button>
      <button class="tab-btn" onclick="switchTab('tab-finops', this)">
        4. FOCUS 1.2 FinOps, EDF Scheduler &amp; 5 KPIs
      </button>
    </nav>

    <!-- TAB 1: DUAL-MODE SIZING & HOLT FORECAST -->
    <section id="tab-sizing" class="tab-panel active">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Active vs 24x7 Duty Cycle</div>
          <div class="kpi-value">23.8%</div>
          <span class="badge badge-blue">8x5 Business Wave</span>
          <div class="kpi-caption">Prevents 24x7 zero-padding P50=0 under-sizing</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Upfront GPU Quota Lock</div>
          <div class="kpi-value" id="kpi-locked-gpus">__LOCKED_GPUS__ GPUs</div>
          <span class="badge badge-danger">__STRANDED_GPUS__ Stranded vs Min</span>
          <div class="kpi-caption">
            max_replica_count (__MAX_REPS__) &times; __ACCEL_PER_REP__ __ACCEL_TYPE__
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Disaggregated P/D Pool</div>
          <div class="kpi-value" id="kpi-disagg-reps">__PREFILL_REPS__P + __DECODE_REPS__D</div>
          <span class="badge badge-green">
            Saves __REPLICA_DELTA__ Replica(s) vs Monolithic
          </span>
          <div class="kpi-caption">Isolates TTFT prefill bursts from TPOT decode</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Non-Prod Scale-to-Zero</div>
          <div class="kpi-value">$__SCALE_ZERO_SAVINGS__/mo</div>
          <span class="badge badge-green">ScaleToZeroSpec (108h/wk idle)</span>
          <div class="kpi-caption">Eliminates overnight/weekend idle GPU burn</div>
        </div>
      </div>

      <div class="card">
        <h2>
          Managed Gemini Platform GSU Sizing &amp; +24h Holt Forecast
          (Active Business Hours vs 24x7)
        </h2>
        <p class="sub">
          Includes standard and high-speed (<code>-fast</code>) decode tiers,
          suffix normalization, and Holt double-exponential smoothing peak projection.
        </p>
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Region</th>
              <th class="num">1 GSU Cap (TPM)</th>
              <th class="num">Duty Cycle</th>
              <th class="num">24x7 P50 (Zero-Padded)</th>
              <th class="num">Active P50</th>
              <th class="num">Active P90</th>
              <th class="num">Active P95</th>
              <th class="num">Holt Forecast +24h Peak</th>
            </tr>
          </thead>
          <tbody>
            __GSU_ROWS__
          </tbody>
        </table>
      </div>

      <div class="card">
        <h2>
          Interactive Disaggregated Prefill / Decode GPU Pool Sizer
          (llm-d / GKE Inference Gateway)
        </h2>
        <p class="sub">
          Separate compute-bound Prefill replicas (TTFT SLA) from HBM
          bandwidth/KV-bound Decode replicas (TPOT SLA).
        </p>
        <div class="controls-grid">
          <div class="field">
            <label for="pd-qps">Peak Arrival QPS</label>
            <input
              id="pd-qps" type="number" value="18" min="1" max="500" step="1"
              oninput="recalcDisagg()"
            />
          </div>
          <div class="field">
            <label for="pd-in">Avg Input Tokens</label>
            <input
              id="pd-in" type="number" value="6400" min="256" max="128000" step="256"
              oninput="recalcDisagg()"
            />
          </div>
          <div class="field">
            <label for="pd-out">Avg Output Tokens</label>
            <input
              id="pd-out" type="number" value="450" min="32" max="16384" step="50"
              oninput="recalcDisagg()"
            />
          </div>
          <div class="field">
            <label for="pd-ttft">Target TTFT (ms)</label>
            <input
              id="pd-ttft" type="number" value="350" min="50" max="2000" step="25"
              oninput="recalcDisagg()"
            />
          </div>
        </div>
        <div id="pd-output" class="info-banner">__DISAGG_RECOMMENDATION__</div>
      </div>
    </section>

    <!-- TAB 2: SUB-SECOND MICRO-BURST & MODEL-TIER PREFIX-CACHE PHYSICS -->
    <section id="tab-burst" class="tab-panel">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">60s Chart vs Sub-Second Reality</div>
          <div class="kpi-value" id="burst-60s-val">62.0% &rarr; 118.2%</div>
          <span class="badge badge-danger" id="burst-badge">
            60s Illusion Detected (10s Bucket)
          </span>
          <div class="kpi-caption" id="burst-caption">
            Requires 12 GSUs for zero 429s/spillover
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Block-0 Prefix Contamination</div>
          <div class="kpi-value" id="cache-hit-val">0.0% &rarr; 60.8%</div>
          <span class="badge badge-danger" id="cache-block0-badge">
            Dynamic Token @ Offset 120 (&lt;4,096)
          </span>
          <div class="kpi-caption">gemini-3.8-flash uses 4,096-token blocks (6,144 floor)</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Argon/Pro Token Checkpointing</div>
          <div class="kpi-value">70.0% Hit</div>
          <span class="badge badge-green">2,048 Floor + Token Boundary</span>
          <div class="kpi-caption">Checkpoints exact system_instruction &amp; tools boundary</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">1/N Round-Robin Cache Dilution</div>
          <div class="kpi-value">80% &rarr; 10% Hit</div>
          <span class="badge badge-warn">5 Wasted GSUs Across 8 Replicas</span>
          <div class="kpi-caption">Sticky / epp prefix routing saves 54.6% burndown</div>
        </div>
      </div>

      <div class="card">
        <h2>Interactive Sub-Second Token-Bucket &amp; Model-Tier Prefix-Cache Simulator</h2>
        <p class="sub">
          Adjust GSU pool size, prompt tokens, static prefix length, and dynamic
          header offset to test 1s (&ge;50 GSUs) vs 10s (&lt;50 GSUs) buckets and
          Block-0 contamination.
        </p>
        <div class="controls-grid">
          <div class="field">
            <label for="sim-model">Model Tier</label>
            <select id="sim-model" onchange="recalcBurstAndCache()">
              <option value="gemini-3.8-flash">
                gemini-3.8-flash (4,096-block / 6,144 floor)
              </option>
              <option value="gemini-3.8-flash-fast">
                gemini-3.8-flash-fast (2.5x decode speed)
              </option>
              <option value="gemini-4-argon">
                gemini-4-argon (token-boundary / 2,048 floor)
              </option>
              <option value="gemini-3.5-flash-lite">
                gemini-3.5-flash-lite (4,096-block / 6,144 floor)
              </option>
            </select>
          </div>
          <div class="field">
            <label for="sim-gsus">Provisioned GSUs</label>
            <input
              id="sim-gsus" type="number" value="10" min="1" max="200"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-60s-tpm">60s Cloud Monitoring TPM</label>
            <input
              id="sim-60s-tpm" type="number" value="750000" min="10000" step="25000"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-peak-tps">Sub-Second Peak TPS</label>
            <input
              id="sim-peak-tps" type="number" value="25000" min="500" step="1000"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-total-in">Total Prompt Tokens</label>
            <input
              id="sim-total-in" type="number" value="18000" min="512" step="1024"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-static-in">Static Prefix Tokens</label>
            <input
              id="sim-static-in" type="number" value="14000" min="0" step="1024"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-dyn-offset">Dynamic Token Offset (&lt;Static)</label>
            <input
              id="sim-dyn-offset" type="number" value="120" min="0" step="100"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-strip-sig">Multi-Turn Failure Mode</label>
            <select id="sim-strip-sig" onchange="recalcBurstAndCache()">
              <option value="none">Healthy Multi-Turn / Dummy Anchor</option>
              <option value="single_unanchored">
                Single-Turn Without Dummy Anchor (1/N hash)
              </option>
              <option value="strip_sig">
                Strip thought_signature (0.77% hit collapse)
              </option>
            </select>
          </div>
        </div>
        <div id="sim-remediation-box" class="danger-banner">
          Block 0 Contamination Active: Dynamic token at offset 120 (&lt;4,096)
          invalidates all three 4,096-token static blocks (0% cache hit). Move
          dynamic metadata to the tail of the prompt to cut weighted burndown by 44.3%.
        </div>
      </div>
    </section>

    <!-- TAB 3: MULTI-GATEWAY CONTROL PLANE (LITELLM + ENVOY + K8S EPP) -->
    <section id="tab-gateway" class="tab-panel">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">LiteLLM Prisma Engines</div>
          <div class="kpi-value" id="gw-prisma-val">48 Engines</div>
          <span class="badge badge-warn" id="gw-status-badge">
            Tune Required (&ge;500 RPS)
          </span>
          <div class="kpi-caption" id="gw-pg-caption">
            PgBouncer pool=34, Postgres max_conns&ge;88
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Anti-Blackhole Cooldown</div>
          <div class="kpi-value" id="gw-cooldown-val">5s Cooldown</div>
          <span class="badge badge-green">Matched to 10s PT Bucket</span>
          <div class="kpi-caption">
            Prevents 1s/10s micro-burst 429 from blackholing PT
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Envoy AI Gateway CEL</div>
          <div class="kpi-value">Two-Phase RLS</div>
          <span class="badge badge-blue">cost.request=0 + SSE CEL</span>
          <div class="kpi-caption">
            Enforces &le;200K &amp; &gt;200K burndown at &gt;2,000 RPS
          </div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">GKE Inference Gateway (epp)</div>
          <div class="kpi-value">Weight 4:2:2:1</div>
          <span class="badge badge-green">Prefix + KV + Queue + LoRA</span>
          <div class="kpi-caption">Eliminates 1/N GPU prefix-cache dilution</div>
        </div>
      </div>

      <div class="card">
        <h2>Interactive LiteLLM Proxy High-QPS Scale &amp; Bottleneck Auditor</h2>
        <p class="sub">
          Simulate target RPS, streaming SSE concurrency, and pod/worker topology
          to size PgBouncer pools and detect the Envoy data-plane crossover.
        </p>
        <div class="controls-grid">
          <div class="field">
            <label for="gw-rps">Target Gateway RPS</label>
            <input
              id="gw-rps" type="number" value="650" min="10" max="10000" step="50"
              oninput="recalcGatewayAudit()"
            />
          </div>
          <div class="field">
            <label for="gw-sse">Concurrent SSE Streams</label>
            <input
              id="gw-sse" type="number" value="400" min="0" max="5000" step="50"
              oninput="recalcGatewayAudit()"
            />
          </div>
          <div class="field">
            <label for="gw-pods">LiteLLM Pods</label>
            <input
              id="gw-pods" type="number" value="12" min="1" max="100" step="1"
              oninput="recalcGatewayAudit()"
            />
          </div>
          <div class="field">
            <label for="gw-workers">Uvicorn Workers / Pod</label>
            <input
              id="gw-workers" type="number" value="4" min="1" max="16" step="1"
              oninput="recalcGatewayAudit()"
            />
          </div>
        </div>
        <div id="gw-audit-msg" class="warn-banner">
          48 Prisma Rust query engines active across 12 pods. Enable
          use_redis_transaction_buffer: true (10s), LITELLM_RUST=1, and front
          PostgreSQL with PgBouncer transaction pooling.
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <h2>1. LiteLLM Proxy Config (<code>litellm_config.yaml</code>)</h2>
          <p class="sub">
            Tag-based PT &rarr; Priority 1.8x spillover, Non-Prod 0.5x Flex
            isolation, and anti-blackholing cooldowns.
          </p>
          <pre><code>__LITELLM_CFG__</code></pre>
        </div>
        <div class="card">
          <h2>
            2. LiteLLM Weighted Burndown Hook (<code>litellm_burndown_callback.py</code>)
          </h2>
          <p class="sub">
            Enforces weighted burndown quotas, dynamic PT-bypass on burst, and
            0ms <code>traffic_type</code> SpendLogs tagging.
          </p>
          <pre><code>__LITELLM_CB__</code></pre>
        </div>
      </div>

      <div class="two-col">
        <div class="card">
          <h2>
            3. Envoy AI Gateway CEL (<code>AIGatewayRoute</code> +
            <code>BackendTrafficPolicy</code>)
          </h2>
          <p class="sub">
            Two-phase pre-flight check (<code>cost=0</code>) + post-response SSE
            <code>type: CEL</code> burndown deduction.
          </p>
          <pre><code>__ENVOY_CFG__</code></pre>
        </div>
        <div class="card">
          <h2>
            4. GKE Inference Gateway (<code>InferencePool</code> +
            <code>EndpointPickerConfig</code>)
          </h2>
          <p class="sub">
            Prefix-cache affinity and KV-cache utilization pod scoring with
            Critical/Sheddable admission.
          </p>
          <pre><code>__EPP_CFG__</code></pre>
        </div>
      </div>

      <div class="card">
        <h2>8 Canonical Vertex AI Gateway Header Routing Policies (6-Rank UTA Admission)</h2>
        <table>
          <thead>
            <tr>
              <th class="num">#</th>
              <th>Workload Tier &amp; Scenario</th>
              <th>X-Vertex-AI-LLM-Request-Type</th>
              <th>X-Vertex-AI-LLM-Shared-Request-Type</th>
              <th>UTA Admission Rank</th>
              <th>Capacity &amp; Chargeback Behavior</th>
            </tr>
          </thead>
          <tbody>
            __HEADER_ROWS__
          </tbody>
        </table>
      </div>
    </section>

    <!-- TAB 4: FOCUS 1.2 FINOPS, RESIDUAL EDF SCHEDULER & 5 AI KPIS -->
    <section id="tab-finops" class="tab-panel">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Cost / Verified Outcome (CPVO)</div>
          <div class="kpi-value">$__KPI_CPVO__</div>
          <span class="badge badge-green">Bridges to bq-eval v_step1_cost_per_outcome</span>
          <div class="kpi-caption">CPWT_1M: $__KPI_CPWT__ / 1M weighted tokens</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Batch 0.5x Double-Pay Saved</div>
          <div class="kpi-value">$__SCAV_SAVED__/mo</div>
          <span class="badge badge-green">
            Lifts PT Util __SCAV_DAY_UTIL__% &rarr; __SCAV_EFF_UTIL__%
          </span>
          <div class="kpi-caption">Paced online scavenging vs separate 0.5x Batch API</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Shared Folder RHPT Pooling</div>
          <div class="kpi-value">__POOL_SILOED__ &rarr; __POOL_POOLED__ GSUs</div>
          <span class="badge badge-blue">
            Saves $__POOL_MO_SAVINGS__/mo ($__POOL_YR_SAVINGS__/yr)
          </span>
          <div class="kpi-caption">Follow-the-sun EMEA + AMER + APAC spoke pooling</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Token &amp; Cache Efficiency</div>
          <div class="kpi-value">CSR __KPI_CSR__%</div>
          <span class="badge badge-blue">
            TER __KPI_TER__% &bull; ECUR_168h __KPI_ECUR__%
          </span>
          <div class="kpi-caption">FinOps Foundation 5 Standardized AI KPIs</div>
        </div>
      </div>

      <div class="card">
        <h2>
          3-Tier Residual Capacity Scheduler
          (Tier 0 Revenue &gt; Tier 1 Hard-Deadline EDF &gt; Tier 2 Evergreen Water-Fill)
        </h2>
        <p class="sub">
          Admits hard-deadline jobs over residual budget
          <code>A(t) = 0.85 &times; C &minus; R(t)</code> by value density
          (<code>v_i / w_i</code>) in Earliest Deadline First (EDF) order,
          spilling low-density jobs to 0.5x Flex.
        </p>
        <table>
          <thead>
            <tr>
              <th>Job ID</th>
              <th class="num">Business Value</th>
              <th class="num">Burndown Tokens</th>
              <th class="num">Hard Cutoff</th>
              <th class="num">Value Density / 1M</th>
              <th>Admission Decision</th>
              <th>Injected Gateway Headers</th>
              <th class="num">Marginal Cost</th>
            </tr>
          </thead>
          <tbody>
            __SCHED_ROWS__
          </tbody>
        </table>
      </div>

      <div class="card">
        <h2>
          FOCUS 1.2 Two-Pool Chargeback Ledger
          (Active Consumed vs Unallocated Idle + Batch Water-Fill)
        </h2>
        <p class="sub">
          Separates Active Consumed Cost from Unallocated Idle Cost and
          eliminates unweighted raw-token distortion across reasoning-heavy vs
          cache-heavy tenants.
        </p>
        <table>
          <thead>
            <tr>
              <th>Tenant / BU</th>
              <th class="num">Weighted Burndown Tokens</th>
              <th class="num">Weighted Share</th>
              <th class="num">Raw Token Share</th>
              <th class="num">FOCUS Active Cost</th>
              <th class="num">Unweighted Raw Cost</th>
              <th class="num">Raw Distortion</th>
              <th class="num">Naive Full-Pool Cost</th>
              <th class="num">Idle Penalty Avoided</th>
            </tr>
          </thead>
          <tbody>
            __CB_ROWS__
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    window.CAPACITY_SHOWCASE_DATA = __EMBEDDED_JSON__;

    function switchTab(panelId, btnEl) {
      document.querySelectorAll('.tab-panel').forEach(
        el => el.classList.remove('active')
      );
      document.querySelectorAll('.tab-btn').forEach(
        el => el.classList.remove('active')
      );
      document.getElementById(panelId).classList.add('active');
      btnEl.classList.add('active');
    }

    function recalcDisagg() {
      const qps = Math.max(
        0.1,
        parseFloat(document.getElementById('pd-qps').value || '18')
      );
      const inTok = Math.max(
        1,
        parseInt(document.getElementById('pd-in').value || '6400', 10)
      );
      const outTok = Math.max(
        1,
        parseInt(document.getElementById('pd-out').value || '450', 10)
      );
      const ttft = Math.max(
        20,
        parseFloat(document.getElementById('pd-ttft').value || '350')
      );

      const prefillCap = 3200.0 * 4.5 * Math.min(1.0, ttft / 350.0);
      const prefillReps = Math.max(1, Math.ceil((qps * inTok) / prefillCap));
      const activeSeqs = qps * (outTok * 0.025);
      const activeKv = activeSeqs * (inTok + outTok);
      const decodeReps = Math.max(
        1,
        Math.ceil(activeKv / (524288 * 0.80)),
        Math.ceil((qps * outTok) / 800.0)
      );
      const totalDisagg = prefillReps + decodeReps;
      const monoReps = Math.max(
        totalDisagg,
        Math.ceil((qps * (inTok + 4.0 * outTok) * 1.30) / (3200.0 * 0.75))
      );
      const saved = monoReps - totalDisagg;

      document.getElementById('kpi-disagg-reps').textContent =
        prefillReps + 'P + ' + decodeReps + 'D';
      document.getElementById('pd-output').textContent =
        'Deploy ' + prefillReps + ' prefill + ' + decodeReps +
        ' decode replicas (' + totalDisagg + ' total vs ' + monoReps +
        ' monolithic, saving ' + saved + ' H100 replica[s] / $' +
        (saved * 18500).toLocaleString() + '/mo) behind GKE Inference Gateway (epp).';
    }

    function recalcBurstAndCache() {
      const model = document.getElementById('sim-model').value;
      const gsus = Math.max(
        1,
        parseInt(document.getElementById('sim-gsus').value || '10', 10)
      );
      const tpm60 = Math.max(
        0,
        parseFloat(document.getElementById('sim-60s-tpm').value || '750000')
      );
      const peakTps = Math.max(
        0,
        parseFloat(document.getElementById('sim-peak-tps').value || '25000')
      );
      const totalIn = Math.max(
        1,
        parseInt(document.getElementById('sim-total-in').value || '18000', 10)
      );
      const staticRaw = parseInt(
        document.getElementById('sim-static-in').value || '14000',
        10
      );
      const staticIn = Math.min(totalIn, Math.max(0, staticRaw));
      const dynOffset = parseInt(
        document.getElementById('sim-dyn-offset').value || '120',
        10
      );
      const mode = document.getElementById('sim-strip-sig').value;

      let tpsPerGsu = 2015.0;
      if (model.includes('argon')) {
        tpsPerGsu = 500.0;
      } else if (model.includes('-fast')) {
        tpsPerGsu = 1007.5;
      } else if (model.includes('lite')) {
        tpsPerGsu = 4030.0;
      }

      const windowSec = gsus >= 50 ? 1 : 10;
      const committedTps = gsus * tpsPerGsu;
      const ceilingTps = committedTps * 1.05;
      const avgTps = tpm60 / 60.0;
      const util60 = (avgTps / committedTps) * 100.0;
      const utilPeak = (Math.max(avgTps, peakTps) / ceilingTps) * 100.0;
      const reqGsus = Math.ceil(Math.max(avgTps, peakTps) / (tpsPerGsu * 1.05));

      document.getElementById('burst-60s-val').textContent =
        util60.toFixed(1) + '% \u2192 ' + utilPeak.toFixed(1) + '%';
      const burstBadge = document.getElementById('burst-badge');
      if (util60 < 70.0 && utilPeak > 100.0) {
        burstBadge.className = 'badge badge-danger';
        burstBadge.textContent = '60s Illusion Detected (' + windowSec + 's Bucket)';
      } else if (utilPeak > 100.0) {
        burstBadge.className = 'badge badge-warn';
        burstBadge.textContent = 'Spillover Active (' + windowSec + 's Bucket)';
      } else {
        burstBadge.className = 'badge badge-green';
        burstBadge.textContent = 'Within ' + windowSec + 's Token Bucket';
      }
      document.getElementById('burst-caption').textContent =
        'Requires ' + reqGsus + ' GSUs for zero 429s/spillover';

      const isArgon = model.includes('argon');
      const minFloor = isArgon ? 2048 : 6144;
      const blockSize = isArgon ? 1 : 4096;
      const effSpan = dynOffset < staticIn ? dynOffset : staticIn;
      const block0 = !isArgon && dynOffset < 4096;
      let aligned = 0;
      if (effSpan >= minFloor && !block0) {
        aligned = isArgon ? effSpan : Math.floor(effSpan / blockSize) * blockSize;
      }
      let affinity = 0.90;
      if (mode === 'single_unanchored') {
        affinity = 0.125;
      } else if (mode === 'strip_sig') {
        affinity = 0.0077;
      }
      const curHit = ((aligned * affinity) / totalIn) * 100.0;
      let optAligned = 0;
      if (staticIn >= minFloor) {
        optAligned = isArgon ? staticIn : Math.floor(staticIn / blockSize) * blockSize;
      }
      const optHit = ((optAligned * 0.90) / totalIn) * 100.0;

      document.getElementById('cache-hit-val').textContent =
        curHit.toFixed(1) + '% \u2192 ' + optHit.toFixed(1) + '%';
      const cBadge = document.getElementById('cache-block0-badge');
      const remBox = document.getElementById('sim-remediation-box');
      if (block0) {
        cBadge.className = 'badge badge-danger';
        cBadge.textContent = 'Block-0 Contaminated (Offset ' + dynOffset + ' < 4,096)';
        remBox.style.background = 'var(--danger-bg)';
        remBox.style.color = 'var(--danger)';
        remBox.textContent =
          'Block 0 Contamination Active: Dynamic token at offset ' + dynOffset +
          ' (<4,096) invalidates all 4,096-token blocks (0% cache hit). ' +
          'Move dynamic headers/UUIDs to the end of the prompt.';
      } else if (mode !== 'none') {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = mode === 'strip_sig'
          ? 'thought_signature Stripped'
          : 'Single-Turn 1/N Hash';
        remBox.style.background = 'var(--warning-bg)';
        remBox.style.color = 'var(--warning)';
        remBox.textContent = mode === 'strip_sig'
          ? 'Stripping thought_signature collapses multi-turn prefix cache hits ' +
            'to 0.77%. Preserve full modelContent across turns.'
          : 'Single-turn request misses prefix router affinity. Prepend a ' +
            '2-token static dummy (user + model) turn pair.';
      } else {
        cBadge.className = 'badge badge-green';
        cBadge.textContent = 'Prefix Cache Healthy (' + curHit.toFixed(1) + '% Hit)';
        remBox.style.background = 'var(--success-bg)';
        remBox.style.color = 'var(--success)';
        remBox.textContent =
          'Prefix is aligned and routing affinity is active (' +
          aligned.toLocaleString() + ' cacheable prefix tokens).';
      }
    }

    function recalcGatewayAudit() {
      const rps = Math.max(
        1,
        parseInt(document.getElementById('gw-rps').value || '650', 10)
      );
      const sse = Math.max(
        0,
        parseInt(document.getElementById('gw-sse').value || '400', 10)
      );
      const pods = Math.max(
        1,
        parseInt(document.getElementById('gw-pods').value || '12', 10)
      );
      const workers = Math.max(
        1,
        parseInt(document.getElementById('gw-workers').value || '4', 10)
      );
      const prisma = pods * workers;
      const pgb = Math.max(20, Math.min(100, Math.floor(prisma / 2) + 10));
      const pgMax = Math.max(100, prisma + 40);

      document.getElementById('gw-prisma-val').textContent = prisma + ' Engines';
      document.getElementById('gw-pg-caption').textContent =
        'PgBouncer pool=' + pgb + ', Postgres max_conns\u2265' + pgMax;
      const badge = document.getElementById('gw-status-badge');
      const msg = document.getElementById('gw-audit-msg');
      if (rps > 1500 || sse > 1000) {
        badge.className = 'badge badge-danger';
        badge.textContent = 'Migrate Data Plane to Envoy AI Gateway';
        msg.style.background = 'var(--danger-bg)';
        msg.style.color = 'var(--danger)';
        msg.textContent =
          'At ' + rps + ' RPS / ' + sse + ' SSE streams, Python GIL and asyncio ' +
          'event-loop stalls inflate TTFT. Offload data-plane routing and CEL ' +
          'burndown rate limiting to Envoy AI Gateway.';
      } else if (rps >= 500 || sse >= 300 || prisma >= 32) {
        badge.className = 'badge badge-warn';
        badge.textContent = 'Tune Required (\u2265500 RPS)';
        msg.style.background = 'var(--warning-bg)';
        msg.style.color = 'var(--warning)';
        msg.textContent =
          prisma + ' Prisma Rust query engines active across ' + pods +
          ' pods. Enable use_redis_transaction_buffer: true (10s), ' +
          'LITELLM_RUST=1, and PgBouncer transaction pooling.';
      } else {
        badge.className = 'badge badge-green';
        badge.textContent = 'Healthy Scale Envelope';
        msg.style.background = 'var(--success-bg)';
        msg.style.color = 'var(--success)';
        msg.textContent =
          'LiteLLM Proxy is within healthy concurrency limits (' +
          prisma + ' Prisma engines, ' + rps +
          ' RPS). Keep use_redis_transaction_buffer: true enabled.';
      }
    }
  </script>
</body>
</html>"""


def build_showcase_summary_payload() -> dict[str, Any]:
    """Build the complete Showcase executive payload across all 4 architectural planes."""
    gsu_sparse, total_mins = build_demo_throughput(days=7, by_region=True, mode="gsu")
    gsu_recs = build_response(
        project="demo-enterprise-ai",
        days=7,
        sparse_series=gsu_sparse,
        total_minutes=total_mins,
        percentiles=[50, 75, 90, 95],
        active_only=True,
        by_region=True,
        mode="gsu",
    )

    sh_sparse, sh_mins = build_demo_throughput(days=7, by_region=True, mode="self-hosted")
    sh_recs = build_response(
        project="demo-enterprise-ai",
        days=7,
        sparse_series=sh_sparse,
        total_minutes=sh_mins,
        percentiles=[50, 75, 90, 95],
        active_only=True,
        by_region=True,
        mode="self-hosted",
    )

    sh_audit = evaluate_self_hosted_endpoint(
        tpm_series=[520_000.0, 480_000.0, 380_000.0, 290_000.0, 180_000.0],
        profile="llama-3.3-70b-fp8",
        min_replica_count=2,
        max_replica_count=8,
    )
    disagg = size_disaggregated_prefill_decode(
        profile="llama-3.3-70b-fp8",
        peak_qps=18.0,
        avg_input_tokens=6_400,
        avg_output_tokens=450,
    )

    burst = evaluate_micro_burst_window(
        sixty_sec_tpm=750_000.0,
        peak_1s_tps=25_000.0,
        provisioned_gsus=10,
        tps_per_gsu=2_015.0,
    )
    prefix_rr = evaluate_prefix_cache_dilution(
        total_input_tokens=20_000,
        shared_prefix_ratio=0.80,
        baseline_cache_hit_rate=0.80,
        num_round_robin_endpoints=8,
        tps_per_gsu=2_015.0,
    )
    prefix_flash = evaluate_model_tier_prefix_cache(
        model="gemini-3.8-flash",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=120,
        single_turn_request=True,
        dummy_turn_anchor_applied=False,
        strip_thought_signatures=False,
        num_replicas=8,
    )
    prefix_argon = evaluate_model_tier_prefix_cache(
        model="gemini-4-argon",
        total_input_tokens=18_000,
        static_prefix_tokens=14_000,
        dynamic_header_offset_tokens=None,
        single_turn_request=False,
        dummy_turn_anchor_applied=True,
        strip_thought_signatures=False,
        num_replicas=8,
    )

    gateway = generate_gateway_bundle(
        model="gemini-3.8-flash",
        project_id="prod-genai-pt-host",
        location="global",
        provisioned_gsus=20,
        target_rps=650,
        concurrent_sse_streams=400,
        pods=12,
        uvicorn_workers_per_pod=4,
    )

    commitment = optimize_hybrid_commitment(
        tpm_series=[
            420_000.0,
            390_000.0,
            360_000.0,
            310_000.0,
            280_000.0,
            250_000.0,
            240_000.0,
            220_000.0,
            190_000.0,
            160_000.0,
            90_000.0,
            40_000.0,
        ],
        model="gemini-3.8-flash",
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2_000.0,
        paygo_per_1m_burndown_tokens_usd=0.50,
        spillover_tier="priority",
        fsp_discount_pct=15.0,
    )
    chargeback = compute_focus_chargeback(
        hourly_consumed_tpm_by_tenant={
            "claims-triage": [403_000.0] * 8 + [80_600.0] * 16,
            "underwriting-copilot": [302_250.0] * 8 + [60_450.0] * 16,
            "policy-audit": [201_500.0] * 8 + [40_300.0] * 16,
        },
        provisioned_gsus=10,
        tpm_per_gsu=120_900,
        gsu_monthly_price_usd=2_000.0,
        async_batch_waterfill_tpm=362_700.0,
    )
    scavenger = evaluate_batch_scavenger_economics(
        model="gemini-3.8-flash",
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
    )
    schedule = schedule_residual_capacity(
        provisioned_gsus=20,
        tpm_per_gsu=120_900,
        deadline_jobs=[
            DeadlineJobSpec(
                job_id="premarket-portfolio-briefings",
                value_usd=18_000.0,
                burndown_tokens=240_000_000.0,
                deadline_hour=6,
            ),
            DeadlineJobSpec(
                job_id="regulatory-risk-digest",
                value_usd=12_000.0,
                burndown_tokens=180_000_000.0,
                deadline_hour=5,
            ),
            DeadlineJobSpec(
                job_id="low-density-archive-sweep",
                value_usd=900.0,
                burndown_tokens=320_000_000.0,
                deadline_hour=6,
            ),
        ],
    )
    folder_pooling = evaluate_folder_pooling(
        hourly_tpm_by_project={
            "emea-wealth-spoke": [480_000.0, 520_000.0, 410_000.0, 90_000.0, 40_000.0, 30_000.0],
            "amer-retail-spoke": [60_000.0, 80_000.0, 490_000.0, 560_000.0, 510_000.0, 90_000.0],
            "apac-markets-spoke": [510_000.0, 110_000.0, 50_000.0, 40_000.0, 460_000.0, 530_000.0],
        },
        model="gemini-3.8-flash",
        tpm_per_gsu=120_900,
    )
    kpis = compute_finops_kpis()

    return {
        "version": "0.3.0",
        "gsu_recommendations": gsu_recs.model_dump(),
        "self_hosted_recommendations": sh_recs.model_dump(),
        "self_hosted_audit": sh_audit.model_dump(),
        "disaggregated_sizing": disagg.model_dump(),
        "micro_burst": burst.model_dump(),
        "prefix_cache_rr": prefix_rr.model_dump(),
        "prefix_cache_flash": prefix_flash.model_dump(),
        "prefix_cache_argon": prefix_argon.model_dump(),
        "gateway": gateway.model_dump(),
        "commitment": commitment.model_dump(),
        "chargeback": chargeback.model_dump(),
        "scavenger": scavenger.model_dump(),
        "residual_schedule": schedule.model_dump(),
        "folder_pooling": folder_pooling.model_dump(),
        "finops_kpis": kpis.model_dump(),
    }


def render_showcase_portal_html() -> str:
    """Render the self-contained GM3 Light 4-Tab Interactive Web Calculator & Scorecard."""
    data = build_showcase_summary_payload()
    embedded_json = json.dumps(data)

    gsu_rows_html = ""
    for m in data["gsu_recommendations"]["models"]:
        p50 = m["active_hours"]["P50"]
        p90 = m["active_hours"]["P90"]
        p95 = m["active_hours"]["P95"]
        all_p50 = m["all_hours"]["P50"]
        gsu_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(m['model']))}</code></td>"
            f"<td>{html.escape(str(m['region'] or 'global'))}</td>"
            f"<td class='num'>{m['gsu_capacity_tpm']:,}</td>"
            f"<td class='num'>{m['duty_cycle_pct']:.1f}%</td>"
            f"<td class='num'>{all_p50['tpm']:,} ({all_p50['recommended_gsus']} GSU)</td>"
            f"<td class='num'>{p50['tpm']:,} ({p50['recommended_gsus']} GSU)</td>"
            f"<td class='num'>{p90['tpm']:,} ({p90['recommended_gsus']} GSU)</td>"
            f"<td class='num'><strong>{p95['tpm']:,} ({p95['recommended_gsus']} GSU)</strong></td>"
            f"<td class='num'>{m['forecast_24h_peak_tpm']:,} "
            f"({m['forecast_24h_recommended_gsus']} GSU)</td>"
            f"</tr>"
        )

    header_rows_html = ""
    for hp in data["gateway"]["header_policies"]:
        header_rows_html += (
            f"<tr>"
            f"<td class='num'>{hp['tier_id']}</td>"
            f"<td><strong>{html.escape(str(hp['scenario']))}</strong></td>"
            f"<td><code>{html.escape(str(hp['request_type_header']))}</code></td>"
            f"<td><code>{html.escape(str(hp['shared_request_type_header']))}</code></td>"
            f"<td><span class='badge badge-blue'>{html.escape(str(hp['uta_rank']))}</span></td>"
            f"<td>{html.escape(str(hp['billing_and_capacity_behavior']))}</td>"
            f"</tr>"
        )

    cb_rows_html = ""
    for t in data["chargeback"]["tenants"]:
        cb_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(t['tenant']))}</code></td>"
            f"<td class='num'>{t['consumed_burndown_tokens']:,.0f}</td>"
            f"<td class='num'>{t['usage_share_pct']:.1f}%</td>"
            f"<td class='num'>{t['unweighted_raw_token_share_pct']:.1f}%</td>"
            f"<td class='num'><strong>${t['active_allocated_cost_usd']:,.2f}</strong></td>"
            f"<td class='num'>${t['unweighted_raw_token_cost_usd']:,.2f}</td>"
            f"<td class='num'>${t['raw_token_distortion_usd']:+,.2f}</td>"
            f"<td class='num'>${t['naive_full_pool_cost_usd']:,.2f}</td>"
            f"<td class='num' style='color:var(--success);font-weight:600;'>"
            f"${t['idle_penalty_avoided_usd']:,.2f}</td>"
            f"</tr>"
        )

    sched_rows_html = ""
    for j in data["residual_schedule"]["jobs"]:
        badge_cls = "badge-green" if j["status"] == "admitted_edf_pt" else "badge-warn"
        hdr_str = ", ".join(f"{k}: {v}" for k, v in j["routing_headers"].items())
        sched_rows_html += (
            f"<tr>"
            f"<td><code>{html.escape(str(j['job_id']))}</code></td>"
            f"<td class='num'>${j['value_usd']:,.0f}</td>"
            f"<td class='num'>{j['burndown_tokens']:,.0f}</td>"
            f"<td class='num'>T+{j['deadline_hour']}h</td>"
            f"<td class='num'>${j['value_density_per_1m_tokens']:,.2f}</td>"
            f"<td><span class='badge {badge_cls}'>{html.escape(str(j['status']))}</span></td>"
            f"<td><code>{html.escape(hdr_str)}</code></td>"
            f"<td class='num'>${j['marginal_cost_usd']:,.2f}</td>"
            f"</tr>"
        )

    gw = data["gateway"]
    kpis = data["finops_kpis"]
    scav = data["scavenger"]
    fpool = data["folder_pooling"]
    disagg_d = data["disaggregated_sizing"]
    sh_d = data["self_hosted_audit"]

    replacements = {
        "__FAVICON_URI__": _FAVICON_DATA_URI,
        "__LOCKED_GPUS__": str(sh_d["locked_regional_accelerators"]),
        "__STRANDED_GPUS__": str(sh_d["stranded_quota_accelerators"]),
        "__MAX_REPS__": str(sh_d["max_replica_count"]),
        "__ACCEL_PER_REP__": str(sh_d["accelerators_per_replica"]),
        "__ACCEL_TYPE__": html.escape(str(sh_d["accelerator_type"])),
        "__PREFILL_REPS__": str(disagg_d["prefill_replicas"]),
        "__DECODE_REPS__": str(disagg_d["decode_replicas"]),
        "__REPLICA_DELTA__": str(disagg_d["replica_delta"]),
        "__SCALE_ZERO_SAVINGS__": f"{sh_d['scale_to_zero_nonprod_savings_usd']:,.0f}",
        "__GSU_ROWS__": gsu_rows_html,
        "__DISAGG_RECOMMENDATION__": html.escape(str(disagg_d["recommendation"])),
        "__LITELLM_CFG__": html.escape(str(gw["litellm_config_yaml"])),
        "__LITELLM_CB__": html.escape(str(gw["litellm_burndown_callback_py"])),
        "__ENVOY_CFG__": html.escape(str(gw["envoy_ai_gateway_yaml"])),
        "__EPP_CFG__": html.escape(str(gw["gke_epp_inference_pool_yaml"])),
        "__HEADER_ROWS__": header_rows_html,
        "__KPI_CPVO__": f"{kpis['cpvo_usd']:.4f}",
        "__KPI_CPWT__": f"{kpis['cpwt_1m_usd']:.4f}",
        "__SCAV_SAVED__": f"{scav['double_payment_waste_avoided_usd']:,.0f}",
        "__SCAV_DAY_UTIL__": f"{scav['daytime_active_utilization_pct']:.0f}",
        "__SCAV_EFF_UTIL__": f"{scav['effective_utilization_after_scavenging_pct']:.1f}",
        "__POOL_SILOED__": str(fpool["siloed_total_gsus"]),
        "__POOL_POOLED__": str(fpool["pooled_folder_gsus"]),
        "__POOL_MO_SAVINGS__": f"{fpool['monthly_savings_usd']:,.0f}",
        "__POOL_YR_SAVINGS__": f"{fpool['annual_savings_usd']:,.0f}",
        "__KPI_CSR__": f"{kpis['csr_pct']:.1f}",
        "__KPI_TER__": f"{kpis['ter_pct']:.1f}",
        "__KPI_ECUR__": f"{kpis['ecur_168h_pct']:.1f}",
        "__SCHED_ROWS__": sched_rows_html,
        "__CB_ROWS__": cb_rows_html,
        "__EMBEDDED_JSON__": embedded_json,
    }

    rendered = _PORTAL_TEMPLATE
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered
