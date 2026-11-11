"""HTML/CSS/JS template for the GM3 Light 4-Tab Web Workbench (`portal_template.py`)."""

from __future__ import annotations

from capacity.portal_scripts import PORTAL_CLIENT_SCRIPT

FAVICON_DATA_URI = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%231a73e8'/%3E"
    "%3Cpath d='M14 46V30h8v16h-8zm14 0V18h8v28h-8zm14 0V24h8v22h-8z' fill='%23ffffff'/%3E"
    "%3Ccircle cx='46' cy='16' r='5' fill='%23fbbc04'/%3E%3C/svg%3E"
)

PORTAL_TEMPLATE = """<!DOCTYPE html>
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
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { font-size: 19px; font-weight: 700; margin: 0; }
    .brand p { margin: 2px 0 0; font-size: 12.5px; color: var(--on-surface-variant); }
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
    main { max-width: 1280px; margin: 20px auto 48px; padding: 0 24px; }
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
    .card h2 { font-size: 15.5px; margin: 0 0 6px; color: var(--on-surface); }
    .card p.sub { margin: 0 0 14px; font-size: 12.5px; color: var(--on-surface-variant); }
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
    .kpi-value { font-size: 26px; font-weight: 700; margin: 6px 0; color: var(--on-surface); }
    .kpi-caption { font-size: 12px; color: var(--on-surface-variant); }
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
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--outline-variant);
      text-align: left;
    }
    th { background: var(--surface-container); font-weight: 600; color: var(--on-surface-variant); }
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
      __DOCS_HEADER_RIGHT__
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
          <span class="badge badge-green" id="kpi-disagg-badge">
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
          suffix normalization, and daily-peak Holt trend projection.
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
              id="pd-qps" type="number" value="18" min="0.1" max="500" step="1"
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
          <div class="kpi-value" id="cache-hit-val">
            __FLASH_CACHE_CUR__% &rarr; __FLASH_CACHE_OPT__%
          </div>
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
              id="sim-60s-tpm" type="number" value="750000" min="0" step="25000"
              oninput="recalcBurstAndCache()"
            />
          </div>
          <div class="field">
            <label for="sim-peak-tps">Sub-Second Peak TPS</label>
            <input
              id="sim-peak-tps" type="number" value="25000" min="0" step="1000"
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

    <section id="tab-gateway" class="tab-panel">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">LiteLLM Prisma Engines</div>
          <div class="kpi-value" id="gw-prisma-val">48 Engines</div>
          <span class="badge badge-warn" id="gw-status-badge">
            Tune Required (&ge;500 RPS)
          </span>
          <div class="kpi-caption" id="gw-pg-caption">
            PgBouncer pool=34, Postgres max_conns&ge;100
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
          <span class="badge badge-blue">cost.request=256 + SSE CEL</span>
          <div class="kpi-caption">
            Enforces &le;200K &amp; &gt;200K per-BU burndown at &gt;2,000 RPS
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
            Enforces stateful Redis weighted burndown quotas, dynamic PT-bypass on burst,
            and 0ms <code>traffic_type</code> SpendLogs tagging.
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
            Two-phase pre-flight floor (<code>cost=256</code>) + post-response SSE
            <code>type: CEL</code> per-BU burndown deduction.
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
          Selects the exact value-maximizing deadline-feasible subset over residual budget
          <code>A(t) = 0.85 &times; C &minus; R(t)</code> and executes admitted jobs in
          Earliest Deadline First (EDF) order, spilling excess jobs to 0.5x Flex.
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
__CLIENT_SCRIPT__
</body>
</html>""".replace("__CLIENT_SCRIPT__", PORTAL_CLIENT_SCRIPT)
