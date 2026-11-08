# CRITICAL.md — Hard Invariants (`capacity`)

1. **Zero Customer Data or Internal Identifiers (`ADR-005`):** Never commit customer names, project numbers, billing IDs, internal shortlinks, Buganizer IDs, or hardcoded machine paths (`/usr/local/...` or local file-URI schemes) into code, tests, or documentation.
2. **Cloud Build Only (`ADR-005`):** All CI/CD runs via [`cloudbuild.yaml`](cloudbuild.yaml). Never create `.github/workflows/`.
3. **Server-Side Aggregation & Regional Bucketing (`ADR-001`):** Always pass `monitoring_v3.Aggregation(cross_series_reducer=REDUCE_SUM, group_by_fields=...)` to `list_time_series` so multi-region and multi-stream metrics are summed per minute before computing percentiles.
4. **Active-Hours vs. All-Hours Transparency (`ADR-001`):** Never silently zero-pad 8x5 business-hours workloads across 24x7 weekend minutes without exposing `active_hours` percentiles and `duty_cycle_pct`.
5. **Weighted Burndown & Sub-Minute Token-Bucket Physics (`ADR-002`):** Always account for output/reasoning token burndown multipliers (`4.0x` at `<=200K`, `7.5x` at `>200K`), cached input discounts (`0.1x` / `0.2x`), `1s` (`>=50` GSUs) vs. `10s` (`<50` GSUs) refill windows, and $1/N$ round-robin prefix-cache dilution.
6. **Two-Pool FOCUS 1.2 Chargeback (`ADR-003`):** Always separate **Pool 1: Active Consumed Capacity Cost** (allocated by weighted burndown tokens, never unweighted raw tokens) from **Pool 2: Unallocated Idle Capacity Cost** (reclaimed off-peak via `$0`-marginal-cost asynchronous batch water-filling).
7. **Dual-Mode Coverage (`ADR-006`):** Every sizing, burst/KV-cache, Newsvendor, and FOCUS 1.2 workflow must support both **Managed Reserved Capacity (`--mode gsu`)** and **Self-Hosted GPU/TPU Replicas (`--mode self-hosted` + `--vllm-file`)**.
