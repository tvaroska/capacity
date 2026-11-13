---
name: capacity
description: >-
  Stateless Google Cloud Provisioned Throughput (1P Gemini & 3P Claude) and self-hosted GPU/TPU
  capacity planner, sub-second burndown & prefix-cache simulator, gateway policy compiler, and
  FOCUS 1.2 Two-Pool chargeback tool. Use when sizing GSUs or vLLM replicas from Cloud Monitoring
  or BigQuery inference_request / OTel logs, auditing 429 bursts and prefix caching, generating
  gateway configs, or rendering a self-contained HTML capacity scorecard.
---

# Capacity Skill (`capacity`)

Drive every capability of [`capacity`](../../README.md) through its stateless, read-only CLI (`--format json`). No resources are ever created or mutated (`roles/monitoring.viewer` / `roles/logging.viewer` only).

## Core Workflow

1. **Pre-flight access check (when collecting live GCP metrics):**
   ```bash
   uv run capacity collect --project <PROJECT_ID> --check-auth --format json
   ```
   If `authenticated` is `false`, instruct the user to run `gcloud auth application-default login`. If `monitoring_api_enabled` or `has_viewer_role` is `false`, surface the returned `instructions`.

2. **Collect customer metrics and/or BigQuery `inference_request` / OTel logs into a `ShowcaseScenario`:**
   ```bash
   # Live Cloud Monitoring + optional commercial order metadata overlay
   uv run capacity collect --project <PROJECT_ID> --days 7 \
     --orders-file examples/orders_overlay.yaml \
     --gateway-mode central_proxy --pooling-mode 1p_vs_3p_hub \
     --strict-public-check --out /tmp/scenario.json

   # Offline from exported BigQuery Log Analytics (`inference_request`) or OTel JSONL
   uv run capacity collect --logs-file bql_inference.jsonl \
     --orders-file examples/orders_overlay.yaml \
     --gateway-mode central_proxy --no-self-hosted \
     --strict-public-check --out /tmp/scenario.json
   ```
   See [`examples/bigquery_inference_request_export.sql`](../../examples/bigquery_inference_request_export.sql) for the customer BigQuery export query.

3. **Render the 4-tab GM3 Light HTML scorecard or inspect JSON output:**
   ```bash
   uv run capacity demo --scenario-file /tmp/scenario.json --strict-public-check \
     --html-out /tmp/capacity_scorecard.html --format json
   ```

4. **Targeted deep-dive commands (all support `--format json`):**
   - **Empirical percentile & daily-peak Holt sizing:** `uv run capacity recommend --project <ID> --active-only --by-region --format json`
   - **Newsvendor hybrid commitment + sub-second burst + prefix cache:** `uv run capacity optimize --model gemini-3.8-flash --sixty-sec-tpm 750000 --peak-1s-tps 25000 --format json`
   - **Gateway policies & manifests:** `uv run capacity gateway --target all --model gemini-3.8-flash --gsus 20 --format json`
   - **3-Tier overnight EDF residual schedule:** `uv run capacity schedule --model gemini-3.8-flash --gsus 20 --format json`
   - **FOCUS 1.2 Two-Pool chargeback:** `uv run capacity chargeback --model gemini-3.8-flash --gsus 20 --tenant-file examples/chargeback_tenants.json --format json`

## Guardrails

- Read [`CRITICAL.md`](../../CRITICAL.md) and [`AGENTS.md`](../../AGENTS.md) before modifying repository code.
- Always pass `--strict-public-check` when exporting shareable HTML scorecards so unredacted 10–14 digit project/order IDs or internal release suffixes fail closed.
