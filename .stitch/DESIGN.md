# DESIGN.md — Capacity Showcase Visual & UX Contract (GM3 Light Tier 2)

## 1. Visual Direction & Domain Persona
- **Tier**: Tier 2 — Reusable Showcase Platform (`Google Cloud / Material 3 GM3 Light`).
- **Aesthetic Persona**: High-trust cloud capacity, tokenomics, and FinOps engineering workbench (`Google Cloud Console` / `BigQuery` / `Looker` clarity).
- **Theme Polarity**: Strictly **Daylight-first (`GM3 Light`)**. Dark mode is never used as the default surface so tables, formulas, and architecture diagrams copy cleanly into executive slides and technical briefings.
- **Tab Identity**: Every HTML view (`GET /` interactive web calculator and `capacity demo --html-out` static executive scorecard) includes a distinct high-contrast inline SVG `<link rel="icon">` favicon (`#1a73e8` rounded square with capacity pulse bars).

## 2. Semantic Color Tokens (`:root`)
```css
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
```

## 3. Typography & Numeric Discipline
- **UI & Headings**: `'Google Sans'`, `'Inter'`, `'Segoe UI'`, `system-ui`, `-apple-system`, `sans-serif`.
- **Code, Headers & Formulas**: `'Roboto Mono'`, `'JetBrains Mono'`, `ui-monospace`, `monospace`.
- **Tabular Numerals**: All KPI hero cards, GSU/replica tables, TPM percentiles, and USD chargeback ledgers enforce `font-variant-numeric: tabular-nums` with right-aligned numeric columns.

## 4. Layout & 4-Tab Interactive Architecture (`GET /` & `--html-out`)
- **Max Container Width**: `1280px`, `24px` outer padding, `16px` grid gap, `12px` card border-radius with `1px solid var(--outline-variant)`.
- **Card Density Cap**: Maximum 4 data points per summary card (`Label`, `Primary Metric`, `Delta / Status Badge`, `1-line Formula or Caption`) and at most 4 KPI cards per row.
- **Four Interactive Workbench Tabs**:
  1. **Tab 1 — Dual-Mode Sizing & Holt Forecast**: Managed Gemini Platform GSUs (`gemini-3.8-flash`, `gemini-3.8-flash-fast`, `gemini-4-argon`, `gemini-3.5-flash-lite`, `gemini-embedding-2`) + Self-Hosted GPU/TPU (`llama-3.3-70b-fp8`, `qwen-2.5-72b-fp8`, `gemma-3-27b-bf16`) + Disaggregated Prefill/Decode Pool Sizer.
  2. **Tab 2 — Sub-Second Micro-Burst & Prefix-Cache Physics**: `1s` (`>=50` GSUs) vs. `10s` (`<50` GSUs) token-bucket enforcement, `>200K` context cliff (`2.0x` input / `7.5x` output), and Model-Tier Prefix Caching (`4,096`-token Block 0 contamination & `6,144` floor on `gemini-3.8-flash` vs. `2,048` token-level boundary checkpointing on `gemini-4-argon`, Turn-0 dummy-turn routing hash fix, and `thought_signature` stripping penalty).
  3. **Tab 3 — Multi-Gateway Config & Scale Advisor**: Interactive generator and bottleneck auditor for **LiteLLM Proxy** (`litellm_config.yaml` + `litellm_burndown_callback.py` + Prisma/Postgres/SSE scale audit), **Envoy AI Gateway** (`AIGatewayRoute` CEL `llmRequestCosts` + `BackendTrafficPolicy`), and **GKE Inference Gateway** (`InferencePool` + `EndpointPickerConfig` `epp`), plus the 8 Vertex AI gateway header policies.
  4. **Tab 4 — FOCUS 1.2 FinOps, Residual Scheduler & 5 AI KPIs**: Newsvendor hybrid commitment (`PT + FSP + Priority 1.8x / Flex 0.5x`), Batch API (`0.5x`) double-payment trap vs. `$0` online PT scavenging, 3-Tier Residual Capacity Scheduler (`Revenue > Hard-Deadline EDF > Evergreen Water-Fill`), Shared Folder RHPT pooling vs. siloed project stranding, FOCUS 1.2 Two-Pool Chargeback, and the 5 FinOps for AI KPIs (`CPWT_1M`, `CPVO`, `TER`, `CSR`, `ECUR_168h`).
